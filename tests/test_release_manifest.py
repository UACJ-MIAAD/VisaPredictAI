"""B1: el manifiesto de release — checksums, criticidad, id determinista, falta crítica aborta."""

from __future__ import annotations

import hashlib
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
