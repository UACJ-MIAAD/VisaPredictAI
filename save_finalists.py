"""Guarda los modelos LOCALES por serie finalistas (darts) para reusar/comparar/graficar/explotar.

Por tabla (FAD, DFF) y por serie del panel familiar, entrena los modelos finalistas locales
sobre TODA la serie (modelo desplegable) y los persiste con ``model.save()`` en
``models/{table}/local/{model}/{pais}_{cat}/``, con una entrada en el manifiesto. Los modelos
diferenciados (GBMs) reciben las covariables de calendario igual que el walk-forward.

Corre en ``ante``. Uso:  ante/bin/python save_finalists.py
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib

from vp_model import config, dataset, models, walkforward

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
MANIFEST = MODELS / "manifest.jsonl"
# Finalistas locales (top del barrido): parsimonia + árbol + clásicos. SARIMA se omite del
# pickle por serie (darts/statsmodels lo serializa en ~10 MB; su forecast vive en los CSV y
# re-ajustarlo es barato). Se persiste con joblib (uniforme, sirve para el wrapper Differenced).
LOCAL = ("ets", "theta", "arima", "kalman", "catboost", "lightgbm")


def _manifest(entry: dict) -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def main() -> None:
    log = config.get_logger("save_finalists")
    for table in config.TABLES:
        cat = dataset.list_series(table=table, block="family", countries=config.PILOT_COUNTRIES)
        for r in cat.itertuples():
            ts = models.to_timeseries(dataset.load_series(r.country, r.category, table))
            extra = {"future_covariates": walkforward._covariates(ts)}
            for name in LOCAL:
                try:
                    model = models.build_model(name)
                    fit_kwargs = extra if name in config.DIFFERENCED else {}
                    model.fit(ts, **fit_kwargs)  # type: ignore[attr-defined]
                    out = MODELS / table / "local" / name / f"{r.country}_{r.category}"
                    out.mkdir(parents=True, exist_ok=True)
                    joblib.dump(model, out / "model.pkl")
                    _manifest(
                        {
                            "model": name,
                            "table": table,
                            "type": "local",
                            "country": r.country,
                            "category": r.category,
                            "path": str((out / "model.pkl").relative_to(ROOT)),
                        }
                    )
                except Exception as e:  # noqa: BLE001 — un modelo/serie que falle no aborta el resto
                    log.warning("%s/%s/%s %s FALLO: %s", table, r.country, r.category, name, type(e).__name__)
        log.info("guardados locales de %s", table)


if __name__ == "__main__":
    main()
