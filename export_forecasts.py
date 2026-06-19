"""Exporta los pronósticos hold-out de los finalistas a un CSV tidy para comparar/graficar.

Genera ``reports/finalist_forecasts_{table}.csv`` en formato largo:
``model, type, country, category, date, forecast, actual`` — listo para pandas/seaborn:
cada finalista (local por serie + deep global) sobre los 24 meses de hold-out, evaluado F-only.

Locales: walk-forward de 1 paso (``historical_forecasts``) sobre el hold-out. Deep: se leen
los CSV de la campaña (``reports/global_{table}_camp_*.csv``), ya reintegrados a nivel.

Corre en ``ante``. Uso:  ante/bin/python export_forecasts.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import config, dataset, models, walkforward

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
LOCAL = ("ets", "theta", "sarima", "arima", "kalman", "catboost", "lightgbm")
# variante ganadora por modelo deep (de la campaña): diff salvo PatchTST (nivel).
DEEP = {
    "BiTCN": "camp_diff_s1",
    "PatchTST": "camp_levels_s1",
    "TiDE": "camp_diff_s1",
    "NHITS": "camp_diff_s1",
    "AutoBiTCN": "camp_auto_s1",
}


def _local_rows(table: str) -> list[dict]:
    rows = []
    cat = dataset.list_series(table=table, block="family", countries=config.PILOT_COUNTRIES)
    for r in cat.itertuples():
        ts = models.to_timeseries(dataset.load_series(r.country, r.category, table))
        split = ts.time_index[-walkforward.HOLDOUT]
        actual = ts[split:]
        cov = {"future_covariates": walkforward._covariates(ts)}
        for name in LOCAL:
            try:
                m = models.build_model(name)
                # retrain=False: ajusta una vez sobre el pre-hold-out y rueda 1-paso (rápido,
                # para visualización; los MASE oficiales del .tex vienen del walk-forward completo).
                fc = m.historical_forecasts(  # type: ignore[attr-defined]
                    ts,
                    start=split,
                    forecast_horizon=1,
                    stride=1,
                    retrain=False,
                    last_points_only=True,
                    verbose=False,
                    **(cov if name in config.DIFFERENCED else {}),
                )
                a = actual.slice_intersect(fc)
                for d, av, fv in zip(
                    a.time_index, a.values().flatten(), fc.slice_intersect(a).values().flatten(), strict=False
                ):
                    rows.append(
                        {
                            "model": name,
                            "type": "local",
                            "country": r.country,
                            "category": r.category,
                            "date": d,
                            "forecast": float(fv),
                            "actual": float(av),
                        }
                    )
            except Exception:  # noqa: BLE001
                pass
    return rows


def _deep_rows(table: str) -> list[dict]:
    rows = []
    for name, suffix in DEEP.items():
        path = REPORTS / f"global_{table}_{suffix}.csv"
        if not path.exists() or name not in pd.read_csv(path, nrows=1).columns:
            continue
        df = pd.read_csv(path, parse_dates=["ds"])
        for uid, g in df.groupby("unique_id"):
            country, _block, category = uid.split("/")
            try:
                full = dataset.load_series(country, category, table).astype("float64")
            except KeyError:
                continue
            for _, row in g.iterrows():
                d = row["ds"]
                if d in full.index and not np.isnan(row[name]):  # F-only
                    rows.append(
                        {
                            "model": name,
                            "type": "global_deep",
                            "country": country,
                            "category": category,
                            "date": d,
                            "forecast": float(row[name]),
                            "actual": float(full.loc[d]),
                        }
                    )
    return rows


def main() -> None:
    for table in config.TABLES:
        rows = _local_rows(table) + _deep_rows(table)
        out = REPORTS / f"finalist_forecasts_{table}.csv"
        pd.DataFrame(rows).to_csv(out, index=False)
        print(f"{table}: {len(rows)} filas, {pd.DataFrame(rows)['model'].nunique()} modelos -> {out.name}")


if __name__ == "__main__":
    main()
