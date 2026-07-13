"""Contrato de cobertura por semilla: productor (seed_coverage) + gate (validate_seed_group).

Auditoria 13-jul-2026 ronda 9, paso 3-4. Ejecuta el helper del productor sobre frames
sinteticos y el gate sobre sidecars sinteticos: grilla canonica, salida sin borrar filas,
escritura atomica, y cobertura IDENTICA entre las 5 semillas (grid/truth/finite-mask).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib

import pandas as pd

import tools.check_campaign_completeness as gate

_P = pathlib.Path(__file__).resolve().parent.parent / "experiments" / "seed_coverage.py"
_spec = importlib.util.spec_from_file_location("seed_coverage_ut", _P)
assert _spec and _spec.loader
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

CAMP = {"campaign_id": "rederiv_x", "source_git_sha": "a" * 40}
REQUIRED = ["BiTCN", "NHITS"]


def _level():
    ds = list(pd.date_range("2020-01-01", periods=5, freq="MS"))
    return pd.DataFrame(
        {
            "unique_id": ["mx/family/F1"] * 5 + ["in/family/F4"] * 5,
            "ds": ds + ds,
            "y": [float(i) for i in range(10)],
        }
    )


def _fc(grid, model, offset=0.5):
    return pd.DataFrame({"unique_id": grid["unique_id"], "ds": grid["ds"], model: grid["y"] + offset})


# ── productor ──
def test_canonical_grid_is_last_holdout_per_series():
    grid = sc.canonical_grid(_level(), holdout=3)
    assert len(grid) == 6 and grid["unique_id"].nunique() == 2


def test_build_output_keeps_all_rows_failed_model_is_nan():
    grid = sc.canonical_grid(_level(), holdout=3)
    out = sc.build_output(grid, {"BiTCN": _fc(grid, "BiTCN")}, REQUIRED)  # NHITS ausente
    assert len(out) == len(grid)  # ninguna fila borrada
    assert out["BiTCN"].notna().all()
    assert out["NHITS"].isna().all()  # modelo fallido -> columna toda-NaN


def test_validate_output_detects_missing_column():
    grid = sc.canonical_grid(_level(), holdout=3)
    out = sc.build_output(grid, {"BiTCN": _fc(grid, "BiTCN")}, ["BiTCN"])
    assert sc.validate_output(out, grid, ["BiTCN", "NHITS"])  # NHITS no esta


def test_coverage_sidecar_counts_finite_and_hashes():
    grid = sc.canonical_grid(_level(), holdout=3)
    out = sc.build_output(grid, {"BiTCN": _fc(grid, "BiTCN")}, REQUIRED)
    sd = sc.coverage_sidecar(
        out, REQUIRED, campaign=CAMP, table="FAD", variant="camp_diff", seed=1, csv_sha256="sha256:" + "0" * 64
    )
    assert sd["n_rows"] == 6 and sd["n_series"] == 2
    assert sd["models"]["BiTCN"]["finite_rows"] == 6
    assert sd["models"]["NHITS"]["finite_rows"] == 0
    assert sd["grid_sha256"] and sd["truth_sha256"] and sd["csv_sha256"].startswith("sha256:")


def test_finalize_seed_writes_atomically(tmp_path):
    grid = sc.canonical_grid(_level(), holdout=3)
    out_csv = tmp_path / "global_FAD_camp_diff_s1.csv"
    side = tmp_path / "coverage_FAD_camp_diff_s1.json"
    sd = sc.finalize_seed(
        grid,
        {"BiTCN": _fc(grid, "BiTCN"), "NHITS": _fc(grid, "NHITS")},
        REQUIRED,
        out_path=out_csv,
        sidecar_path=side,
        campaign=CAMP,
        table="FAD",
        variant="camp_diff",
        seed=1,
    )
    assert out_csv.exists() and side.exists()
    assert not list(tmp_path.glob(".seed.*.tmp"))  # sin residuo temporal
    assert json.loads(side.read_text())["grid_sha256"] == sd["grid_sha256"]
    # csv_sha256 liga el sidecar al CSV por bytes (el gate lo recalcula asi)
    assert sd["csv_sha256"] == "sha256:" + hashlib.sha256(out_csv.read_bytes()).hexdigest()


# ── gate: cobertura identica y VERIFICABLE entre semillas (ronda 10) ──
def _bundle(
    camp_dir,
    variant,
    seed,
    *,
    grid="a" * 64,
    truth="b" * 64,
    masks=None,
    models=("AutoBiTCN", "AutoTiDE", "AutoNHITS"),
    sha="a" * 40,
    n_rows=6,
    n_series=2,
    finite=6,
    csv_content="unique_id,ds,y\nx,2020-01-01,1.0\n",
    write_csv=True,
):
    """Escribe el CSV real + su sidecar con csv_sha256 ligado por bytes."""
    if write_csv:
        (camp_dir / f"global_FAD_{variant}_s{seed}.csv").write_text(csv_content)
    csv_sha = "sha256:" + hashlib.sha256(csv_content.encode("utf-8")).hexdigest()
    m = {name: {"finite_rows": finite, "finite_mask_sha256": (masks or {}).get(name, "c" * 64)} for name in models}
    (camp_dir / f"coverage_FAD_{variant}_s{seed}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": "c",
                "source_git_sha": sha,
                "table": "FAD",
                "variant": variant,
                "seed": seed,
                "csv_sha256": csv_sha,
                "grid_sha256": grid,
                "truth_sha256": truth,
                "n_rows": n_rows,
                "n_series": n_series,
                "models": m,
            }
        )
    )


def _write_group(camp_dir, variant="camp_auto"):
    for s in range(1, 6):
        _bundle(camp_dir, variant, s)


def test_seed_group_identical_passes(tmp_path):
    _write_group(tmp_path)
    assert gate.validate_seed_group("FAD", "camp_auto", tmp_path) == []


def test_seed_group_missing_sidecar_fails(tmp_path):
    _write_group(tmp_path)
    (tmp_path / "coverage_FAD_camp_auto_s3.json").unlink()
    assert any("falta cobertura" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))


def test_seed_group_different_grid_fails(tmp_path):
    _write_group(tmp_path)
    _bundle(tmp_path, "camp_auto", 2, grid="f" * 64)
    assert any("grid_sha256 DIFIERE" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))


def test_seed_group_different_truth_fails(tmp_path):
    _write_group(tmp_path)
    _bundle(tmp_path, "camp_auto", 4, truth="f" * 64)
    assert any("truth_sha256 DIFIERE" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))


def test_seed_group_different_finite_mask_fails(tmp_path):
    _write_group(tmp_path)
    _bundle(tmp_path, "camp_auto", 5, masks={"AutoBiTCN": "d" * 64})
    assert any("finite-mask" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))


def test_seed_group_wrong_model_inventory_fails(tmp_path):
    _write_group(tmp_path)
    _bundle(tmp_path, "camp_auto", 1, models=("AutoBiTCN", "AutoTiDE"))  # falta AutoNHITS
    assert any("modelos" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))


def test_seed_group_wrong_sha_fails(tmp_path):
    _write_group(tmp_path)
    _bundle(tmp_path, "camp_auto", 2, sha="b" * 40)
    assert any(
        "source_git_sha" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path, sealed_sha="a" * 40)
    )


# ── P2 ronda 10: sidecar vacio / sin CSV / CSV alterado / s6 extra / degenerado ──
def test_seed_group_empty_sidecar_fails(tmp_path):
    _write_group(tmp_path)
    _bundle(tmp_path, "camp_auto", 3, n_rows=0)  # sidecar "vacio"
    assert any("n_rows" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))


def test_seed_group_sidecar_without_csv_fails(tmp_path):
    _write_group(tmp_path)
    _bundle(tmp_path, "camp_auto", 2, write_csv=False)
    (tmp_path / "global_FAD_camp_auto_s2.csv").unlink()
    assert any("sin CSV" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))


def test_seed_group_csv_altered_after_sidecar_fails(tmp_path):
    _write_group(tmp_path)
    (tmp_path / "global_FAD_camp_auto_s2.csv").write_text("unique_id,ds,y\nx,2020-01-01,999.0\n")  # alterado
    assert any("csv_sha256" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))


def test_seed_group_extra_s6_csv_fails(tmp_path):
    _write_group(tmp_path)
    (tmp_path / "global_FAD_camp_auto_s6.csv").write_text("unique_id,ds,y\nx,2020-01-01,1.0\n")
    assert any("extra" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))


def test_seed_group_all_finite_zero_fails(tmp_path):
    _write_group(tmp_path)
    _bundle(tmp_path, "camp_auto", 4, finite=0)  # cobertura vacia
    assert any("finite_rows=0" in p for p in gate.validate_seed_group("FAD", "camp_auto", tmp_path))
