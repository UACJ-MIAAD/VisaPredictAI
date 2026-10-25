"""Prepare one assisted bulletin, or explicitly continue its governed publication."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from bs4 import BeautifulSoup

from pipeline import ingest_pdf
from pipeline.pdf_bulletin import as_html, extract_text, parse_layout
from vp_data.fetchers import FetchError, requests_fetcher
from vp_data.visa_common import SCRAPER_COUNTRIES, parse_tables

ROOT = Path(__file__).resolve().parents[1]
REPO = "UACJ-MIAAD/VisaPredictAI"
WORKFLOW = "freeze_and_rebuild.yml"
BUCKET = "visapredictai-raw-snapshots"
PUBLIC_OUTPUTS = {
    "data/processed/visa_panel_long.csv": "visa_panel_long.csv",
    "data/processed/bulletins.json": "bulletins.json",
    "reports/prospective/web_forecasts.csv": "forecasts.csv",
    "reports/prospective/web_forecasts_meta.json": "forecasts_meta.json",
    "reports/prospective/forecast_scorecard_meta.json": "forecast_scorecard.json",
    "reports/governance/key_facts.json": "key_facts.json",
    "reports/governance/ingestion_state.json": "ingestion_state.json",
    "reports/governance/MODEL_CARD.md": "MODEL_CARD.md",
    "reports/eda/eda_facts.json": "eda_facts.json",
    "reports/eda/eda_report.pdf": "eda_report.pdf",
    "reports/eda/en/eda_report.pdf": "eda_report_en.pdf",
    "reports/fe/fe_facts.json": "fe_facts.json",
    "reports/fe/fe_report.pdf": "fe_report.pdf",
    "reports/fe/en/fe_report.pdf": "fe_report_en.pdf",
    "reports/governance/e5_facts.json": "e5_facts.json",
    "reports/eval/series_cohorts.json": "series_cohorts.json",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(data: bytes | str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON contiene claves duplicadas")
            result[key] = value
        return result

    result = json.loads(data, object_pairs_hook=pairs)
    if not isinstance(result, dict):
        raise ValueError("se exige un objeto JSON")
    return result


def checked_file(path: Path, boundary: Path) -> bytes:
    if boundary.is_symlink():
        raise ValueError("raíz del archivo enlazada")
    relative = path.absolute().relative_to(boundary.absolute())
    if ".." in relative.parts:
        raise ValueError("ruta fuera del archivo acreditado")
    current = boundary
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("ruta enlazada no autorizada")
    if not path.is_file():
        raise ValueError("archivo del recibo ausente")
    return path.read_bytes()


def semantic_cells(body: bytes, month: str) -> list[tuple]:
    """Compare a preserved historical HTML and the PDF without replacing either."""
    from datetime import datetime

    soup = BeautifulSoup(body, "html.parser")
    date = datetime.strptime(month, "%Y-%m")
    if ingest_pdf._completeness_problems(soup, date):
        raise ValueError("snapshot del candidato incompleto")
    result = []
    for module, block in ((ingest_pdf.employment, "employment"), (ingest_pdf.family, "family")):
        matcher = module.is_employment_section if block == "employment" else module.is_family_section
        tables = parse_tables(soup, date, matcher)
        for country in SCRAPER_COUNTRIES:
            frame = module.extract_country_data(country, tables)
            level = "EB_level" if block == "employment" else "F_level"
            for row in frame[[level, "table_type", "raw_value", "status"]].itertuples(index=False, name=None):
                result.append((block, country, *map(str, row)))
    if len(result) != 150:
        raise ValueError("snapshot no tiene 150 celdas")
    return sorted(result)


def prepare(month: str, file: Path | None) -> Path:
    args = ["--month", month] + (["--file", str(file)] if file else [])
    # The core returns no caller-selected receipt or observation. Its last line
    # names the immutable archive it just produced.
    completed = subprocess.run(
        [sys.executable, "-m", "pipeline.ingest_pdf", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    return Path(completed.stdout.strip().splitlines()[-1])


def validate_receipt(path: Path, month: str) -> dict:
    """Recheck archived evidence and reparse the PDF before any remote effect."""
    boundary = ROOT / "data/pdf_intake"
    raw = checked_file(path, boundary)
    receipt = load_json(raw)
    if (
        not isinstance(receipt.get("files"), dict)
        or not isinstance(receipt.get("producer_files"), dict)
        or not isinstance(receipt.get("candidate"), str)
        or receipt.get("candidate_promoted") is not False
    ):
        raise ValueError("receipt no tiene inventario y candidato tipados")
    if (
        not isinstance(receipt, dict)
        or any(
            receipt.get(k) != v
            for k, v in {
                "schema": "visapredict.pdf-intake/1",
                "month": month,
                "status": "local_candidate_green",
                "publication": "not_requested",
                "candidate_promoted": False,
                "scoring_monthly": "not_executed",
            }.items()
        )
        or receipt.get("source_git_dirty") is not False
    ):
        raise ValueError("receipt no acredita candidato local limpio")
    archive = path.parent
    for name, expected in receipt.get("files", {}).items():
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
            raise ValueError("inventario del receipt inválido")
        if sha(checked_file(archive / name, archive)) != expected:
            raise ValueError("bytes del receipt cambiaron")
    mandatory = {"source.pdf", "bulletin.html", "cells.json", "extracted.txt"}
    if not mandatory.issubset(receipt.get("files", {})) or "candidate" not in receipt:
        raise ValueError("receipt incompleto")
    candidate = ROOT / receipt["candidate"]
    for relative in (
        "data/processed/visa_panel_long.csv",
        "data/processed/bulletins.json",
        "data/processed/visapredict.duckdb",
    ):
        dest = candidate / relative
        checked_file(dest, archive)
        if str(dest.relative_to(archive)) not in receipt["files"]:
            raise ValueError("candidato fuera del inventario del receipt")
    # Receipt provenance must refer to committed producer bytes, not a claimed
    # future SHA. The downstream monthly workflow rebuilds from the snapshot.
    source_sha = receipt.get("source_git_sha", "")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("receipt sin SHA de fuentes")
    expected_producers = {"pipeline/ingest_pdf.py", "pipeline/pdf_bulletin.py", "Makefile"}
    if set(receipt.get("producer_files", {})) != expected_producers:
        raise ValueError("receipt sin productores completos")
    for name, digest in receipt["producer_files"].items():
        blob = subprocess.check_output(
            ["git", "--no-replace-objects", "show", f"{source_sha}:{name}"],
            cwd=ROOT,
            timeout=30,
        )
        if sha(blob) != digest:
            raise ValueError("productor del receipt no coincide con Git")
    head = (
        subprocess.check_output(["git", "--no-replace-objects", "rev-parse", "HEAD"], cwd=ROOT, timeout=30)
        .decode()
        .strip()
    )
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, timeout=30)
    if head != source_sha or dirty:
        raise ValueError("HEAD o árbol del productor cambió; preparar un recibo nuevo")
    pdf = checked_file(archive / "source.pdf", archive)
    if sha(pdf) != receipt.get("pdf_sha256"):
        raise ValueError("PDF del receipt cambió")
    with tempfile.TemporaryDirectory(prefix="bulletin-validated-pdf-") as temporary:
        frozen_pdf = Path(temporary) / "source.pdf"
        frozen_pdf.write_bytes(pdf)
        text, _ = extract_text(frozen_pdf)
    regenerated = as_html(parse_layout(text, month), month, sha(pdf))
    html = checked_file(archive / "bulletin.html", archive)
    if regenerated != html:
        raise ValueError("HTML no deriva del PDF acreditado")
    english = next(n for n, number in ingest_pdf.MONTH_MAP.items() if number == int(month[5:]))
    frozen = candidate / f"data/snapshots/visa-bulletin-for-{english}-{month[:4]}.html"
    body = checked_file(frozen, archive)
    if str(frozen.relative_to(archive)) not in receipt["files"] or semantic_cells(body, month) != semantic_cells(
        html, month
    ):
        raise ValueError("snapshot preservado no coincide con las celdas del PDF")
    return {
        "month": month,
        "archive": archive,
        "html": frozen,
        "html_sha": sha(body),
        "receipt_sha": sha(raw),
        "source_sha": source_sha,
    }


class Transport:
    """Bounded CLI transport; no credentials or environment dumps in receipts."""

    def command(self, argv: list[str], timeout: int = 60) -> bytes:
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"transporte {argv[0]} falló (rc={result.returncode}); sin continuar")
        return result.stdout

    def api(self, path: str) -> dict:
        return json.loads(self.command(["gh", "api", f"repos/{REPO}/{path}"]))

    def public_get(self, url: str) -> bytes:
        data = requests_fetcher(timeout=45)(url)
        if len(data) > 16_000_000:
            raise ValueError("artefacto público excede la cota")
        return data

    def s3_get(self, key: str) -> bytes | None:
        # Only a genuine Not Found permits creation. Authentication, network and
        # permission failures must never be reclassified as an absent object.
        head = subprocess.run(
            ["aws", "s3api", "head-object", "--bucket", BUCKET, "--key", key],
            capture_output=True,
            timeout=60,
        )
        if head.returncode:
            if b"(404)" in head.stderr or b"(NoSuchKey)" in head.stderr:
                return None
            raise RuntimeError("S3 HEAD falló; no se crea ni se dispara nada")
        metadata = json.loads(head.stdout)
        with tempfile.TemporaryDirectory(prefix="bulletin-s3-") as tmp:
            dest = Path(tmp) / "snapshot"
            self.command(
                [
                    "aws",
                    "s3api",
                    "get-object",
                    "--bucket",
                    BUCKET,
                    "--key",
                    key,
                    "--if-match",
                    metadata["ETag"],
                    str(dest),
                ]
            )
            return dest.read_bytes()

    def s3_put(self, key: str, path: Path) -> None:
        self.command(
            [
                "aws",
                "s3api",
                "put-object",
                "--bucket",
                BUCKET,
                "--key",
                key,
                "--body",
                str(path),
                "--if-none-match",
                "*",
            ],
            timeout=120,
        )


def archive_snapshot(transport, key: str, path: Path, expected: str | None = None) -> str:
    source = path.read_bytes()
    if expected is not None and sha(source) != expected:
        raise ValueError("HTML cambió tras acreditar el receipt")
    existing = transport.s3_get(key)
    if existing is not None:
        if existing != source:
            raise ValueError("S3 contiene otro snapshot; no se sobrescribe")
        return "identical"
    with tempfile.TemporaryDirectory(prefix="bulletin-validated-html-") as temporary:
        frozen = Path(temporary) / "bulletin.html"
        frozen.write_bytes(source)
        transport.s3_put(key, frozen)
    if transport.s3_get(key) != source:
        raise ValueError("S3 readback distinto; sin dispatch")
    return "created"


def require_success(run: dict) -> bool:
    if run.get("status") != "completed":
        return False
    if run.get("conclusion") != "success":
        raise ValueError(f"corrida mensual terminó {run.get('conclusion')}; no es publicación verde")
    return True


def consumer_name(path: str) -> str | None:
    if path in PUBLIC_OUTPUTS:
        return PUBLIC_OUTPUTS[path]
    if found := re.fullmatch(r"reports/(eda|fe)/gallery/((?:en/dark/|en/|dark/)?[\w-]+\.png)", path):
        return f"{found[1]}/{found[2]}"
    if found := re.fullmatch(r"vp_data/contracts/([\w.-]+\.json)", path):
        return f"contracts/{found[1]}"
    return None


def verify_consumers(transport, manifest_bytes: bytes, deadline: float | None = None) -> dict:
    manifest = json.loads(manifest_bytes)
    result = {}
    for host in ("https://visapredictai.com", "https://main--visapredictai.netlify.app"):
        state = json.loads(transport.public_get(f"{host}/data/release-state.json"))
        if state.get("status") != "fresh" or state.get("release_id") != manifest["release_id"]:
            raise ValueError("consumidor no sirve la release fresh esperada")
        served_manifest = transport.public_get(f"{host}/data/release_manifest.json")
        if served_manifest != manifest_bytes:
            raise ValueError("manifiesto servido diferente")
        checked = {}
        for entry in manifest["artifacts"]:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("se agotó la espera del consumidor; no repetir el dispatch")
            name = consumer_name(entry["path"])
            if name:
                data = transport.public_get(f"{host}/data/{name}")
                if sha(data) != entry["sha256"] or len(data) != entry["size"]:
                    raise ValueError(f"artefacto servido distinto: {name}")
                checked[name] = sha(data)
        if (
            "visa_panel_long.csv" not in checked
            or "forecasts.csv" not in checked
            or "ingestion_state.json" not in checked
        ):
            raise ValueError("manifiesto no contiene consumidores críticos")
        result[host] = {"release_id": state["release_id"], "fetched_at": state.get("fetched_at"), "hashes": checked}
    return result


def wait_consumers(transport, manifest_bytes: bytes, seconds: int) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        try:
            return verify_consumers(transport, manifest_bytes, deadline)
        except ValueError, OSError, FetchError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(5)


def match_runs(runs: list[dict], request: str, head: str) -> list[dict]:
    return [
        r
        for r in runs
        if r.get("display_title") == f"Assisted bulletin {request}"
        and r.get("head_sha") == head
        and r.get("head_branch") == "main"
    ]


def find_dispatch(transport, request: str, head: str, seconds: int = 120) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        runs = transport.api(f"actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=30")
        matches = match_runs(runs["workflow_runs"], request, head)
        changed = [
            r
            for r in runs["workflow_runs"]
            if r.get("display_title") == f"Assisted bulletin {request}"
            and r.get("head_branch") == "main"
            and r.get("head_sha") != head
        ]
        if changed:
            raise ValueError(f"HEAD de la corrida cambió; gate esperado la bloquea: run {changed[0]['id']}")
        if len(matches) > 1:
            raise ValueError("dispatch ambiguo; revisar corridas sin duplicar el mes")
        if matches:
            return matches[0]
        time.sleep(5)
    raise TimeoutError("dispatch no identificado dentro de la cota; no repetir a ciegas")


def verify_telemetry(folder: Path, run_id: int) -> dict:
    receipt = load_json(checked_file(folder / "receipt.json", folder))
    if (
        receipt.get("schema") != "scoring-telemetry-archive/1"
        or receipt.get("complete") is not True
        or receipt.get("run_id") != str(run_id)
        or receipt.get("missing") != []
    ):
        raise ValueError("telemetría original incompleta o de otra corrida")
    expected = {"tracking_monthly.txt", "web_forecast_scoring.jsonl"}
    if set(receipt.get("files", {})) != expected:
        raise ValueError("inventario de telemetría incompleto")
    hashes = {}
    for name, item in receipt["files"].items():
        content = checked_file(folder / name, folder)
        if sha(content) != item.get("sha256") or len(content) != item.get("bytes"):
            raise ValueError("bytes de telemetría cambiaron")
        hashes[name] = sha(content)
    records = [load_json(line) for line in (folder / "web_forecast_scoring.jsonl").read_text().splitlines()]
    if (
        not records
        or len(records) != receipt.get("records")
        or any(
            r.get("experiment") != "web_forecast_scoring"
            or r.get("provenance", {}).get("pipeline_run_id") != str(run_id)
            for r in records
        )
    ):
        raise ValueError("record original no corresponde a la corrida")
    hashes["receipt.json"] = sha((folder / "receipt.json").read_bytes())
    return hashes


def preserve_telemetry(transport, directory: Path, run_id: int) -> dict:
    destination = directory / "telemetry"
    if destination.is_symlink():
        raise ValueError("telemetría enlazada fuera del archivo")
    destination.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="telemetry-download-", dir=directory) as temporary:
        folder = Path(temporary)
        transport.command(
            [
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                REPO,
                "--name",
                f"scoring-telemetry-{run_id}",
                "--dir",
                str(folder),
            ],
            timeout=120,
        )
        hashes = verify_telemetry(folder, run_id)
        for name in hashes:
            target = destination / name
            if target.exists():
                checked_file(target, destination)
            ingest_pdf.write_once(target, (folder / name).read_bytes())
    return hashes


@contextlib.contextmanager
def publication_lock(directory: Path):
    descriptor = os.open(directory / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("lock de publicación inseguro")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("otro proceso conserva esta operación de publicación") from exc
        yield
    finally:
        os.close(descriptor)


def publish(evidence: dict, transport=None, seconds: int = 3600) -> Path:
    directory = evidence["archive"] / "publication"
    if directory.is_symlink():
        raise ValueError("archivo de publicación enlazado")
    directory.mkdir(exist_ok=True)
    with publication_lock(directory):
        return _publish_locked(evidence, transport, seconds)


def _publish_locked(evidence: dict, transport=None, seconds: int = 3600) -> Path:
    transport = transport or Transport()
    receipt_dir = evidence["archive"] / "publication"
    if receipt_dir.is_symlink():
        raise ValueError("archivo de publicación enlazado")
    receipt_dir.mkdir(exist_ok=True)
    operation = receipt_dir / "operation.json"
    state = (
        load_json(checked_file(operation, receipt_dir))
        if operation.exists()
        else {
            "schema": "visapredict.assisted-publication/1",
            "month": evidence["month"],
            "receipt_sha256": evidence["receipt_sha"],
            "request": evidence["receipt_sha"][:24],
            "status": "prepared",
            "effects": [],
        }
    )
    if (
        state.get("receipt_sha256") != evidence["receipt_sha"]
        or state.get("request") != evidence["receipt_sha"][:24]
        or state.get("month") != evidence["month"]
        or state.get("schema") != "visapredict.assisted-publication/1"
        or ("head" in state and state["head"] != evidence["source_sha"])
    ):
        raise ValueError("operación vinculada a otro receipt")

    def save():
        with tempfile.NamedTemporaryFile(mode="w", dir=receipt_dir, prefix="operation-", delete=False) as stream:
            stream.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.replace(operation)
        directory_fd = os.open(receipt_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    try:
        # An existing, bound attempt is observation only. Its own cron may have
        # advanced main; a retry must not authorize another upload or dispatch.
        if not state.get("dispatch_attempted") and "run_id" not in state:
            if transport.api("commits/main")["sha"] != evidence["source_sha"]:
                raise ValueError("recibo no corresponde al código integrado en main; no publicar")
            name = ingest_pdf.MONTH_MAP
            english = next(n for n, number in name.items() if number == int(evidence["month"][5:]))
            key = f"raw-html/visa-bulletin-for-{english}-{evidence['month'][:4]}.html"
            state["s3"] = archive_snapshot(transport, key, evidence["html"], evidence["html_sha"])
            save()
            manifest = transport.api("contents/reports/release/release_manifest.json?ref=main")
            published = json.loads(base64.b64decode(manifest["content"]))
            if published.get("panel_vintage", "") >= evidence["month"]:
                state["consumers"] = wait_consumers(transport, base64.b64decode(manifest["content"]), min(seconds, 300))
                state.update(status="already_sealed_no_dispatch", release_id=published["release_id"])
                save()
                return operation
        if "run_id" not in state:
            head = evidence["source_sha"]
            # A dispatch transport failure can be an accepted request with a lost
            # response. Record the attempt first; a retry searches the same ID.
            if not state.get("dispatch_attempted"):
                state.update(dispatch_attempted=True, head=head)
                save()
                transport.command(
                    [
                        "gh",
                        "workflow",
                        "run",
                        WORKFLOW,
                        "--repo",
                        REPO,
                        "--ref",
                        "main",
                        "-f",
                        "skip_freeze=true",
                        "-f",
                        f"operator_request_id={state['request']}",
                        "-f",
                        f"expected_source_sha={evidence['source_sha']}",
                    ]
                )
            run = find_dispatch(transport, state["request"], state["head"])
            state["run_id"] = run["id"]
            save()
        deadline = time.monotonic() + seconds
        while True:
            if type(state["run_id"]) is not int or state["run_id"] < 1:
                raise ValueError("run_id inválido")
            run = transport.api(f"actions/runs/{state['run_id']}")
            if match_runs([run], state["request"], state["head"]) != [run]:
                raise ValueError("corrida no corresponde al recibo y HEAD registrados")
            if run.get("status") == "completed":
                # Download original bytes on both success and failure. No summary
                # manufactured locally may replace missing original telemetry.
                state["conclusion"] = run.get("conclusion")
                state["telemetry"] = preserve_telemetry(transport, receipt_dir, state["run_id"])
                save()
                require_success(run)
                latest = transport.api("contents/reports/release/release_manifest.json?ref=main")
                release_bytes = base64.b64decode(latest["content"])
                release = load_json(release_bytes)
                if release.get("panel_vintage") != evidence["month"] or release.get("pipeline_run_id") != str(
                    state["run_id"]
                ):
                    raise ValueError("release no pertenece al mes y a la corrida solicitados")
                state["release_id"] = release["release_id"]
                state["manifest_sha256"] = sha(release_bytes)
                state["consumers"] = wait_consumers(transport, release_bytes, min(seconds, 300))
                state["status"] = "monthly_success_consumers_verified"
                save()
                return operation
            if time.monotonic() >= deadline:
                raise TimeoutError("corrida continúa; conservar run_id, no duplicar dispatch")
            time.sleep(15)
    except ValueError, RuntimeError, OSError, subprocess.SubprocessError, TimeoutError, FetchError:
        state["status"] = "blocked_inspect_recorded_run"
        save()
        raise


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--environment" in argv or "--environment-prepare-only" in argv:
        if argv not in (["--environment"], ["--environment-prepare-only"]):
            raise ValueError("modo environment no admite argumentos superpuestos")
        prepare_only = argv == ["--environment-prepare-only"]
        publish_flag = "0" if prepare_only else os.environ.get("BULLETIN_PUBLISH", "")
        if publish_flag not in {"", "0", "1"}:
            raise ValueError("PUBLISH debe ser 0 o 1")
        argv = ["--month", os.environ.get("BULLETIN_MONTH", "")]
        for name, option in (
            ("BULLETIN_FILE", "--file"),
            ("BULLETIN_RECEIPT", "--receipt"),
            ("BULLETIN_WAIT_SECONDS", "--wait-seconds"),
        ):
            if prepare_only and name == "BULLETIN_RECEIPT":
                continue
            if value := os.environ.get(name):
                argv += [option, value]
        if publish_flag == "1":
            argv += ["--publish"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", required=True)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", args.month):
        raise ValueError("mes YYYY-MM requerido")
    if not 1 <= args.wait_seconds <= 7200:
        raise ValueError("espera debe estar entre 1 y 7200 segundos")
    if args.publish and (not args.receipt or args.file):
        raise ValueError("publicar exige --receipt existente y no admite otro PDF")
    receipt = args.receipt or prepare(args.month, args.file)
    evidence = validate_receipt(receipt, args.month)
    print(publish(evidence, seconds=args.wait_seconds) if args.publish else receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
