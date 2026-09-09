"""E1 · Las cohortes de estabilidad no pueden mirar el hold-out.

La prueba que manda es la metamórfica: si mutar ENTERA la ventana de evaluación cambia una
sola feature o una sola etiqueta, la partición está contaminada y cualquier comparación
posterior por cohorte sería una profecía autocumplida. Las demás fijan el determinismo, el
esquema cerrado, la igualdad JSON↔CSV, la monotonía alrededor de los dos umbrales y la
población, esta última CRUZADA contra `key_facts.json` (otra derivación del mismo panel),
nunca tecleada.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vp_model import stability
from vp_model.config import HOLDOUT

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.parquet"
PANEL_CSV = ROOT / "data" / "processed" / "visa_panel_long.csv"  # versionado: existe siempre
KEY_FACTS = ROOT / "reports" / "governance" / "key_facts.json"


# ------------------------------------------------------------------ material sintético
def serie(valores: list[float], inicio: str = "2005-01-01") -> pd.Series:
    idx = pd.date_range(inicio, periods=len(valores), freq="MS")
    return pd.Series(np.asarray(valores, dtype="float64"), index=idx)


def rampa(n: int = 120, paso: float = 25.0, retros: tuple[int, ...] = ()) -> pd.Series:
    v = [1000.0]
    for i in range(1, n):
        v.append(v[-1] - 900.0 if i in retros else v[-1] + paso)
    return serie(v)


def casi_congelada(n: int = 141) -> pd.Series:
    """Quieta pero no constante: paso mediano 0 con avances ocasionales."""
    v = [500.0]
    for i in range(1, n):
        v.append(v[-1] + (30.0 if i % 7 == 0 else 0.0))
    return serie(v)


def panel_sintetico(series: dict[tuple[str, str, str, str], pd.Series]) -> pd.DataFrame:
    filas = []
    for (país, bloque, categoría, tabla), s in series.items():
        for fecha, valor in s.items():
            filas.append(
                {
                    "country": país,
                    "block": bloque,
                    "category": categoría,
                    "table": tabla,
                    "bulletin_date": fecha,
                    "status": "F",
                    "priority_date": None,
                    "days_since_base": float(valor),
                    "raw_value": str(valor),
                }
            )
    return pd.DataFrame(filas)


# --------------------------------------------------------------------- causalidad (★)
def test_mutar_todo_el_holdout_no_cambia_nada() -> None:
    """La prueba insignia: el hold-out ENTERO puede ser basura y nada se mueve."""
    raw = rampa(140, retros=(37, 91))
    corte = stability.holdout_start(raw)
    antes = stability.pre_split_features(raw, corte)

    for mutación in (
        lambda v: v + 1e6,
        lambda v: v * -3.0,
        lambda v: np.full(len(v), 0.0),
        lambda v: v[::-1],
    ):
        roto = raw.copy()
        cola = roto.index >= corte
        roto.loc[cola] = mutación(roto.loc[cola].to_numpy())
        assert stability.holdout_start(roto) == corte
        después = stability.pre_split_features(roto, corte)
        assert después == antes, f"la mutación {mutación} movió una feature"
        assert stability.classify(después) == stability.classify(antes)
        assert stability.annotate(después) == stability.annotate(antes)


def test_el_holdout_mutado_si_cambia_las_features_de_toda_la_serie() -> None:
    """Si la mutación fuera inocua por sí misma, la prueba anterior no probaría nada."""
    raw = rampa(140, retros=(37, 91))
    corte = stability.holdout_start(raw)
    roto = raw.copy()
    roto.loc[roto.index >= corte] = -5e5
    completa_antes = stability.pre_split_features(raw, raw.index[-1] + pd.DateOffset(months=1))
    completa_después = stability.pre_split_features(roto, roto.index[-1] + pd.DateOffset(months=1))
    assert completa_antes != completa_después


def test_el_corte_sale_de_la_rejilla_causal_no_del_indice_disperso() -> None:
    """Con huecos DENTRO del hold-out, contar posiciones sobre las F daría otro corte."""
    raw = rampa(120)
    con_huecos = raw.drop(raw.index[[100, 101, 110]])
    corte = stability.holdout_start(con_huecos)
    assert corte == con_huecos.index[-1] - pd.DateOffset(months=HOLDOUT - 1)
    assert corte != con_huecos.index[-HOLDOUT]  # el índice disperso adelanta el corte 3 meses
    # Los 3 huecos caen DENTRO del hold-out: el corte por calendario deja 96 F antes,
    # tres más de las que dejaría contar 24 posiciones sobre el índice disperso.
    assert len(stability.pre_split(con_huecos, corte)) == len(con_huecos) - HOLDOUT + 3


# ------------------------------------------------------------------------- monotonía
@pytest.mark.parametrize("n_retros,esperado", [(0, "estable"), (2, "estable"), (3, "no_estable"), (9, "no_estable")])
def test_monotonia_alrededor_del_umbral_de_retro_rate(n_retros: int, esperado: str) -> None:
    """116 pasos pre-split: 2 retros = 0.0172 (<= 0.02) y 3 = 0.0259 (> 0.02)."""
    raw = rampa(141, paso=400.0, retros=tuple(range(20, 20 + n_retros)))
    corte = stability.holdout_start(raw)
    f = stability.pre_split_features(raw, corte)
    assert f.retro_rate_pre == pytest.approx(n_retros / 116, abs=1e-9)
    assert stability.classify(f) == esperado


def test_mas_retrogresiones_nunca_devuelve_a_estable() -> None:
    """Monotonía real: la etiqueta cambia una vez y no vuelve."""
    etiquetas = []
    for k in range(0, 12):
        raw = rampa(141, paso=400.0, retros=tuple(range(20, 20 + k)))
        f = stability.pre_split_features(raw, stability.holdout_start(raw))
        etiquetas.append(stability.classify(f))
    assert etiquetas[0] == "estable" and etiquetas[-1] == "no_estable"
    assert etiquetas == sorted(etiquetas, key=lambda e: e == "no_estable"), etiquetas


@pytest.mark.parametrize("caida,esperado", [(30_000.0, "estable"), (45_000.0, "no_estable")])
def test_monotonia_alrededor_del_umbral_de_retro_escalado(caida: float, esperado: str) -> None:
    """Un solo retroceso (retro_rate 0.0086 <= 0.02) decidido por su TAMAÑO escalado.

    El escalado no es lineal en la caída: la propia retrogresión infla la escala naïve
    (entra en 12 diferencias de rezago 12), así que 30,000 d da 4.18 y 45,000 d da 5.06.
    """
    v = [1000.0]
    for i in range(1, 141):
        v.append(v[-1] - caida if i == 30 else v[-1] + 400.0)
    raw = serie(v)
    f = stability.pre_split_features(raw, stability.holdout_start(raw))
    assert f.retro_rate_pre <= stability.RETRO_RATE_MAX
    assert (f.worst_retro_scaled_pre > stability.WORST_RETRO_SCALED_MAX) == (esperado == "no_estable")
    assert stability.classify(f) == esperado


def test_una_entrada_no_finita_no_se_clasifica() -> None:
    """NaN > 5 es False: dejarlo pasar convertiría «no medible» en «estable»."""
    plana = serie([7.0] * 140)
    f = stability.pre_split_features(plana, stability.holdout_start(plana))
    assert not np.isfinite(f.worst_retro_scaled_pre)
    with pytest.raises(ValueError, match="no finito"):
        stability.classify(f)


# ------------------------------------------------------- artefacto: esquema y formatos
@pytest.fixture
def catalogo_sintetico(tmp_path: Path) -> tuple[dict, Path]:
    import experiments.build_cohorts as bc

    largo = 141
    panel = panel_sintetico(
        {
            ("mexico", "family", "F1", "FAD"): rampa(largo, retros=(30, 60)),
            ("india", "family", "F4", "FAD"): rampa(largo, retros=tuple(range(20, 32))),
            ("china", "employment", "EB2", "DFF"): casi_congelada(largo),
        }
    )
    # CSV y no parquet a propósito: el job base de CI no instala pyarrow.
    ruta = tmp_path / "panel.csv"
    panel.to_csv(ruta, index=False)
    return bc.build(ruta), ruta


def test_el_panel_se_lee_en_sus_dos_serializaciones(tmp_path: Path) -> None:
    """Las dos serializaciones gobernadas del panel dan el MISMO catálogo."""
    import experiments.build_cohorts as bc

    panel = panel_sintetico({("mexico", "family", "F1", "FAD"): rampa(141, retros=(30, 60))})
    csv, parquet = tmp_path / "p.csv", tmp_path / "p.parquet"
    panel.to_csv(csv, index=False)
    pytest.importorskip("pyarrow")
    panel.to_parquet(parquet)
    desde_csv, desde_parquet = bc.build(csv), bc.build(parquet)
    for cat in (desde_csv, desde_parquet):
        cat["provenance"] = {k: v for k, v in cat["provenance"].items() if k not in {"panel", "panel_sha256"}}
    assert desde_csv == desde_parquet
    with pytest.raises(ValueError, match="panel no reconocido"):
        bc.build(tmp_path / "p.txt")


def test_esquema_cerrado(catalogo_sintetico: tuple[dict, Path]) -> None:
    cat, _ = catalogo_sintetico
    assert set(cat) == {"rule_version", "rule", "split", "population", "cohorts", "provenance", "series"}
    assert set(cat["rule"]) == {
        "no_estable_si",
        "retro_rate_max",
        "worst_retro_scaled_max",
        "recent_window_months",
        "annotations",
        "status",
        "registered_before",
    }
    assert set(cat["split"]) == {"holdout_months", "seasonal_period", "holdout_start_rule", "features_window"}
    assert set(cat["population"]) == {"n_structural", "n_with_F", "n_evaluable", "eligibility"}
    assert set(cat["provenance"]) == {
        "panel",
        "panel_sha256",
        "panel_rows",
        "panel_months",
        "panel_last_month",
        "features",
    }
    import experiments.build_cohorts as bc

    for fila in cat["series"]:
        assert set(fila) == set(bc.COLUMNAS), sorted(set(fila) ^ set(bc.COLUMNAS))
    assert cat["rule_version"] == stability.RULE_VERSION
    assert cat["rule"]["status"] == "exploratoria"
    assert cat["provenance"]["features"] == list(stability.FEATURE_NAMES)


def test_json_y_csv_dicen_lo_mismo(catalogo_sintetico: tuple[dict, Path], tmp_path: Path) -> None:
    import experiments.build_cohorts as bc

    cat, _ = catalogo_sintetico
    rj, rc = bc.write(cat, tmp_path / "salida")
    desde_json = pd.DataFrame(json.loads(rj.read_text())["series"], columns=bc.COLUMNAS)
    desde_csv = pd.read_csv(rc)
    assert list(desde_csv.columns) == bc.COLUMNAS
    for col in bc.COLUMNAS:
        a, b = desde_json[col], desde_csv[col]
        if pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(b):
            assert np.allclose(a.to_numpy(dtype="float64"), b.to_numpy(dtype="float64"), equal_nan=True), col
        else:
            assert a.astype(str).tolist() == b.astype(str).tolist(), col


def test_determinismo_entre_procesos(catalogo_sintetico: tuple[dict, Path], tmp_path: Path) -> None:
    """Dos intérpretes distintos, con hash de cadenas aleatorio, escriben lo mismo."""
    _, panel = catalogo_sintetico
    salidas = []
    for i in range(2):
        destino = tmp_path / f"corrida{i}"
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys;sys.argv=['x','--panel',sys.argv[1],'--out-dir',sys.argv[2]];"
                "import experiments.build_cohorts as bc;bc.main()",
                str(panel),
                str(destino),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            env={**os.environ, "PYTHONHASHSEED": str(i)},
        )
        salidas.append(((destino / "series_cohorts.json").read_bytes(), (destino / "series_cohorts.csv").read_bytes()))
    assert salidas[0] == salidas[1]


def test_las_cohortes_sinteticas_no_son_degeneradas(catalogo_sintetico: tuple[dict, Path]) -> None:
    cat, _ = catalogo_sintetico
    assert cat["cohorts"] == {"estable": 2, "no_estable": 1}, cat["cohorts"]
    assert cat["population"]["n_structural"] == 3 and cat["population"]["n_evaluable"] == 3
    quietas = [s for s in cat["series"] if s["congelada"]]
    assert len(quietas) == 1 and quietas[0]["cohort"] == "estable"


def test_una_serie_elegible_no_clasificable_detiene_el_catalogo(tmp_path: Path) -> None:
    """No hay tercera cohorte: una serie sin escala medible PARA el catálogo, nombrada."""
    import experiments.build_cohorts as bc

    ruta = tmp_path / "panel.csv"
    panel_sintetico({("mexico", "family", "F1", "FAD"): serie([7.0] * 141)}).to_csv(ruta, index=False)
    with pytest.raises(ValueError, match="no finito"):
        bc.build(ruta)


# ------------------------------------------------------------------ panel real (cruce)
class TestPanelReal:
    """Sobre el panel VERSIONADO (CSV): corre en los dos jobs, sin motor de parquet."""

    def test_poblacion_cruzada_contra_key_facts(self) -> None:
        """Dos derivaciones independientes del mismo panel deben coincidir."""
        import experiments.build_cohorts as bc

        cat = bc.build(PANEL_CSV)
        kf = json.loads(KEY_FACTS.read_text())
        assert cat["population"]["n_structural"] == kf["n_series_structural"]
        assert cat["population"]["n_with_F"] == kf["n_series_with_F"]
        assert cat["population"]["n_evaluable"] == kf["n_series_evaluable"]
        assert cat["provenance"]["panel_rows"] == kf["n_obs"]
        assert cat["provenance"]["panel_months"] == kf["n_months"]

    def test_cohortes_reales_no_degeneradas(self) -> None:
        import experiments.build_cohorts as bc

        cat = bc.build(PANEL_CSV)
        assert set(cat["cohorts"]) == {"estable", "no_estable"}
        assert min(cat["cohorts"].values()) >= 10, cat["cohorts"]
        assert sum(cat["cohorts"].values()) == cat["population"]["n_evaluable"]


# El .parquet sólo existe donde alguien lo construyó, y construirlo ya exigió un motor de
# parquet: no hay entorno real con el archivo presente y sin motor. El job base no lo tiene.
@pytest.mark.skipif(not PANEL.exists(), reason="el panel .parquet se construye en el job de modelado")
class TestCatalogoPublicado:
    """Contra el panel que produjo el artefacto: el .parquet del job de modelado."""

    def test_las_dos_serializaciones_dan_el_mismo_catalogo(self) -> None:
        """Si CSV y parquet divergieran, la clase de arriba estaría midiendo otro panel."""
        import experiments.build_cohorts as bc

        a, b = bc.build(PANEL_CSV), bc.build(PANEL)
        for cat in (a, b):
            cat["provenance"] = {k: v for k, v in cat["provenance"].items() if k not in {"panel", "panel_sha256"}}
        assert a == b

    def test_el_catalogo_commiteado_esta_al_dia(self, tmp_path: Path) -> None:
        """Maestro dorado: el archivo del repo es EXACTAMENTE lo que produce el panel de hoy.

        Sin esto, el catálogo podría quedarse rancio y las pruebas seguirían verdes: todas
        las demás reconstruyen desde el panel y ninguna mira los bytes publicados.
        """
        import experiments.build_cohorts as bc

        rj, rc = bc.write(bc.build(), tmp_path / "fresco")
        for fresco, publicado in ((rj, bc.OUT_DIR / rj.name), (rc, bc.OUT_DIR / rc.name)):
            assert publicado.exists(), f"falta {publicado.name}"
            assert fresco.read_bytes() == publicado.read_bytes(), (
                f"{publicado.name} está rancio: corre `python experiments/build_cohorts.py`"
            )

    def test_la_quietud_viaja_aparte_de_la_estabilidad(self) -> None:
        """La trampa del 26-ago: si toda la cohorte estable estuviera congelada, la
        partición estaría midiendo quietud y no regularidad."""
        import experiments.build_cohorts as bc

        estables = [s for s in bc.build(PANEL_CSV)["series"] if s["cohort"] == "estable"]
        con_avance = [s for s in estables if s["median_step_pre"] > 0]
        assert con_avance, "la cohorte estable sería sólo series congeladas"
        assert any(s["congelada"] for s in estables), "la anotación no distinguiría nada"
