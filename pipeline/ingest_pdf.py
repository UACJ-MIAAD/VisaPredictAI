"""PDF inbox -> validated local candidate; never S3, Git push or publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from pipeline import scrape_family_visa_bulletins as family
from pipeline import scrape_visa_bulletins as employment
from pipeline.ingest_manual import _completeness_problems, _resolve_month
from pipeline.pdf_bulletin import as_html, extract_text, parse_layout
from vp_data.visa_common import MONTH_MAP, SCRAPER_COUNTRIES, extract_datetime_from_link, parse_tables

ROOT = Path(__file__).resolve().parents[1]


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_once(path: Path, content: bytes) -> None:
    """An archived run cannot be silently edited or reused with different bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"archivo inmutable en conflicto: {path}")
        return
    with path.open("xb") as stream:
        stream.write(content)


def merge_month(old: pd.DataFrame, new: pd.DataFrame, month: str) -> pd.DataFrame:
    """Existing months are immutable by category/table/value, not HTML formatting."""
    selected = pd.to_datetime(old.visa_bulletin_date).dt.strftime("%Y-%m") == month
    if selected.any():
        identity = [c for c in ("EB_level", "F_level", "table_type", "raw_value", "status") if c in old]

        def rows(frame):
            return sorted(tuple(str(v) for v in row) for row in frame[identity].itertuples(index=False, name=None))

        if rows(old[selected]) != rows(new):
            raise ValueError(f"{month}: las celdas difieren de un mes ya congelado; no se sustituye")
        return old
    merged = pd.concat([old, new], ignore_index=True)
    level = "EB_level" if "EB_level" in merged else "F_level"
    return merged.sort_values(["visa_bulletin_date", "table_type", level], ascending=[False, True, True])


def build_candidate(html_bytes: bytes, month: str, stage: Path, log_dir: Path, run_id: str) -> dict:
    """Build away from the checkout. Failure leaves all live outputs untouched."""
    raw = stage / "data/raw"
    shutil.copytree(ROOT / "data/raw", raw)
    (stage / "data/processed").mkdir(parents=True)
    snapshots = ROOT / "data/snapshots"
    historical = pd.read_csv(ROOT / "data/processed/visa_panel_long.csv", keep_default_na=False)
    required = set(historical.bulletin_date.str[:7]) - {month}
    available = {
        parsed.strftime("%Y-%m")
        for path in snapshots.glob("*.html")
        if (parsed := extract_datetime_from_link(path.name)) is not None
    }
    if missing := required - available:
        raise ValueError(
            f"caché de fuentes histórica incompleta: faltan {len(missing)} meses; restaure data/snapshots antes de construir"
        )
    if snapshots.exists():
        shutil.copytree(snapshots, stage / "data/snapshots")
    else:
        (stage / "data/snapshots").mkdir()
    name = next(n for n, number in MONTH_MAP.items() if number == int(month[5:]))
    snapshot = stage / f"data/snapshots/visa-bulletin-for-{name}-{month[:4]}.html"
    # Existing frozen HTML remains byte-identical; the receipt carries the PDF
    # and extracted representation even when its historical snapshot is HTML.
    if not snapshot.exists():
        snapshot.write_bytes(html_bytes)
    soup = BeautifulSoup(html_bytes, "html.parser")
    parsed_month = datetime.strptime(month, "%Y-%m")
    for module, suffix in ((employment, ""), (family, "_family")):
        matcher = module.is_employment_section if module is employment else module.is_family_section
        tables = parse_tables(soup, parsed_month, matcher)
        for country in SCRAPER_COUNTRIES:
            dest = raw / f"{country}{suffix}_visa_backlog_timecourse.csv"
            old = pd.read_csv(dest, keep_default_na=False)
            new = module.extract_country_data(country, tables)
            combined = merge_month(old, new, month)
            if not combined.equals(old):
                combined.to_csv(dest, index=False)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "VP_PIPELINE_RUN_ID": run_id}
    commands = [
        ("panel", [sys.executable, "-m", "pipeline.build_panel"]),
        (
            "database",
            [
                sys.executable,
                "-m",
                "pipeline.build_database",
                "--dvc-lock",
                str(ROOT / "dvc.lock"),
                "--env-lock",
                str(ROOT / "locks/runtime.txt"),
            ],
        ),
        ("news", [sys.executable, "-m", "pipeline.build_bulletins_json"]),
    ]
    for label, argv in commands:
        with (log_dir / f"{label}.log").open("wb") as log:
            subprocess.run(argv, cwd=stage, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    candidate = pd.read_csv(stage / "data/processed/visa_panel_long.csv", keep_default_na=False)
    latest = candidate[candidate.bulletin_date.str.startswith(month)]
    if len(latest) != 150 or latest.duplicated(["country", "block", "category", "table"]).any():
        raise ValueError("panel reconstruido no tiene 150 identidades únicas en el mes")
    key = ["country", "block", "category", "table", "bulletin_date"]
    before = historical[~historical.bulletin_date.str.startswith(month)].sort_values(key).reset_index(drop=True)
    after = candidate[~candidate.bulletin_date.str.startswith(month)].sort_values(key).reset_index(drop=True)
    pd.testing.assert_frame_equal(before, after, check_dtype=False)
    return {
        "panel_rows": len(candidate),
        "month_rows": len(latest),
        "statuses": latest.status.value_counts().to_dict(),
        "historical_rows_preserved": len(before),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, help="PDF; por defecto uno solo en data/inbox/")
    ap.add_argument("--month", required=True, help="YYYY-MM; debe coincidir con título y nombre si contiene mes")
    ap.add_argument("--extract-only", action="store_true", help="valida y archiva, sin reconstruir candidato")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", args.month):
        raise ValueError("--month debe ser YYYY-MM exacto")
    files = [args.file] if args.file else sorted((ROOT / "data/inbox").glob("*.pdf"))
    if len(files) != 1 or not files[0].is_file():
        raise ValueError("deje exactamente un PDF en data/inbox o indique --file")
    pdf = files[0]
    # Reuse the HTML contract after converting only the filename, not contents.
    normalized_name = pdf.name.lower().replace(" ", "-").removesuffix(".pdf") + ".html"
    _resolve_month(Path(normalized_name), args.month)
    source = pdf.read_bytes()
    digest = sha(source)
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"pdf-{args.month}-{digest[:12]}-{now}"
    archive = ROOT / "data/pdf_intake" / args.month / digest / run_id
    archive.mkdir(parents=True)
    receipt = {
        "schema": "visapredict.pdf-intake/1",
        "run_id": run_id,
        "month": args.month,
        "pdf_sha256": digest,
        "source_name": pdf.name,
        "status": "failed",
        "publication": "not_requested",
        "scoring_monthly": "not_executed",
        "candidate_promoted": False,
    }
    try:
        write_once(archive / "source.pdf", source)
        # Parse the archived bytes we just hashed, not a reopened caller path.
        text, version = extract_text(archive / "source.pdf")
        cells = parse_layout(text, args.month)
        body = as_html(cells, args.month, digest)
        soup = BeautifulSoup(body, "html.parser")
        problems = _completeness_problems(soup, datetime.strptime(args.month, "%Y-%m"))
        if problems:
            raise ValueError(f"K3: {problems}")
        write_once(archive / "extracted.txt", text.encode())
        write_once(archive / "cells.json", json.dumps(cells, indent=2).encode() + b"\n")
        write_once(archive / "bulletin.html", body)
        receipt.update(
            {
                "extractor": version,
                "cells": len(cells),
                "matrices": 4,
                "source_git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            }
        )
        if not args.extract_only:
            # Candidate is durable and inspectable; publication remains a
            # distinct governed operation, never an implicit network effect.
            stage = Path(tempfile.mkdtemp(prefix="candidate-", dir=archive))
            receipt["candidate"] = str(stage.relative_to(ROOT))
            receipt.update(build_candidate(body, args.month, stage, archive, run_id))
        receipt["status"] = "validated_extraction" if args.extract_only else "local_candidate_green"
    except (ValueError, OSError, subprocess.SubprocessError, AssertionError, SystemExit) as exc:
        receipt["error"] = str(exc)
        raise
    finally:
        receipt["files"] = {
            str(p.relative_to(archive)): sha(p.read_bytes()) for p in sorted(archive.rglob("*")) if p.is_file()
        }
        write_once(archive / "receipt.json", json.dumps(receipt, indent=2, sort_keys=True).encode() + b"\n")
        print(archive / "receipt.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
