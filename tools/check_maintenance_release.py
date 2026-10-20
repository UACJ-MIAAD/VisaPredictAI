"""Expediente de mantenimiento de metadatos: elegibilidad, nunca autorización ni publicación.

Compara dos commits por sus blobs reales. Sólo tarjeta/manifiesto y la incorporación
nominal del contrato de ingesta pueden cambiar el contenido servido. El resultado
debe someterse a CI remoto del candidato exacto y revisión humana independiente.
No crea recibos /3, no escribe transacciones, no etiqueta ni empuja nada.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from tools import check_contracts as cc

MANIFEST = cc.MANIFEST_PATH
CARD = cc.CARD_PATH
CONTRACT = "vp_data/contracts/ingestion_state.json"
CONTRACT_V1 = {
    "contract_version": 1,
    "artifact": cc.INGESTION_FEED_PATH,
    "kind": "json",
    "required_keys": {
        "schema": "int",
        "status": "str",
        "expected_month": "str",
        "missing_months": "list",
        "status_since": "str",
        "failed_links": "list",
        "reason": "str",
    },
}
MUTABLE = frozenset({MANIFEST, CARD, CONTRACT})
SCIENTIFIC = ("reports/", "data/", "models/", "models.dvc", "mlflow.db.dvc", "dvc.lock", "docs/cohort_deck.json")
CI_JOBS = frozenset({"commit-policy", "lint-and-test", "model-tests", "consistency"})
REVIEW_BINDINGS = (
    "base_commit",
    "base_release_id",
    "candidate_commit",
    "candidate_tree",
    "release_id",
    "source_commit_full",
    "manifest_sha256",
)


class MaintenanceError(ValueError):
    """No es un corte elegible de mantenimiento."""


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "--no-replace-objects", "-C", str(repo), *args], capture_output=True, check=False)
    if result.returncode:
        raise MaintenanceError(f"git {args[0]} no pudo leer el objeto solicitado")
    return result.stdout


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=cc._reject_duplicate_keys)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MaintenanceError(f"JSON inválido: {exc}") from exc
    if not isinstance(value, dict):
        raise MaintenanceError("JSON: se exige un objeto")
    return value


def _tree(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise MaintenanceError("cada commit debe ser un SHA completo de 40 hex")
    rows = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0")
    result = {}
    for row in filter(None, rows):
        meta, path = row.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split()
        if kind != "blob":
            raise MaintenanceError("el candidato contiene una entrada que no es blob")
        result[path.decode("utf-8")] = (mode, oid)
    return result


def _blob(repo: Path, tree: dict[str, tuple[str, str]], path: str) -> bytes:
    if path not in tree or tree[path][0] != "100644":
        raise MaintenanceError(f"falta archivo regular 100644: {path}")
    return _git(repo, "cat-file", "blob", tree[path][1])


def _artifacts(repo: Path, tree: dict[str, tuple[str, str]]) -> tuple[dict, dict[str, dict]]:
    manifest_raw = _blob(repo, tree, MANIFEST)
    manifest = _json(manifest_raw)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise MaintenanceError("inventario vacío o inválido")
    entries: dict[str, dict] = {}
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise MaintenanceError("entrada de inventario inválida")
        path = item["path"]
        if str(PurePosixPath(path)) != path or path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise MaintenanceError(f"ruta de artefacto inválida: {path}")
        if path in entries:
            raise MaintenanceError(f"artefacto duplicado: {path}")
        body = _blob(repo, tree, path)
        if hashlib.sha256(body).hexdigest() != item.get("sha256") or type(item.get("size")) is not int:
            raise MaintenanceError(f"hash o tamaño inválido: {path}")
        if len(body) != item["size"]:
            raise MaintenanceError(f"tamaño distinto: {path}")
        entries[path] = item
    # Reutiliza el gate de identidad completo. Los dos archivos proceden de los mismos blobs ya leídos.
    with tempfile.TemporaryDirectory(prefix="vp-maintenance-identity-") as directory:
        snapshot = Path(directory)
        for path, body in ((MANIFEST, manifest_raw), (CARD, _blob(repo, tree, CARD))):
            target = snapshot / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        with contextlib.redirect_stdout(io.StringIO()):
            problems = cc.release_identity_problems(snapshot)
    if problems:
        raise MaintenanceError("; ".join(problems))
    sha = manifest.get("git_sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{12}", sha):
        raise MaintenanceError("git_sha de origen inválido")
    _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    return manifest, entries


def inspect(repo: Path, base: str, candidate: str) -> dict[str, Any]:
    """Describe la elegibilidad y la identidad exacta; no acepta ni consume permisos."""
    if _git(repo, "rev-parse", "HEAD").decode().strip() != candidate:
        raise MaintenanceError("HEAD no es el candidato inspeccionado")
    if _git(repo, "status", "--porcelain", "--untracked-files=all").strip():
        raise MaintenanceError("el checkout candidato no está limpio")
    return inspect_committed(repo, base, candidate)


def inspect_committed(repo: Path, base: str, candidate: str) -> dict[str, Any]:
    """Sólo objetos de un commit: no certifica el checkout ni concede permisos.

    Permite volver a comprobar el candidato revisado tras un squash con el mismo
    árbol. El consumidor operativo debe comprobar aparte HEAD/limpieza/integración.
    La API `inspect` conserva las guardas de checkout originales sin excepciones.
    """
    before, after = _tree(repo, base), _tree(repo, candidate)
    _git(repo, "merge-base", "--is-ancestor", base, candidate)
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    forbidden = [path for path in changed if path.startswith(SCIENTIFIC) and path not in MUTABLE]
    if forbidden:
        raise MaintenanceError(f"cambio científico o fuera del corte: {forbidden}")
    old, old_entries = _artifacts(repo, before)
    new, new_entries = _artifacts(repo, after)
    if old["release_id"] == new["release_id"] or old.get("panel_vintage") != new.get("panel_vintage"):
        raise MaintenanceError("se exige una identidad nueva de la misma añada")
    if set(old_entries) - set(new_entries) or set(new_entries) - set(old_entries) != {CONTRACT}:
        raise MaintenanceError("sólo se admite incorporar el contrato de ingesta al inventario")
    for path, entry in old_entries.items():
        if path != CARD and new_entries[path] != entry:
            raise MaintenanceError(f"cambia el artefacto publicado o su contrato: {path}")
    if new_entries[CONTRACT].get("criticality") != "required" or new_entries[CARD].get("criticality") != "required":
        raise MaintenanceError("tarjeta y contrato deben ser required")
    contract = _json(_blob(repo, after, CONTRACT))
    if type(contract.get("contract_version")) is not int or contract != CONTRACT_V1:
        raise MaintenanceError("el contrato añadido no es el de ingesta v1")
    # Un squash no conserva la ascendencia de la rama fuente. Se contrasta el
    # contenido servido del origen, no se inventa que ese commit sea ancestro.
    for manifest, entries, tree, strict in ((old, old_entries, before, False), (new, new_entries, after, True)):
        source = _tree(repo, _git(repo, "rev-parse", f"{manifest['git_sha']}^{{commit}}").decode().strip())
        if any(source.get(path) != tree.get(path) for path in entries if path != CARD):
            raise MaintenanceError("el commit de origen no contiene los artefactos del corte")
        if strict and any(
            source.get(path) != tree.get(path) for path in source.keys() | tree.keys() if path not in {CARD, MANIFEST}
        ):
            raise MaintenanceError("el commit de origen no describe el árbol del corte fuera de tarjeta/manifiesto")
    return {
        "schema": "metadata-maintenance-inspection/1",
        "eligible": True,
        "authorizes_publication": False,
        "base_commit": base,
        "base_release_id": old["release_id"],
        "candidate_commit": candidate,
        "candidate_tree": _git(repo, "rev-parse", f"{candidate}^{{tree}}").decode().strip(),
        "release_id": new["release_id"],
        "source_commit": new["git_sha"],
        "source_commit_full": _git(repo, "rev-parse", f"{new['git_sha']}^{{commit}}").decode().strip(),
        "manifest_sha256": hashlib.sha256(_blob(repo, after, MANIFEST)).hexdigest(),
        "changed_paths": changed,
        "required_next": [
            "CI remoto verde del commit candidato",
            "revisión humana de estas identidades",
            "autorización explícita y consumo en el registro de mantenimiento adoptado",
        ],
    }


def _date(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise MaintenanceError("fecha de evidencia inválida")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaintenanceError("fecha de evidencia inválida") from exc
    if date.tzinfo is None or date > datetime.now(UTC):
        raise MaintenanceError("fecha sin zona o futura")
    return date


def check_review_bindings(inspection: dict, review_raw: bytes, ci_raw: bytes) -> dict[str, str]:
    """Coteja declaraciones de revisión/CI, no autentica firmantes ni consulta GitHub.

    Se conservan los hashes de las mismas lecturas. Una captura forjada no se
    convierte en permiso: este expediente nunca autoriza publicar. La adopción
    exige comprobar la captura contra GitHub y la aprobación contra el autor.
    """
    review, ci = _json(review_raw), _json(ci_raw)
    if set(review) != {*REVIEW_BINDINGS, "schema", "decision", "reviewed_by", "reviewed_at", "ci_run_id"}:
        raise MaintenanceError("esquema cerrado de revisión: claves incorrectas")
    if review["schema"] != "metadata-maintenance-review/1" or review["decision"] != "aprobada":
        raise MaintenanceError("revisión no aprobada o esquema inválido")
    if any(review[key] != inspection[key] for key in REVIEW_BINDINGS):
        raise MaintenanceError("la revisión describe otro candidato, base o corte")
    name = review["reviewed_by"]
    # Misma autoridad de identidades automatizadas que el recibo de campaña.
    # Sólo el cotejo opcional de revisión requiere el perfil dev (PyYAML).
    from tools.campaign_txn import _AUTOMATION, _MIN_REVISOR

    if (
        not isinstance(name, str)
        or len(name.strip()) < _MIN_REVISOR
        or len(name.strip().split()) < 2
        or _AUTOMATION.search(name)
    ):
        raise MaintenanceError("revisor declarado vacío o automatizado")
    if set(ci) != {"schema", "repository", "workflow", "head_sha", "tested_tree_sha", "run_id", "completed_at", "jobs"}:
        raise MaintenanceError("esquema cerrado de captura CI: claves incorrectas")
    if (ci["schema"], ci["repository"], ci["workflow"]) != (
        "metadata-maintenance-ci-capture/1",
        "UACJ-MIAAD/VisaPredictAI",
        ".github/workflows/ci.yml",
    ):
        raise MaintenanceError("CI de otro repositorio o workflow")
    if ci["head_sha"] != inspection["candidate_commit"]:
        raise MaintenanceError("CI no corresponde al commit candidato")
    if ci["tested_tree_sha"] != inspection["candidate_tree"]:
        raise MaintenanceError("CI no ensayó el árbol exacto del candidato")
    if (
        type(ci["run_id"]) is not int
        or type(review["ci_run_id"]) is not int
        or ci["run_id"] <= 0
        or review["ci_run_id"] != ci["run_id"]
    ):
        raise MaintenanceError("la revisión no liga la corrida CI")
    jobs = ci["jobs"]
    if not isinstance(jobs, dict) or set(jobs) != CI_JOBS or any(value != "success" for value in jobs.values()):
        raise MaintenanceError("CI incompleto o no verde: se exigen los cuatro jobs")
    if _date(review["reviewed_at"]) <= _date(ci["completed_at"]):
        raise MaintenanceError("la revisión debe ser posterior al CI que declara haber revisado")
    return {
        "review_sha256": hashlib.sha256(review_raw).hexdigest(),
        "ci_capture_sha256": hashlib.sha256(ci_raw).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--review", type=Path, help="revisión declarada; no se emite ni firma aquí")
    parser.add_argument("--ci", type=Path, help="captura de CI que debe contrastarse contra GitHub")
    args = parser.parse_args()
    if (args.review is None) != (args.ci is None):
        parser.error("--review y --ci se cotejan juntos")
    try:
        result = inspect(args.repo, args.base, args.candidate)
        if args.review is not None:
            result["evidence_bindings"] = check_review_bindings(result, args.review.read_bytes(), args.ci.read_bytes())
    except (MaintenanceError, OSError, KeyError, TypeError, UnicodeError, ImportError) as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
