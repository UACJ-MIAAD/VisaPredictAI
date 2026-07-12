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


# --- Ronda 4 de auditoría: las 4 ramas de _sha_unresolvable como tests PERMANENTES ---
# (las reproducciones manuales de la ronda 3 no bloqueaban regresiones futuras).
# Requieren el CLI de git — presente en dev y en todos los runners de CI.

import subprocess  # noqa: E402


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_sha_gate_valid_sha_in_full_clone_passes() -> None:
    head = subprocess.check_output(["git", "rev-parse", "--short=12", "HEAD"], text=True, cwd=cc.ROOT).strip()
    assert cc._sha_unresolvable(cc.ROOT, head) == []


def test_sha_gate_phantom_sha_fails_closed() -> None:
    v = cc._sha_unresolvable(cc.ROOT, "deadbeefdead")
    assert v and "fantasma" in v[0]


def test_sha_gate_shallow_clone_fails_closed(tmp_path) -> None:
    """Un clone shallow ES violación (la rama que el estreno en CI detonó: el bypass
    dejaba pasar un sha fantasma exactamente en los checkouts depth-1 de Actions)."""
    src = tmp_path / "src"
    src.mkdir()
    _run(src, "init", "-q")
    (src / "a.txt").write_text("1")
    _run(src, "add", "a.txt")
    _run(src, "commit", "-qm", "one")
    (src / "a.txt").write_text("2")
    _run(src, "add", "a.txt")
    _run(src, "commit", "-qm", "two")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{src}", str(shallow)],
        check=True,
        capture_output=True,
    )
    v = cc._sha_unresolvable(shallow, "abcdefabcdef")
    assert v and "SHALLOW" in v[0] and "fetch-depth" in v[0]


def test_sha_gate_no_git_repo_fails_closed(tmp_path) -> None:
    v = cc._sha_unresolvable(tmp_path, "abcdefabcdef")
    assert v and "NO VERIFICABLE" in v[0]


def test_champion_contract_required_paths_catch_schema_drift(tmp_path) -> None:
    """Reproduccion EXACTA de R0-03: quitar gate_scope y holdout_winner pasaba el
    contrato nominal (solo exigia FAD/DFF dict). required_paths lo rompe ahora."""
    import shutil

    root = tmp_path / "repo"
    (root / "reports" / "governance").mkdir(parents=True)
    (root / "data" / "processed").mkdir(parents=True)
    shutil.copy(cc.ROOT / "data" / "processed" / "visa_panel_long.csv", root / "data" / "processed")
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    shutil.copy(cc.CONTRACTS_DIR / "champion_challenger.json", cdir)
    art = json.loads((cc.ROOT / "reports" / "governance" / "champion_challenger.json").read_text())
    for t in ("FAD", "DFF"):
        art[t].pop("gate_scope", None)
        art[t].pop("holdout_winner", None)
    (root / "reports" / "governance" / "champion_challenger.json").write_text(json.dumps(art))
    problems = cc.check(root, cdir)
    assert any("gate_scope" in p for p in problems) and any("holdout_winner" in p for p in problems)
    # y el artefacto REAL del repo si cumple el contrato profundo
    assert not [p for p in cc.check() if "champion_challenger" in p]


def test_manifest_missing_listed_artifact_fails_closed(tmp_path) -> None:
    """Reauditoria 2: un artefacto listado en el manifiesto pero BORRADO producia cero
    problemas (el rehash solo corria con ap.exists())."""
    import shutil

    root = tmp_path / "repo"
    (root / "reports" / "release").mkdir(parents=True)
    (root / "data" / "processed").mkdir(parents=True)
    shutil.copy(cc.ROOT / "data" / "processed" / "visa_panel_long.csv", root / "data" / "processed")
    man = json.loads((cc.ROOT / "reports" / "release" / "release_manifest.json").read_text())
    (root / "reports" / "release" / "release_manifest.json").write_text(json.dumps(man))
    cdir = tmp_path / "contracts"
    cdir.mkdir()  # sin contratos de artefactos: aisla el chequeo del manifiesto
    (cdir / "visa_panel_long.csv.json").write_text(
        json.dumps(
            {
                "contract_version": 1,
                "artifact": "data/processed/visa_panel_long.csv",
                "kind": "csv",
                "required_columns": ["country"],
            }
        )
    )
    problems = cc.check(root, cdir)
    assert any("AUSENTE del árbol" in p for p in problems)
