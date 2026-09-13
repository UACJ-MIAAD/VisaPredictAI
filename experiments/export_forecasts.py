"""Exporta los pronósticos hold-out de los finalistas a un CSV tidy para comparar/graficar.

Genera ``reports/eval/finalist_forecasts_{table}.csv`` en formato largo:
``model, type, country, category, date, forecast, actual`` — listo para pandas/seaborn:
cada finalista (local por serie + deep global) sobre los 24 meses de hold-out, evaluado F-only.

Locales: walk-forward de 1 paso (``historical_forecasts``) sobre el hold-out. Deep: se leen
los CSV de la campaña (``reports/campaign/global_{table}_camp_*.csv``), ya reintegrados a nivel.

Corre en ``ante``. Uso:  ante/bin/python experiments/export_forecasts.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import config, dataset, models, walkforward
from vp_model.feature_builder import FeatureBuilder

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


# ★ M74-E: las listas salen del REGISTRO CANÓNICO, no de aquí. Había tres autoridades y ya
# discrepaban: este archivo esperaba 7 locales y `save_finalists.py` persistía 6 (sin `sarima`).
#
# El import va DENTRO de la función, no arriba: arriba dispararía el trinquete de `noqa: E402`
# por el orden de imports de este guion. Diferirlo cuesta una línea y no deja marcadores.
def _registro():
    """Las tres vistas del registro canónico que este exportador consume."""
    from vp_model.model_registry import (
        RECOMPUTED_FORECAST_MODELS,
        REQUIRED_FORECAST_MODELS,
        TRANSPORTED_FORECAST_MODELS,
    )

    return RECOMPUTED_FORECAST_MODELS, REQUIRED_FORECAST_MODELS, TRANSPORTED_FORECAST_MODELS


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
        raw = dataset.load_series(r.country, r.category, table)
        ts = models.to_timeseries(raw)
        split = ts.time_index[-walkforward.HOLDOUT]
        actual = ts[split:]
        for name in _registro()[0]:
            try:
                m = models.build_model(name, table=table)  # tuned per-table params for GBMs (Wave-1)
                fcov = FeatureBuilder(name).covariates(ts, raw)  # política por modelo (AD1/AD8/F1)
                fit_kw = {"future_covariates": fcov} if fcov is not None else {}
                # ajustar una vez sobre el pre-hold-out, luego rodar 1-paso con retrain=False
                # (rápido, para visualización; los MASE oficiales del .tex vienen del walk-forward).
                m.fit(ts[: -walkforward.HOLDOUT], **fit_kw)  # type: ignore[attr-defined]
                fc = m.historical_forecasts(  # type: ignore[attr-defined]
                    ts,
                    start=split,
                    forecast_horizon=1,
                    stride=1,
                    retrain=False,
                    last_points_only=True,
                    verbose=False,
                    **fit_kw,
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
            except Exception as e:  # noqa: BLE001
                # B6: un modelo que falla ya no desaparece del CSV sin registro
                print(f"[export_forecasts] FAIL {name} {r.country}/{r.category}: {type(e).__name__}: {str(e)[:80]}")
    return rows


def _deep_rows(table: str) -> list[dict]:
    rows = []
    for name, suffix in DEEP.items():
        path = REPORTS / "campaign" / f"global_{table}_{suffix}.csv"
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


def _expected_transport_keys() -> set:
    """Las claves que el transporte DEBE traer. Del universo vigente y del registro, no de las filas.

    Derivarlo de lo producido sería preguntarle al sospechoso si el conjunto está completo.
    """
    import os

    from vp_model import transported_forecasts as tf

    universo: list[tuple[str, str, str]] = []
    objetivos: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for table in config.TABLES:
        cat = dataset.list_series(table=table, block="family", countries=config.PILOT_COUNTRIES)
        for r in cat.itertuples():
            ts = models.to_timeseries(dataset.load_series(r.country, r.category, table))
            fechas = ts.time_index[-walkforward.HOLDOUT :]
            clave = (table, r.country, r.category)
            universo.append(clave)
            objetivos[clave] = [
                ((pd.Timestamp(d).to_period("M") - 1).to_timestamp().strftime("%Y-%m-%d"),
                 pd.Timestamp(d).strftime("%Y-%m-%d"))
                for d in fechas
            ]  # fmt: skip
    return tf.expected_keys(
        campaign_id=os.environ.get("CAMPAIGN_ID", ""),
        universe=universo,
        models=_registro()[2],
        targets=objetivos,
    )


def _transported_rows(table: str, esperado: set) -> list[dict]:
    """Los pronósticos TRANSPORTADOS (`ets`, `theta`) del walk-forward oficial. Fail-closed.

    ⚠️ Sin respaldo a un archivo anterior y **sin caer a `retrain=True`**. Que AutoETS/AutoTheta no
    puedan recalcularse aquí no los vuelve opcionales: sin transporte acreditado, no hay exportación.
    """
    import os

    from vp_model import transported_forecasts as tf

    filas = tf.load_and_accredit(
        root=ROOT,
        campaign_id=os.environ.get("CAMPAIGN_ID", ""),
        code_sha=os.environ.get("CAMPAIGN_SHA", ""),
        panel_sha256=tf.panel_sha256_of(ROOT),
        esperado=esperado,
    )
    return [
        {
            "model": f["model"], "type": "local_transported", "country": f["country"],
            "category": f["category"], "date": f["target"], "forecast": f["y_pred"], "actual": f["y_true"],
        }
        for f in filas
        if f["table"] == table and f["observed"]
    ]  # fmt: skip


def main() -> None:
    # ★ M74-E: la cobertura se acredita ANTES de escribir, contra el registro canónico. El
    # exportador terminaba en verde ocultando 50 eventos gobernados fallidos (`ets`/`theta`).
    _, requeridos, _ = _registro()
    esperado = _expected_transport_keys()
    for table in config.TABLES:
        rows = _local_rows(table) + _deep_rows(table) + _transported_rows(table, esperado)
        presentes = {r["model"] for r in rows}
        faltan = [m for m in requeridos if m not in presentes]
        sobran = sorted(presentes - set(requeridos))
        if faltan or sobran:
            raise SystemExit(
                f"✗ {table}: cobertura de pronósticos ROTA — faltan {faltan}, sobran {sobran}. "
                f"El registro exige exactamente {list(requeridos)}. NO se escribe nada."
            )
        out = REPORTS / "eval" / f"finalist_forecasts_{table}.csv"
        pd.DataFrame(rows).to_csv(out, index=False)
        print(f"{table}: {len(rows)} filas, {len(presentes)} modelos (cobertura completa) -> {out.name}")


if __name__ == "__main__":
    main()
