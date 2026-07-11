"""B3: contratos cross-repo — columnas/llaves requeridas y corte de añada única."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_contracts as cc  # noqa: E402


def _mini(tmp_path: Path, panel_vintage: str = "2026-07", eda_vintage: str = "2026-07") -> tuple[Path, Path]:
    """Árbol mínimo (panel + eda_facts) con contratos propios, parametrizable por añada."""
    root = tmp_path / "repo"
    cdir = tmp_path / "contracts"
    cdir.mkdir(parents=True)
    (cdir / "visa_panel_long.csv.json").write_text(
        json.dumps(
            {
                "contract_version": 1,
                "artifact": "data/processed/visa_panel_long.csv",
                "kind": "csv",
                "required_columns": ["country", "bulletin_date", "days_since_base"],
            }
        )
    )
    (cdir / "eda_facts.json").write_text(
        json.dumps(
            {
                "contract_version": 1,
                "artifact": "reports/eda/eda_facts.json",
                "kind": "json",
                "vintage_key": "vintage",
                "required_keys": {"vintage": "str", "panel": "dict"},
            }
        )
    )
    panel = root / "data" / "processed" / "visa_panel_long.csv"
    panel.parent.mkdir(parents=True)
    panel.write_text(f"country,bulletin_date,days_since_base\nmexico,{panel_vintage}-01,100\n")
    eda = root / "reports" / "eda" / "eda_facts.json"
    eda.parent.mkdir(parents=True)
    eda.write_text(json.dumps({"vintage": eda_vintage, "panel": {"n_obs": 1}}))
    return root, cdir


def test_clean_tree_passes(tmp_path) -> None:
    root, cdir = _mini(tmp_path)
    assert cc.check(root, cdir) == []


def test_missing_csv_column_fails(tmp_path) -> None:
    root, cdir = _mini(tmp_path)
    (root / "data" / "processed" / "visa_panel_long.csv").write_text("country,bulletin_date\nmexico,2026-07-01\n")
    problems = cc.check(root, cdir)
    assert any("days_since_base" in p for p in problems)


def test_missing_key_and_wrong_type_fail(tmp_path) -> None:
    root, cdir = _mini(tmp_path)
    eda = root / "reports" / "eda" / "eda_facts.json"
    eda.write_text(json.dumps({"vintage": "2026-07", "panel": []}))  # panel: list, no dict
    problems = cc.check(root, cdir)
    assert any("'panel' debería ser dict" in p for p in problems)
    eda.write_text(json.dumps({"panel": {}}))  # sin vintage
    assert any("'vintage'" in p for p in cc.check(root, cdir))


def test_mixed_vintage_cut_fails(tmp_path) -> None:
    root, cdir = _mini(tmp_path, panel_vintage="2026-07", eda_vintage="2026-06")
    problems = cc.check(root, cdir)
    assert any("AÑADAS MEZCLADAS" in p for p in problems)


def test_missing_artifact_fails(tmp_path) -> None:
    root, cdir = _mini(tmp_path)
    (root / "reports" / "eda" / "eda_facts.json").unlink()
    assert any("ausente" in p for p in cc.check(root, cdir))


def test_real_repo_passes() -> None:
    """Integración: los 13 artefactos reales cumplen sus contratos con añada única."""
    assert cc.check() == []
