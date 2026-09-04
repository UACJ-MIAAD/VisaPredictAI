"""B1: el manifiesto de release — checksums, criticidad, id determinista, falta crítica aborta."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))

import build_release_manifest as brm  # noqa: E402


def _seed_tree(root: Path, skip: set[str] | None = None) -> None:
    """Árbol mínimo con todos los artefactos no-opcionales del spec."""
    skip = skip or set()
    for rel, crit in brm.artifact_spec():
        if crit == "optional" or rel in skip:
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"contenido:{rel}\n")
    # panel con esquema mínimo para panel_vintage/panel_hash, y JSONs de verdad
    panel = root / "data" / "processed" / "visa_panel_long.csv"
    panel.write_text("bulletin_date,days_since_base\n2026-07-01,100\n")
    if "reports/governance/key_facts.json" not in skip:
        (root / "reports" / "governance" / "key_facts.json").write_text('{"n_obs": 5, "n_months": 1}')
    (root / "reports" / "governance" / "champion_manifest.json").write_text('{"FAD": {"models": ["theta"]}}')


def test_missing_critical_aborts(tmp_path) -> None:
    _seed_tree(tmp_path, skip={"reports/governance/key_facts.json"})
    with pytest.raises(SystemExit, match="critical:reports/governance/key_facts.json"):
        brm.build(tmp_path)


def test_missing_required_aborts(tmp_path) -> None:
    _seed_tree(tmp_path, skip={"reports/fe/fe_facts.json"})
    with pytest.raises(SystemExit, match="required:reports/fe/fe_facts.json"):
        brm.build(tmp_path)


def test_empty_critical_aborts(tmp_path) -> None:
    """D2-A: un critical de 0 bytes es un bloqueante inválido — el productor aborta."""
    _seed_tree(tmp_path)
    (tmp_path / "reports" / "prospective" / "web_forecasts.csv").write_bytes(b"")
    with pytest.raises(SystemExit, match=r"vacíos \(0 bytes\).*critical:reports/prospective/web_forecasts\.csv"):
        brm.build(tmp_path)


def test_empty_required_aborts(tmp_path) -> None:
    """D2-A: un required de 0 bytes también aborta, con su criticidad y ruta en el diagnóstico."""
    _seed_tree(tmp_path)
    (tmp_path / "reports" / "prospective" / "forecast_scorecard_shadow.csv").write_bytes(b"")
    with pytest.raises(
        SystemExit, match=r"vacíos \(0 bytes\).*required:reports/prospective/forecast_scorecard_shadow\.csv"
    ):
        brm.build(tmp_path)


def test_nonempty_blocking_artifacts_are_accepted(tmp_path) -> None:
    """Comportamiento intacto para bloqueantes con contenido: se emite el corte completo."""
    _seed_tree(tmp_path)
    m = brm.build(tmp_path)
    blocking = [e for e in m["artifacts"] if e["criticality"] in ("critical", "required")]
    assert blocking and all(e["size"] > 0 for e in blocking)
    assert m["n_artifacts"] == len(m["artifacts"])


def test_missing_blocking_keeps_previous_diagnostic(tmp_path) -> None:
    """Un archivo AUSENTE sigue abortando por la vía previa (ausentes), no por la nueva."""
    _seed_tree(tmp_path, skip={"reports/prospective/web_forecasts.csv"})
    with pytest.raises(SystemExit, match="ausentes") as exc:
        brm.build(tmp_path)
    assert "vacíos" not in str(exc.value)


def test_empty_optional_keeps_current_behaviour(tmp_path) -> None:
    """D2-A no toca los opcionales: un optional de 0 bytes se incluye y no aborta."""
    _seed_tree(tmp_path)
    pdf = tmp_path / "reports" / "eda" / "eda_report.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"")
    m = brm.build(tmp_path)
    by_path = {e["path"]: e for e in m["artifacts"]}
    assert by_path["reports/eda/eda_report.pdf"]["size"] == 0
    assert "reports/eda/eda_report.pdf" not in m["missing_optional"]


def test_real_manifest_blocking_artifacts_exist_and_are_nonempty() -> None:
    """El corte commiteado cumple el contrato: todo critical/required existe y pesa > 0."""
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "reports" / "release" / "release_manifest.json").read_text())
    blocking = [e for e in manifest["artifacts"] if e["criticality"] in ("critical", "required")]
    assert blocking, "el manifiesto real no declara artefactos bloqueantes"
    zero_in_manifest = [e["path"] for e in blocking if e["size"] == 0]
    absent = [e["path"] for e in blocking if not (root / e["path"]).exists()]
    empty_on_disk = [
        e["path"] for e in blocking if (root / e["path"]).exists() and (root / e["path"]).stat().st_size == 0
    ]
    assert zero_in_manifest == [] and absent == [] and empty_on_disk == []


def test_missing_optional_warns_and_omits(tmp_path) -> None:
    _seed_tree(tmp_path)
    m = brm.build(tmp_path)
    assert "reports/eda/eda_report.pdf" in m["missing_optional"]
    assert all(
        e["criticality"] in ("critical", "required") or e["path"].endswith(".png") is False for e in m["artifacts"]
    )


def test_release_id_is_deterministic_and_content_addressed(tmp_path) -> None:
    _seed_tree(tmp_path)
    a = brm.build(tmp_path)
    b = brm.build(tmp_path)
    assert a["release_id"] == b["release_id"]  # generated_at NO entra al id
    assert a["release_id"].startswith(a["panel_vintage"])
    (tmp_path / "reports" / "governance" / "key_facts.json").write_text("{}")
    c = brm.build(tmp_path)
    assert c["release_id"] != a["release_id"]  # un byte distinto ⇒ release distinto


def test_sha256_and_mime_are_real(tmp_path) -> None:
    _seed_tree(tmp_path)
    m = brm.build(tmp_path)
    by_path = {e["path"]: e for e in m["artifacts"]}
    kf = by_path["reports/governance/key_facts.json"]
    raw = (tmp_path / kf["path"]).read_bytes()
    assert kf["sha256"] == hashlib.sha256(raw).hexdigest()
    assert kf["size"] == len(raw)
    assert kf["mime"] == "application/json"
    assert by_path["data/processed/visa_panel_long.csv"]["mime"] == "text/csv"


def test_spec_covers_web_consumption_contract() -> None:
    """El spec cubre los 11 base + 72 PNG (4 variantes × 11 EDA + 7 FE) que baja el web."""
    spec = dict(brm.artifact_spec())
    assert spec["data/processed/visa_panel_long.csv"] == "critical"
    assert spec["reports/prospective/web_forecasts.csv"] == "critical"
    assert spec["reports/governance/key_facts.json"] == "critical"
    pngs = [p for p in spec if p.endswith(".png")]
    assert len(pngs) == (11 + 7) * 4
    assert all(spec[p] == "optional" for p in pngs)
