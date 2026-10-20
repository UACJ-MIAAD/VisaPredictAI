"""M74-E-R21 · una sola forma de fecha en `finalist_forecasts_*` y un cruce deep↔parsimonia que no divide por cero.

La tercera campaña real sobre `a6df0f4` (`rederiv_a6df0f4_20260917T055408`, 17/18-sep-2026) pasó las etapas 1 a 6.5
en 24 h y murió en la 7: `significance_tables._dm_deep_vs_parsimony` cruza los pronósticos deep del artefacto de
finalistas con el hold-out acreditado por (country, category, date); el exportador escribía el `Timestamp` crudo
(`2024-10-01 00:00:00`) en las filas locales y deep y la cadena ISO (`2024-10-01`) en las transportadas, el hold-out
va en ISO, el join daba CERO pares y `dm_test` dividía por cero en la corrección de Harvey-Leybourne-Newbold.

Ahora el exportador escribe `YYYY-MM-DD` en todas las filas (`iso_date`), el consumidor normaliza la fecha de los dos
lados antes de cruzar y falla con las claves si no hay pares, y `dm_test` exige al menos dos errores pareados.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "experiments"))

pytest.importorskip("scipy")

from tests.holdout_fixture import artefacto_completo, escena_coherente  # noqa: E402
from vp_model import significance  # noqa: E402

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_RED_dm_test_sin_pares_falla_con_un_error_explicito() -> None:
    """Contra `a6df0f4`: ZeroDivisionError dentro de la corrección de muestra finita."""
    with pytest.raises(ValueError, match="al menos 2"):
        significance.dm_test(np.array([]), np.array([]))
    with pytest.raises(ValueError, match="al menos 2"):
        significance.dm_test(np.array([0.5]), np.array([0.7]))


def test_RED_las_filas_deep_del_exportador_llevan_fecha_iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Contra `a6df0f4`: `date` era un `Timestamp` y `to_csv` lo escribía como `2024-10-01 00:00:00`."""
    import export_forecasts as ef

    from vp_model import dataset

    name, suffix = next(iter(ef.DEEP.items()))
    serie = dataset.load_series("mexico", "F1", "FAD")
    fechas = list(serie.index[-3:])
    reports = tmp_path / "reports"
    (reports / "campaign").mkdir(parents=True)
    pd.DataFrame({"unique_id": "mexico/family/F1", "ds": fechas, name: [1.0, 2.0, 3.0]}).to_csv(
        reports / "campaign" / f"global_FAD_{suffix}.csv", index=False
    )
    monkeypatch.setattr(ef, "REPORTS", reports)
    filas = ef._deep_rows("FAD")
    assert len(filas) == 3
    assert all(isinstance(f["date"], str) and ISO.match(f["date"]) for f in filas), [f["date"] for f in filas]
    assert ef.iso_date(pd.Timestamp("2024-10-01 00:00:00")) == "2024-10-01" == ef.iso_date("2024-10-01")


def test_RED_el_cruce_deep_parsimonia_encuentra_pares_aunque_el_artefacto_traiga_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La escena de la 3ª campaña: hold-out acreditado en ISO y finalistas deep con `00:00:00`."""
    import significance_tables as st

    from vp_model import persist_forecasts as pf

    reports = escena_coherente(tmp_path, monkeypatch, campaign_id="camp_r21")
    artefacto_completo(reports, "FAD", campaign_id="camp_r21")
    claves = sorted(pf.expected_keys("FAD"))
    deep = pd.DataFrame(
        [
            {"model": "BiTCN", "type": "global_deep", "country": c, "category": k, "date": f"{d} 00:00:00", "forecast": 1.5, "actual": 1.0}
            for (_m, c, k, d) in claves
            if _m == "ets"
        ]
    )  # fmt: skip
    # la columna REAL es mixta: deep/locales con `00:00:00` y ETS/Theta transportados en ISO puro. `pd.to_datetime`
    # infiere el formato del primer valor y revienta con el segundo salvo que se declare ISO 8601 (visto en el ensayo).
    ets = deep.assign(model="ets", type="local_transported", date=lambda d: d.date.str.slice(0, 10))
    pd.concat([deep, ets]).to_csv(reports / "eval" / "finalist_forecasts_FAD.csv", index=False)
    monkeypatch.setattr(st, "REPORTS", reports)
    resultado = st._dm_deep_vs_parsimony("FAD")
    assert resultado["n_pairs"] > 0 and resultado["best_deep"] == "BiTCN"


def test_sin_pares_el_consumidor_dice_que_claves_faltan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed con contexto: fechas deep fuera del hold-out ⇒ ValueError que nombra el cruce, no un ZeroDivisionError."""
    import significance_tables as st

    reports = escena_coherente(tmp_path, monkeypatch, campaign_id="camp_r21")
    artefacto_completo(reports, "FAD", campaign_id="camp_r21")
    deep = pd.DataFrame(
        [{"model": "BiTCN", "type": "global_deep", "country": "mexico", "category": "F1", "date": "1999-01-01", "forecast": 1.5, "actual": 1.0}]
    )  # fmt: skip
    deep.to_csv(reports / "eval" / "finalist_forecasts_FAD.csv", index=False)
    monkeypatch.setattr(st, "REPORTS", reports)
    with pytest.raises(ValueError, match="no comparten ninguna clave"):
        st._dm_deep_vs_parsimony("FAD")


def test_RED_las_figuras_de_resultados_crean_su_carpeta_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`reports/latex/Figures/` está ignorada por git desde M66 y no existe en un checkout limpio: la etapa 10 (run_req)
    moría con FileNotFoundError. Contra `a6df0f4`: `_emit` no crea la carpeta."""
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    import make_result_figures as mrf
    import matplotlib.pyplot as plt

    destino = tmp_path / "reports" / "latex" / "Figures"
    monkeypatch.setattr(mrf, "FIG", destino)
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    mrf._emit(fig, "prueba_r21")
    assert (destino / "prueba_r21.pdf").is_file()
