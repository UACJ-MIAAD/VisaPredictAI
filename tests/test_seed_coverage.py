"""Contrato de cobertura por semilla: productor y puerta única (`seed_coverage.accredit_group`).

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


def test_seed_problems_detects_missing_column():
    grid = sc.canonical_grid(_level(), holdout=3)
    out = sc.build_output(grid, {"BiTCN": _fc(grid, "BiTCN")}, ["BiTCN"])
    assert sc.seed_problems(out, grid, ["BiTCN", "NHITS"])  # NHITS no esta


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


# ── puerta única (M74-E-R7): cada semilla se recalcula contra la rejilla, no contra sus hermanas ──
REQ_G = ["AutoBiTCN", "AutoTiDE", "AutoNHITS"]


def _rejilla():
    return sc.canonical_grid(_level(), holdout=3)


def _completo():
    out = _rejilla()
    for k, m in enumerate(REQ_G):
        out[m] = out["y"] + 0.5 + k
    return out


def _semilla(camp_dir, seed, marco=None, *, sha="a" * 40, write_csv=True, **mentira):
    """CSV + sidecar HONESTO sobre ese CSV; ``mentira`` altera campos del sidecar."""
    marco = _completo() if marco is None else marco
    texto = marco.to_csv(index=False)
    if write_csv:
        (camp_dir / f"global_FAD_camp_auto_s{seed}.csv").write_text(texto)
    modelos = [c for c in marco.columns if c not in ("unique_id", "ds", "y")]
    side = sc.coverage_sidecar(
        marco,
        modelos,
        campaign={"campaign_id": "c", "source_git_sha": sha},
        table="FAD",
        variant="camp_auto",
        seed=seed,
        csv_sha256="sha256:" + hashlib.sha256(texto.encode("utf-8")).hexdigest(),
    )
    side.update(mentira)
    (camp_dir / f"coverage_FAD_camp_auto_s{seed}.json").write_text(json.dumps(side))


def _write_group(camp_dir):
    for s in range(1, 6):
        _semilla(camp_dir, s)


def _problemas(camp_dir) -> str:
    try:
        sc.accredit_group(
            "FAD", "camp_auto", camp_dir, grid=_rejilla(), required=REQ_G, campaign_id="c", source_git_sha="a" * 40
        )
    except SystemExit as exc:
        return str(exc)
    return ""


def test_seed_group_identical_passes(tmp_path):
    _write_group(tmp_path)
    assert _problemas(tmp_path) == ""


def test_seed_group_missing_sidecar_fails(tmp_path):
    _write_group(tmp_path)
    (tmp_path / "coverage_FAD_camp_auto_s3.json").unlink()
    assert "sin sidecar" in _problemas(tmp_path)


def test_seed_group_different_grid_fails(tmp_path):
    _write_group(tmp_path)
    marco = _completo()
    marco.loc[0, "ds"] = pd.Timestamp("2019-01-01")
    _semilla(tmp_path, 2, marco)
    assert "rejilla" in _problemas(tmp_path)


def test_seed_group_different_truth_fails(tmp_path):
    _write_group(tmp_path)
    marco = _completo()
    marco.loc[0, "y"] += 1.0
    _semilla(tmp_path, 4, marco)
    assert "verdad" in _problemas(tmp_path)


def test_seed_group_partial_nan_fails(tmp_path):
    _write_group(tmp_path)
    marco = _completo()
    marco.loc[2, "AutoBiTCN"] = float("nan")
    _semilla(tmp_path, 5, marco)
    assert "AutoBiTCN: 1 pronostico" in _problemas(tmp_path)


def test_seed_group_wrong_model_inventory_fails(tmp_path):
    _write_group(tmp_path)
    _semilla(tmp_path, 1, _completo().drop(columns=["AutoNHITS"]))
    assert "columnas" in _problemas(tmp_path)


def test_seed_group_wrong_sha_fails(tmp_path):
    _write_group(tmp_path)
    _semilla(tmp_path, 2, sha="b" * 40)
    assert "source_git_sha" in _problemas(tmp_path)


def test_seed_group_sidecar_lying_about_rows_fails(tmp_path):
    _write_group(tmp_path)
    _semilla(tmp_path, 3, n_rows=0)
    assert "n_rows" in _problemas(tmp_path)


def test_seed_group_sidecar_without_csv_fails(tmp_path):
    _write_group(tmp_path)
    (tmp_path / "global_FAD_camp_auto_s2.csv").unlink()
    assert "sin CSV" in _problemas(tmp_path)


def test_seed_group_csv_altered_after_sidecar_fails(tmp_path):
    _write_group(tmp_path)
    marco = _completo()
    marco.loc[1, "AutoTiDE"] += 1.0
    (tmp_path / "global_FAD_camp_auto_s2.csv").write_text(marco.to_csv(index=False))
    assert "csv_sha256" in _problemas(tmp_path)


def test_seed_group_extra_s6_csv_fails(tmp_path):
    _write_group(tmp_path)
    (tmp_path / "global_FAD_camp_auto_s6.csv").write_text("unique_id,ds,y\nx,2020-01-01,1.0\n")
    assert "grupo" in _problemas(tmp_path)


def test_seed_group_model_without_any_forecast_fails(tmp_path):
    _write_group(tmp_path)
    marco = _completo()
    marco["AutoTiDE"] = float("nan")
    _semilla(tmp_path, 4, marco)
    assert "AutoTiDE: 6" in _problemas(tmp_path)
