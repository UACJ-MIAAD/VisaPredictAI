"""M74-F · deuda 8 — el diagnóstico de deriva entre añadas de hold-out, con datos sintéticos.

Fija la conducta que sostiene su lectura: la igualdad se mide sobre las claves compartidas, los huecos
interiores se cuentan antes del hold-out y la contingencia separa series con y sin huecos. Y que no
escribe dentro de `reports/`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "experiments"))

import diagnose_holdout_drift as diag  # noqa: E402


def _panel() -> pd.DataFrame:
    filas = []
    for mes in pd.period_range("2020-01", "2024-12", freq="M"):
        d = str(mes.to_timestamp().date())
        filas.append({"country": "mexico", "category": "F1", "table": "FAD", "bulletin_date": d, "status": "F"})
        # india/F2 pierde dos meses interiores: C en 2021-03 y sin fila en 2021-04
        if str(mes) == "2021-03":
            filas.append({"country": "india", "category": "F2", "table": "FAD", "bulletin_date": d, "status": "C"})
        elif str(mes) != "2021-04":
            filas.append({"country": "india", "category": "F2", "table": "FAD", "bulletin_date": d, "status": "F"})
    return pd.DataFrame(filas)


def test_cuenta_los_huecos_interiores_antes_del_holdout() -> None:
    gaps = diag.huecos_interiores(_panel(), "FAD", pd.Timestamp("2024-01-01"))
    assert gaps == {("india", "F2"): 2, ("mexico", "F1"): 0}


def _hold(forecast_india: float) -> pd.DataFrame:
    filas = []
    for d in ("2024-01-01", "2024-02-01"):
        for m in ("naive1", "ets"):
            filas.append(
                {"model": m, "country": "mexico", "category": "F1", "date": d, "actual": 10.0, "forecast": 9.0}
            )
            f = 5.0 if m == "naive1" else forecast_india
            filas.append({"model": m, "country": "india", "category": "F2", "date": d, "actual": 6.0, "forecast": f})
    return pd.DataFrame(filas)


def test_la_contingencia_separa_series_con_y_sin_huecos() -> None:
    m = diag.comparar(_hold(4.0), _hold(4.5))
    gaps = diag.huecos_interiores(_panel(), "FAD", pd.Timestamp("2024-01-01"))
    c = {(f["model"], f["has_interior_gaps"]): f for f in diag.contingencia(m, gaps)}
    assert c[("ets", False)]["series_all_identical"] == 1 and c[("ets", True)]["series_all_identical"] == 0
    assert c[("naive1", True)]["rows_identical"] == 2


def test_main_escribe_fuera_de_reports_y_se_niega_dentro(tmp_path: Path) -> None:
    _hold(4.0).to_csv(tmp_path / "base.csv", index=False)
    _hold(4.5).to_csv(tmp_path / "new.csv", index=False)
    _panel().to_csv(tmp_path / "panel.csv", index=False)
    args = ["--table", "FAD", "--base", str(tmp_path / "base.csv"), "--new", str(tmp_path / "new.csv"),
            "--panel", str(tmp_path / "panel.csv")]  # fmt: skip
    out = tmp_path / "drift.json"
    assert diag.main([*args, "--out", str(out)]) == 0
    informe = json.loads(out.read_text())
    assert informe["by_model"]["ets"] == {"shared": 4, "identical": 2}
    assert informe["actuals_identical_on_overlap"] is True
    with pytest.raises(SystemExit, match="dentro de reports"):
        diag.main([*args, "--out", str(RAIZ / "reports" / "eval" / "drift.json")])
