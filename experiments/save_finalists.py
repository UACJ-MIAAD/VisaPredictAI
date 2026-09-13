"""Guarda los modelos LOCALES por serie finalistas (darts) para reusar/comparar/graficar/explotar.

Por tabla (FAD, DFF) y por serie del panel familiar, entrena los modelos finalistas locales
sobre TODA la serie (modelo desplegable) y los persiste con ``model.save()`` en
``models/{table}/local/{model}/{pais}_{cat}/``, con una entrada en el manifiesto. Los modelos
diferenciados (GBMs) reciben las covariables de calendario igual que el walk-forward.

AO5 — acta de nacimiento: cada entrada del manifiesto lleva ``git_sha``, ``git_dirty`` y
``panel_hash`` (md5 del panel CSV), atando cada pickle al código y a los datos exactos que
lo produjeron. Decisión documentada: HOY nadie consume ``models/`` en producción — el
demostrador web re-ajusta los modelos estadísticos en cada corrida (son baratos). El
directorio se conserva a propósito como **snapshot de campaña con procedencia** (auditoría
y recuperación de cualquier cifra publicada), no como shelf-ware mudo. Ver
``docs/mlops_experimentos.md``.

Corre en ``ante``. Uso:  ante/bin/python experiments/save_finalists.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import joblib

from vp_data import tracking
from vp_model import config, dataset, models
from vp_model.feature_builder import FeatureBuilder

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
MANIFEST = MODELS / "manifest.jsonl"
PANEL_CSV = ROOT / "data" / "processed" / "visa_panel_long.csv"


# ★ M74-E-R1: DERIVADA del registro canónico, no escrita a mano.
#
# Esta lista es una de las tres que motivaron `vp_model/model_registry.py` — y hasta hoy seguía
# escrita a mano y divergente. Producía `ets` y `theta`, que el registro declara NO persistibles
# (AutoETS/AutoTheta no conservan estado reutilizable: persistirlos es trabajo tirado e invita a
# consumirlos), y **omitía `sarima`**, que el promotor SÍ exige. El paso [2b] habría rechazado su
# propia tubería por falta de cobertura.
#
# ⚠️ La omisión de SARIMA tenía un motivo de tamaño escrito aquí (darts/statsmodels lo serializa
# en ~10 MB por serie). M74-E MIDIÓ que sí es persistible y lo declaró en el registro; si el coste
# en disco pesa más que la reutilización, la decisión se cambia EN EL REGISTRO y esta lista la
# sigue sola. Lo que no puede volver a pasar es que productor y gate declaren cosas distintas.
def _locales_persistibles() -> tuple[str, ...]:
    from vp_model.model_registry import LOCAL_MODELS, PERSISTED_MODELS

    persistidos = set(PERSISTED_MODELS)
    return tuple(m for m in LOCAL_MODELS if m in persistidos)


LOCAL = _locales_persistibles()


def _manifest(entry: dict) -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def birth_certificate() -> dict:
    """Provenance stamp for every manifest entry (AO5): code sha + data hash.

    Same panel-hash convention as ``build_model_card._panel_hash`` (md5, 12 hex chars)
    so lineage is joinable across governance artifacts.
    """
    sha, dirty = tracking.git_state()
    panel_hash = hashlib.md5(PANEL_CSV.read_bytes()).hexdigest()[:12] if PANEL_CSV.exists() else "n/d"
    return {"git_sha": sha, "git_dirty": dirty, "panel_hash": panel_hash}


def main() -> None:
    log = config.get_logger("save_finalists")
    birth = birth_certificate()
    for table in config.TABLES:
        cat = dataset.list_series(table=table, block="family", countries=config.PILOT_COUNTRIES)
        for r in cat.itertuples():
            raw = dataset.load_series(r.country, r.category, table)
            ts = models.to_timeseries(raw)
            for name in LOCAL:
                try:
                    model = models.build_model(name, table=table)  # tuned per-table params for GBMs (Wave-1)
                    cov = FeatureBuilder(name).covariates(ts, raw)  # política por modelo (AD1/AD8/F1)
                    fit_kwargs = {"future_covariates": cov} if cov is not None else {}
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
                            **birth,
                        }
                    )
                except Exception as e:  # noqa: BLE001 — un modelo/serie que falle no aborta el resto
                    log.warning("%s/%s/%s %s FALLO: %s", table, r.country, r.category, name, type(e).__name__)
        log.info("guardados locales de %s", table)


if __name__ == "__main__":
    main()
