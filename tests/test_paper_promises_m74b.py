"""M74-B · Los dos análisis que `paper.tex` promete «con la re-derivación» (H8).

El manuscrito afirma en futuro que la re-derivación cuantificará (1) el efecto de proyectar al cono
de coherencia sobre el error puntual agregado y la cobertura, comparando la misma cohorte antes y
después, y (2) una comparación directa contra un naïve estacional **fuera de muestra evaluado en
los mismos orígenes**. Ninguno existía. Aquí se prueban sobre datos sintéticos, donde la respuesta
se conoce de antemano: una prueba que sólo comprobara que el script corre no diría nada.
"""

from __future__ import annotations

from importlib.util import find_spec

import pytest

MODELADO = pytest.mark.skipif(find_spec("statsmodels") is None, reason="requiere el extra `model`")
pytestmark = MODELADO


@pytest.fixture
def app():
    import importlib

    return importlib.import_module("experiments.analyze_paper_promises")


def _marco(app):
    import pandas as pd

    # dos celdas del MISMO país/categoría en ambas tablas: FAD por encima de DFF viola el cono
    return pd.DataFrame(
        [
            {"country": "mexico", "category": "F1", "table": "FAD", "date": "2026-10-01",
             "days": 1000, "lo80": 900, "hi80": 1100, "lo95": 800, "hi95": 1200},
            {"country": "mexico", "category": "F1", "table": "DFF", "date": "2026-10-01",
             "days": 900, "lo80": 800, "hi80": 1000, "lo95": 700, "hi95": 1100},
        ]
    )  # fmt: skip


def test_the_cone_effect_is_measured_on_the_same_cells_before_and_after(app) -> None:
    """Se comparan las MISMAS celdas, y se separa el subconjunto que la proyección altera."""
    import pandas as pd

    forecasts = _marco(app)
    actuals = pd.DataFrame(
        [
            {"country": "mexico", "category": "F1", "table": "FAD", "date": "2026-10-01", "actual": 880},
            {"country": "mexico", "category": "F1", "table": "DFF", "date": "2026-10-01", "actual": 900},
        ]
    )
    r = app.cone_effect(forecasts, actuals)
    assert r["cells_compared"] == 2
    assert r["violations"]["cone_violations_pre"] >= 1, "el marco sintético viola el cono a propósito"
    assert r["violations"]["cone_violations_post"] == 0, "proyectar debe eliminar la violación"
    assert r["cells_altered_by_projection"] >= 1
    # el agregado y el subconjunto alterado se reportan por separado: sobre el agregado el
    # efecto se diluye, y decir sólo el agregado escondería la magnitud del cambio
    assert set(r["aggregate"]) == {"before", "after"} and set(r["altered_only"]) == {"before", "after"}
    assert r["aggregate"]["before"]["n"] == 2
    for lado in ("before", "after"):
        assert 0.0 <= r["aggregate"][lado]["coverage95"] <= 1.0


def test_a_frame_without_violations_passes_through_unchanged(app) -> None:
    """Sin violaciones, proyectar no altera ninguna celda: el efecto debe salir nulo, no inventado."""
    import pandas as pd

    forecasts = _marco(app)
    forecasts.loc[forecasts["table"] == "FAD", "days"] = 800  # FAD <= DFF: coherente
    actuals = pd.DataFrame(
        [
            {"country": "mexico", "category": "F1", "table": "FAD", "date": "2026-10-01", "actual": 800},
            {"country": "mexico", "category": "F1", "table": "DFF", "date": "2026-10-01", "actual": 900},
        ]
    )
    r = app.cone_effect(forecasts, actuals)
    assert r["cells_altered_by_projection"] == 0
    assert r["aggregate"]["before"] == r["aggregate"]["after"], "sin violaciones, antes y después coinciden"
    assert r["altered_only"]["before"]["n"] == 0 and r["altered_only"]["before"]["mae_days"] is None


def test_the_seasonal_naive_is_out_of_sample_and_uses_only_information_available_at_the_origin(app) -> None:
    """La referencia estacional sale de ``y(target − m)`` y SÓLO de meses ≤ origen.

    Es lo que distingue este rival del denominador del MASE, que es la escala **dentro** de muestra.
    """
    import pandas as pd

    # panel mensual con estacionalidad conocida: el mes t vale 100*t
    fechas = pd.date_range("2024-01-01", periods=36, freq="MS")
    panel = pd.DataFrame(
        {
            "country": "mexico", "category": "F1", "table": "FAD", "status": "F",
            "bulletin_date": fechas, "days_since_base": [100 * i for i in range(36)],
        }
    )  # fmt: skip
    origen, objetivo = fechas[30], fechas[33]
    scorecard = pd.DataFrame(
        [{"origin": origen, "h": 3, "country": "mexico", "category": "F1", "table": "FAD",
          "target": objetivo, "pred": 3300.0, "actual": 3300.0}]
    )  # fmt: skip
    r = app.seasonal_naive_oos(scorecard, panel, m=12)
    assert r["pairs_compared"] == 1 and r["pairs_without_seasonal_reference"] == 0
    # la referencia es el mes 33−12 = 21 ⇒ 2100; el actual es 3300 ⇒ error 1200
    assert r["overall"]["mae_seasonal_naive_oos"] == pytest.approx(1200.0)
    assert r["overall"]["mae_model"] == pytest.approx(0.0)
    assert r["overall"]["model_wins_share"] == pytest.approx(1.0)


def test_a_pair_without_a_seasonal_reference_is_discarded_not_imputed(app) -> None:
    """RED: si el mes de referencia no está disponible AL ORIGEN, el par se descarta y se cuenta."""
    import pandas as pd

    fechas = pd.date_range("2024-01-01", periods=36, freq="MS")
    panel = pd.DataFrame(
        {
            "country": "mexico", "category": "F1", "table": "FAD", "status": "F",
            "bulletin_date": fechas, "days_since_base": [100 * i for i in range(36)],
        }
    )  # fmt: skip
    # origen muy temprano: el mes objetivo−12 aún no se ha publicado al origen
    scorecard = pd.DataFrame(
        [{"origin": fechas[5], "h": 3, "country": "mexico", "category": "F1", "table": "FAD",
          "target": fechas[8], "pred": 800.0, "actual": 800.0}]
    )  # fmt: skip
    with pytest.raises(SystemExit, match="referencia estacional"):
        app.seasonal_naive_oos(scorecard, panel, m=12)


@pytest.mark.skipif(find_spec("darts") is None, reason="requiere el extra `model` (darts)")
def test_the_publisher_keeps_the_pre_projection_frame_without_touching_the_live_tree(tmp_path) -> None:
    """El ANTES se conserva (H8), y proyectar sigue sin escribir por su cuenta.

    ⚠️ Defecto propio, cazado por `git status` tras la suite: al persistir el marco DENTRO de
    `_project_rows` convertí una proyección en una escritura, y cuatro pruebas de
    `test_web_publish.py` que la llaman con filas sintéticas escribían en
    `reports/prospective/` del árbol vivo. La escritura es del llamador; aquí se prueban las dos
    mitades, ejecutando, no leyendo el texto.
    """
    import sys
    from pathlib import Path

    import pandas as pd

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))
    import generate_web_forecasts as gwf

    filas = [
        {
            "origin": "2026-07", "h": 1, "country": "mexico", "category": "F1", "table": tabla,
            "date": "2026-08-01", "days": d, "lo80": d - 50, "hi80": d + 50,
            "lo95": d - 100, "hi95": d + 100, "band_method": "q_h",
        }
        for tabla, d in (("FAD", 9_000), ("DFF", 9_500))
    ]  # fmt: skip
    vivo = Path(gwf.REPORTS) / "prospective" / "web_forecasts_precone.csv"
    #: ⚠️ `not vivo.exists()` no bastaría: si el archivo ya estuviera ahí, la aserción pasaría
    #: sola. Se compara el CONTENIDO, que cambiaría igual con una escritura encubierta.
    antes = vivo.read_bytes() if vivo.exists() else None

    salida, _ = gwf._project_rows([dict(f) for f in filas])  # sin destino: NO escribe
    assert salida, "proyectar debe seguir devolviendo filas"
    despues = vivo.read_bytes() if vivo.exists() else None
    assert despues == antes, "proyectar sin destino no puede tocar el árbol vivo"

    destino = tmp_path / "prospective" / "web_forecasts_precone.csv"
    gwf._project_rows([dict(f) for f in filas], precone=destino)
    guardado = pd.read_csv(destino)
    assert len(guardado) == len(filas), "el llamador sí persiste el marco PREVIO a la proyección"
    assert list(guardado["days"]) == [f["days"] for f in filas], "y lo guarda ANTES de proyectar"


def test_the_runbook_produces_both_promised_analyses() -> None:
    from pathlib import Path

    guion = (Path(__file__).resolve().parent.parent / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    assert "analyze_paper_promises.py" in guion
    assert "run_req $ANTE experiments/analyze_paper_promises.py" in guion, "es obligatoria, no best-effort"
