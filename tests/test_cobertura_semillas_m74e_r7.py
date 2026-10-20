"""M74-E-R7 · la acreditación de semillas acredita la COBERTURA, no sólo la identidad.

Auditoría `a23c878e…`: R6 ligaba cada CSV a su campaña y a sus bytes y aceptaba igual una semilla
con meses distintos o cinco semillas truncadas a 2 filas, porque comparaba semillas entre sí y
nunca contra lo que el panel exige. Aquí la rejilla la deriva el LECTOR del panel, y los atacantes
escriben sidecars HONESTOS, calculados sobre sus propios CSV: es lo que haría un productor roto.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "experiments"))
sys.path.insert(0, str(RAIZ))

import aggregate_seeds as ag  # noqa: E402
import run_global_deep as rgd  # noqa: E402
import seed_coverage as sc  # noqa: E402

from tools.check_campaign_completeness import SEED_MODELS  # noqa: E402

CID, SHA, VAR = "camp_actual", "b" * 40, "camp_diff"
MODELOS = list(SEED_MODELS[VAR])
CAMPANA = {"campaign_id": CID, "source_git_sha": SHA}


def _rejilla(nivel: pd.DataFrame) -> pd.DataFrame:
    return sc.canonical_grid(nivel, 24)


def _completo(rejilla: pd.DataFrame, semilla: int) -> pd.DataFrame:
    out = rejilla.copy()
    for k, m in enumerate(MODELOS):
        out[m] = out["y"] + 0.5 + k + semilla / 10
    return out


def _escribir(camp: Path, marco: pd.DataFrame, semilla: int, **mentira) -> None:
    """CSV + sidecar HONESTO sobre ese CSV; ``mentira`` altera campos del sidecar después."""
    camp.mkdir(parents=True, exist_ok=True)
    csv = camp / f"global_FAD_{VAR}_s{semilla}.csv"
    marco.to_csv(csv, index=False)
    modelos = [c for c in marco.columns if c not in ("unique_id", "ds", "y")]
    side = sc.coverage_sidecar(
        marco,
        modelos,
        campaign=CAMPANA,
        table="FAD",
        variant=VAR,
        seed=semilla,
        csv_sha256="sha256:" + hashlib.sha256(csv.read_bytes()).hexdigest(),
    )
    side.update(mentira)
    (camp / f"coverage_FAD_{VAR}_s{semilla}.json").write_text(json.dumps(side, sort_keys=True), encoding="utf-8")


def _acreditar(camp: Path) -> None:
    ag.acreditar_grupo("FAD", f"{VAR}_s", camp, campaign_id=CID, source_git_sha=SHA)


# ═══════════════════════════════ control
def test_control_seiscientas_claves_exactas_se_acreditan(tmp_path: Path, panel_semillas) -> None:
    rejilla = _rejilla(panel_semillas)
    assert len(rejilla) == 600 and rejilla.groupby("unique_id").size().eq(24).all()
    for s in range(1, 6):
        _escribir(tmp_path, _completo(rejilla, s), s)
    _acreditar(tmp_path)


# ═══════════════════════════════ los dos ataques de la auditoría
def test_RED_una_semilla_con_un_mes_distinto(tmp_path: Path, panel_semillas) -> None:
    """Ataque A: s3 trae otra ventana para una serie, y su sidecar es coherente con ella."""
    rejilla = _rejilla(panel_semillas)
    for s in range(1, 6):
        marco = _completo(rejilla, s)
        if s == 3:
            i = marco.index[0]
            marco.loc[i, "ds"] = marco.loc[i, "ds"] - pd.DateOffset(months=24)
        _escribir(tmp_path, marco, s)
    with pytest.raises(SystemExit, match="rejilla: faltan 1 y sobran 1"):
        _acreditar(tmp_path)


def test_RED_las_cinco_semillas_truncadas_igual(tmp_path: Path, panel_semillas) -> None:
    """★ Ataque B: 2 filas por semilla en vez de 600, idénticas en las cinco."""
    rejilla = _rejilla(panel_semillas)
    for s in range(1, 6):
        _escribir(tmp_path, _completo(rejilla, 1).head(2), s)
    with pytest.raises(SystemExit, match="faltan 598"):
        _acreditar(tmp_path)


# ═══════════════════════════════ el resto del catálogo exigido
def test_RED_un_duplicado_que_compensa_un_faltante(tmp_path: Path, panel_semillas) -> None:
    rejilla = _rejilla(panel_semillas)
    for s in range(1, 6):
        marco = _completo(rejilla, s)
        marco.iloc[1] = marco.iloc[0]  # 600 filas: la clave 0 dos veces y la 1 ninguna
        _escribir(tmp_path, marco, s)
    with pytest.raises(SystemExit, match="duplicada"):
        _acreditar(tmp_path)


@pytest.mark.parametrize("campo", ["grid_sha256", "truth_sha256", "models"])
def test_RED_un_sidecar_que_miente_sobre_un_csv_correcto(tmp_path: Path, panel_semillas, campo: str) -> None:
    rejilla = _rejilla(panel_semillas)
    for s in range(1, 6):
        _escribir(tmp_path, _completo(rejilla, s), s)
    falso = (
        {m: {"finite_rows": 600, "finite_mask_sha256": "0" * 64} for m in MODELOS} if campo == "models" else "0" * 64
    )
    _escribir(tmp_path, _completo(rejilla, 2), 2, **{campo: falso})
    with pytest.raises(SystemExit, match=campo):
        _acreditar(tmp_path)


def test_RED_el_mismo_nan_parcial_en_las_cinco(tmp_path: Path, panel_semillas) -> None:
    """Iguales entre sí y aun así inutilizables: `nanmean` los promediaba sobre 599 meses."""
    rejilla = _rejilla(panel_semillas)
    for s in range(1, 6):
        marco = _completo(rejilla, s)
        marco.loc[marco.index[7], "TiDE"] = np.nan
        _escribir(tmp_path, marco, s)
    with pytest.raises(SystemExit, match="TiDE: 1 pronostico"):
        _acreditar(tmp_path)


def test_RED_una_verdad_que_no_es_la_del_panel(tmp_path: Path, panel_semillas) -> None:
    rejilla = _rejilla(panel_semillas)
    for s in range(1, 6):
        marco = _completo(rejilla, s)
        marco.loc[marco.index[0], "y"] += 1.0
        _escribir(tmp_path, marco, s)
    with pytest.raises(SystemExit, match="verdad del panel"):
        _acreditar(tmp_path)


def test_RED_falta_un_modelo_del_inventario(tmp_path: Path, panel_semillas) -> None:
    rejilla = _rejilla(panel_semillas)
    for s in range(1, 6):
        _escribir(tmp_path, _completo(rejilla, s).drop(columns=["PatchTST"]), s)
    with pytest.raises(SystemExit, match="columnas"):
        _acreditar(tmp_path)


# ═══════════════════════════════ el productor parte de la rejilla, no de lo que sobrevivió
def _sellar(tmp_path: Path, nivel: pd.DataFrame, pronosticos: pd.DataFrame, semilla: int) -> pd.DataFrame:
    merged = nivel.merge(pronosticos.drop(columns=["y"]), on=["unique_id", "ds"], how="left")
    out = tmp_path / f"global_FAD_{VAR}_s{semilla}.csv"
    return rgd._sellar_semilla(merged, nivel, out, "FAD", rgd._SEMILLA_RE.match(f"{VAR}_s{semilla}"), campaign=CAMPANA)


def test_el_productor_escribe_la_rejilla_entera_y_el_lector_la_acredita(tmp_path: Path, panel_semillas) -> None:
    rejilla = _rejilla(panel_semillas)
    for s in range(1, 6):
        assert len(_sellar(tmp_path, panel_semillas, _completo(rejilla, s), s)) == 600
    _acreditar(tmp_path)


def test_RED_el_productor_no_sella_lo_que_sobrevivio(tmp_path: Path, panel_semillas) -> None:
    """R6 borraba las filas sin pronóstico y sellaba el resto: 2 filas quedaban acreditadas."""
    rejilla = _rejilla(panel_semillas)
    with pytest.raises(SystemExit, match="no finito"):
        _sellar(tmp_path, panel_semillas, _completo(rejilla, 1).head(2), 1)
    assert not list(tmp_path.glob("global_*")) and not list(tmp_path.glob("coverage_*"))


# ═══════════════════════════════ una sola autoridad, y el evaluador no promedia ausencias
def test_una_sola_puerta_de_cobertura() -> None:
    import tools.check_campaign_completeness as gate

    assert not hasattr(gate, "validate_seed_group"), "dos autoridades de cobertura acaban divergiendo"
    llamadores = sorted(
        str(p.relative_to(RAIZ))
        for d in ("experiments", "tools", "vp_model", "pipeline")
        for p in (RAIZ / d).rglob("*.py")
        if any(
            isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None)) == "accredit_group"
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
        )
    )
    assert llamadores == ["experiments/aggregate_seeds.py"]


def test_RED_el_evaluador_no_convierte_una_ausencia_en_metrica(tmp_path: Path, monkeypatch) -> None:
    from vp_model import dataset
    from vp_model import eval_neuralforecast as ev

    idx = pd.date_range("2018-01-01", periods=48, freq="MS")
    serie = pd.Series(np.arange(48, dtype="float64") * 30.0 + 9000.0, index=idx)
    monkeypatch.setattr(dataset, "load_series", lambda *a, **k: serie)
    monkeypatch.setattr(ev, "REPORTS", tmp_path)
    (tmp_path / "campaign").mkdir()
    cola = serie.iloc[-6:]
    pd.DataFrame(
        {
            "unique_id": "mexico/family/F1",
            "ds": cola.index,
            "y": cola.to_numpy(),
            "NHITS": [*(cola.to_numpy()[:-1] + 5.0), np.nan],
        }
    ).to_csv(tmp_path / "campaign" / "global_FAD_x.csv", index=False)
    fila = ev.eval_global_deep("FAD").iloc[0]
    assert not np.isfinite(fila["hold_mase"]), "un mes sin pronóstico no puede quedar fuera del promedio"
