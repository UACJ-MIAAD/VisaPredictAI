"""Manifiesto de release verificable (B1, plan auditoría 2026-07-11).

Emite ``reports/release/release_manifest.json``: la lista COMPLETA de artefactos que
constituyen un corte publicable (panel, forecasts, scorecards, facts, galerías, PDFs,
gobernanza), cada uno con SHA-256, tamaño, MIME y criticidad, bajo un ``release_id``
determinista derivado del contenido (mismos bytes ⇒ mismo id; ``generated_at`` NO entra
al id). El consumidor (B2, loader del web) descarga el manifiesto, verifica cada hash y
hace swap atómico: o toma el corte completo o se queda con el anterior — nunca una
mezcla de añadas.

Criticidad (contrato de dos lados):
- ``critical``/``required``: el PRODUCTOR aborta si faltan (un corte sin ellos es un
  pipeline roto — falta crítica aborta, aceptación B1). Para el CONSUMIDOR:
  ``critical`` bloquea el deploy; ``required`` permite servir con insignia de stale.
- ``optional``: se omite con aviso (cosmético: PDFs/galerías); el consumidor degrada.

La lista espejea el consumo real del web (``scripts/fetch-data.mjs`` del repo hermano:
11 archivos base + 72 PNG de galería) más la gobernanza que el RAG indexa.

Corre en ``ante`` desde la raíz:  ante/bin/python experiments/build_release_manifest.py
(en el cron: paso 4h, tras EDA/FE y antes del commit del bloque de modelado)
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vp_model import config, ledger  # noqa: E402

log = config.get_logger("release_manifest")

OUT = ROOT / "reports" / "release" / "release_manifest.json"
SCHEMA_VERSION = 1

MIME = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".png": "image/png",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
}

_EDA_FIGS = [
    "g01_panel", "g02_trayectorias", "g03_backlog", "g04_retros", "g05_brecha",
    "g06_pulso_fiscal", "g07_leadlag", "g08_congelados", "g09_estacionariedad",
    "g10_dv", "g11_completitud",
]  # fmt: skip
_FE_FIGS = [
    "f01_differencing", "f02_calendar", "f03_importance", "f04_gaps",
    "f05_regime", "f06_parser", "f07_pipeline",
]  # fmt: skip
_VARIANTS = ["", "dark/", "en/", "en/dark/"]


def artifact_spec(root: Path = ROOT) -> list[tuple[str, str]]:
    """(path repo-relativo, criticidad) — la definición del corte publicable."""
    spec: list[tuple[str, str]] = [
        ("data/processed/visa_panel_long.csv", "critical"),
        ("data/processed/bulletins.json", "required"),
        ("reports/prospective/web_forecasts.csv", "critical"),
        ("reports/prospective/web_forecasts_meta.json", "critical"),
        ("reports/prospective/forecast_scorecard_meta.json", "critical"),
        ("reports/prospective/forecast_scorecard.csv", "required"),
        ("reports/prospective/forecast_scorecard_shadow.csv", "required"),
        ("reports/prospective/forecast_scorecard_shadow_meta.json", "required"),
        ("reports/prospective/forecast_log.csv", "required"),
        ("reports/prospective/forecast_log_shadow.csv", "required"),
        ("reports/prospective/pi_scale_by_h.json", "required"),
        ("reports/prospective/prospective_head_to_head.json", "required"),
        ("reports/eda/eda_facts.json", "critical"),
        ("reports/eda/eda_report.pdf", "optional"),
        ("reports/eda/en/eda_report.pdf", "optional"),
        ("reports/fe/fe_facts.json", "required"),
        ("reports/fe/fe_report.pdf", "optional"),
        ("reports/fe/en/fe_report.pdf", "optional"),
        ("reports/governance/key_facts.json", "critical"),
        ("reports/governance/champion_manifest.json", "critical"),
        ("reports/governance/champion_challenger.json", "required"),
        ("reports/governance/completeness_allowlist.json", "required"),
        ("reports/governance/promotion_decision.json", "required"),
        ("reports/governance/MODEL_CARD.md", "required"),
        ("reports/governance/mega_audit_report.md", "optional"),
    ]
    # B3: los contratos viajan EN el release — el loader del web compara su hash contra
    # su copia vendorizada (lib/contracts/) y trata la deriva como corte incompatible.
    for c in sorted((root / "vp_data" / "contracts").glob("*.json")):
        spec.append((f"vp_data/contracts/{c.name}", "required"))
    for base, figs in (("reports/eda/gallery", _EDA_FIGS), ("reports/fe/gallery", _FE_FIGS)):
        for fig in figs:
            for sub in _VARIANTS:
                spec.append((f"{base}/{sub}{fig}.png", "optional"))
    return spec


def _entry(root: Path, rel: str, criticality: str) -> dict | None:
    p = root / rel
    if not p.exists():
        return None
    data = p.read_bytes()
    return {
        "path": rel,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mime": MIME.get(p.suffix, "application/octet-stream"),
        "criticality": criticality,
    }


def build(root: Path = ROOT) -> dict:
    """Construye el manifiesto; aborta (SystemExit) si falta un artefacto no-opcional."""
    entries: list[dict] = []
    missing_blocking: list[str] = []
    missing_optional: list[str] = []
    for rel, crit in artifact_spec(root):
        e = _entry(root, rel, crit)
        if e is None:
            (missing_optional if crit == "optional" else missing_blocking).append(f"{crit}:{rel}")
            continue
        entries.append(e)
    if missing_blocking:
        raise SystemExit(f"ABORT release manifest — artefactos bloqueantes ausentes: {missing_blocking}")
    for m in missing_optional:
        log.warning("artefacto opcional ausente (omitido del corte): %s", m)

    vintage = ledger.panel_vintage(root / "data" / "processed" / "visa_panel_long.csv")
    # release_id determinista: SOLO el contenido define el id (generated_at fuera).
    content = json.dumps([(e["path"], e["sha256"]) for e in entries], sort_keys=True).encode()
    release_id = f"{vintage}-{hashlib.sha256(content).hexdigest()[:12]}"

    def _json(rel: str) -> dict:
        # Degradación C1: un JSON ausente o corrupto no tumba el manifiesto — los campos
        # informativos quedan vacíos (los checksums de arriba ya garantizan la integridad).
        p = root / rel
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except json.JSONDecodeError:
            log.warning("%s ilegible como JSON — campos informativos vacíos", rel)
            return {}

    kf = _json("reports/governance/key_facts.json")
    champions = _json("reports/governance/champion_manifest.json")

    from vp_data.tracking import pipeline_run_id  # C3: enlaza el corte con el run que lo produjo

    return {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "pipeline_run_id": pipeline_run_id(),
        # Auditoria 11-jul: git_sha SIEMPRE resoluble a un commit (sin sufijo -dirty --
        # el cron genera con el arbol sucio por diseno: los artefactos del run aun no se
        # commitean). La suciedad se registra aparte como worktree_dirty; un manifiesto
        # commiteado con worktree_dirty=true por CODIGO local sucio es rastreable aqui.
        "git_sha": ledger.git_sha().removesuffix("-dirty"),
        "worktree_dirty": ledger.git_sha().endswith("-dirty"),
        "panel_vintage": vintage,
        "panel_hash_md5_12": ledger.panel_hash(root / "data" / "processed" / "visa_panel_long.csv"),
        "champion_recipes": {t: r.get("models") for t, r in champions.items()} if champions else {},
        "counts": {k: kf[k] for k in ("n_obs", "n_series_structural", "n_series_evaluable", "n_months") if k in kf},
        "n_artifacts": len(entries),
        "missing_optional": [m.split(":", 1)[1] for m in missing_optional],
        "artifacts": entries,
    }


def main() -> int:
    manifest = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    if manifest.get("worktree_dirty"):
        # Esperado en el cron (artefactos del run sin commitear); en escritorio significa
        # codigo sin commitear mezclado -- regenerar tras commitear da worktree_dirty=false
        # y el MISMO release_id (content-addressed).
        log.warning(
            "worktree_dirty=true: arbol sucio al generar (normal en el cron; en local, regenera tras commitear)"
        )
    log.info(
        "release %s · vintage %s · %d artefactos (%d opcionales ausentes) → %s",
        manifest["release_id"],
        manifest["panel_vintage"],
        manifest["n_artifacts"],
        len(manifest["missing_optional"]),
        OUT,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
