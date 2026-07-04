"""Motor de ingeniería de características de la capa de modelado (AD1).

Antes, las transformaciones que alimentan a los 21 modelos vivían inline y
dispersas: la regularización de huecos en ``models.to_timeseries``, las
covariables de calendario en ``walkforward._covariates``, el escalado en dos
bloques gemelos de ``walkforward``. Correctas, pero implícitas: ninguna corrida
podía declarar QUÉ features la produjeron.

``FeatureBuilder`` compone esas transformaciones bajo un contrato explícito:

* una instancia por modelo (la política por familia vive en ``config.COVARIATES``
  / ``DIFFERENCED`` / ``NEEDS_SCALING`` — SRP: el builder compone, config decide);
* ``fit_scaler`` recibe la ventana de entrenamiento explícita (leakage-safe por
  construcción: el que llama no puede olvidar el corte);
* ``realized()`` devuelve el linaje exacto (versión, covariables, transforms)
  que consume ``config.run_metadata`` → MLflow y ``build_fe_facts`` → web/reporte.

El comportamiento es BYTE-IDÉNTICO al camino previo (golden master en
``tests/test_feature_builder.py``): esto es arquitectura, no una re-campaña.

FE_DECISIONS es el gemelo de ``vp_data.cleaning.CLEANING_DECISIONS``: el registro
único de las decisiones magistrales de FE del que derivan el reporte PDF y la
sección #fe del sitio (la narrativa no puede divergir del código que la implementa).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from darts import TimeSeries
from darts.dataprocessing.transformers import Scaler

from vp_model import models, preprocess
from vp_model.config import (
    COVARIATES,
    DIFFERENCED,
    HYPERPARAMS,
    MAX_INTERPOLABLE_GAP,
    NEEDS_SCALING,
)

FE_VERSION = "1.0.0"

# Registro único de decisiones magistrales de FE (qué · dónde · por qué).
FE_DECISIONS: tuple[dict[str, str], ...] = (
    {
        "id": "target_days_since_base",
        "title": "Objetivo = días desde t0 (1975-01-01), solo estado F",
        "module": "pipeline/build_panel.py · schema.sql:days_is_datediff",
        "rationale": (
            "La fecha de prioridad se convierte a un entero de días desde una época "
            "fija anterior a la prioridad más antigua observada (1979-11, Filipinas "
            "F4). Un objetivo numérico continuo, con contrato aritmético re-verificado "
            "en el almacén, en lugar de fechas crudas imposibles de regresar."
        ),
    },
    {
        "id": "gap_regularization",
        "title": "Rejilla mensual regular con huecos acotados",
        "module": "vp_model/preprocess.py:to_regular_monthly",
        "rationale": (
            "Los modelos exigen índice regular; los meses C/U no son objetivo. "
            "Corridas de hueco ≤3 meses se interpolan linealmente; las largas quedan "
            "NaN (todo-o-nada por corrida) y el relleno de continuidad posterior "
            "jamás se puntúa (máscara F-only B1)."
        ),
    },
    {
        "id": "differencing_trees",
        "title": "Árboles predicen la primera diferencia, no el nivel",
        "module": "vp_model/models.py:Differenced · vp_model/preprocess.py:difference/undifference",
        "rationale": (
            "Un árbol no extrapola fuera del rango visto: sobre el nivel (tendencia "
            "creciente de décadas) se satura al máximo histórico. Modelar Δy mensual "
            "(estacionario) y reintegrar de forma causal (cumsum anclado al último "
            "nivel observado) resuelve la extrapolación gratis."
        ),
    },
    {
        "id": "calendar_cyclic",
        "title": "Calendario fiscal codificado cíclicamente (seno/coseno)",
        "module": "vp_model/preprocess.py:calendar_features",
        "rationale": (
            "El año fiscal de visas arranca en octubre (las cuotas se reinician ahí). "
            "Codificar mes y posición fiscal con seno/coseno evita imponer un orden "
            "falso entre diciembre y enero — un entero 1..12 haría que el modelo "
            "viera esos meses vecinos como los más lejanos."
        ),
    },
    {
        "id": "lags_24",
        "title": "24 rezagos mensuales como memoria de los regresores",
        "module": "vp_model/config.py:HYPERPARAMS['trees']/'rlinear'",
        "rationale": (
            "Dos años de historia por origen: cubre un ciclo fiscal completo con "
            "margen y deja grados de libertad suficientes (series evaluables ≥84 F). "
            "Constante externalizada en config, no enterrada por modelo."
        ),
    },
    {
        "id": "scaling_leakage_free",
        "title": "Escalado ajustado SOLO en la ventana inicial",
        "module": "vp_model/feature_builder.py:fit_scaler",
        "rationale": (
            "Las redes torch operan mal sobre magnitudes de ~18,000 días. El Scaler "
            "se ajusta únicamente con la ventana de entrenamiento explícita y se "
            "invierte tras predecir: ajustarlo sobre la serie completa filtraría el "
            "futuro al pasado (leakage)."
        ),
    },
    {
        "id": "covariate_policy",
        "title": "Política de covariables explícita por familia de modelo",
        "module": "vp_model/config.py:COVARIATES",
        "rationale": (
            "Solo los árboles diferenciados reciben calendario (la campaña canónica "
            "se derivó así); rlinear y las NN van conscientemente sin covariables. "
            "'year' se conserva por procedencia de las cifras publicadas y está "
            "documentada como candidata a retiro en la próxima re-campaña."
        ),
    },
    {
        "id": "selection_fresh_mrmr",
        "title": "Selección FRESH (FDR) + des-redundancia mRMR del catálogo",
        "module": "vp_model/feature_select.py:select · experiments/build_fe_facts.py",
        "rationale": (
            "Con n=130–296 observaciones cada grado de libertad cuenta. El conjunto "
            "unido de características de caracterización (catch22 + descriptores) se "
            "filtra por relevancia con corrección Benjamini-Yekutieli y se colapsa la "
            "colinealidad conservando una representante por grupo (|Spearman|>0.9), "
            "contra la dificultad real de pronóstico de cada serie (MASE del campeón)."
        ),
    },
)


@dataclass(frozen=True)
class FeatureBuilder:
    """Compone las transformaciones de features para UN modelo del catálogo."""

    model_name: str

    @property
    def covariate_cols(self) -> tuple[str, ...]:
        return COVARIATES.get(self.model_name, ())

    def to_timeseries(self, series: pd.Series) -> TimeSeries:
        """Serie F cruda -> TimeSeries regular con la política de huecos documentada."""
        return models.to_timeseries(series)

    def covariates(self, ts: TimeSeries) -> TimeSeries | None:
        """Covariables futuras según la política del modelo (None si no aplica)."""
        if not self.covariate_cols:
            return None
        feats = preprocess.calendar_features(ts.time_index)[list(self.covariate_cols)]
        return TimeSeries.from_dataframe(feats)

    def fit_scaler(self, ts: TimeSeries, train_len: int) -> Scaler | None:
        """Scaler ajustado SOLO en ``ts[:train_len]`` (None si el modelo no escala)."""
        if self.model_name not in NEEDS_SCALING:
            return None
        scaler = Scaler()
        scaler.fit(ts[:train_len])
        return scaler

    def realized(self) -> dict:
        """Linaje exacto de features de este modelo (MLflow / fe_facts)."""
        hp_key = "trees" if self.model_name in DIFFERENCED else self.model_name
        lags = HYPERPARAMS.get(hp_key, {}).get("lags")
        return {
            "fe_version": FE_VERSION,
            "model": self.model_name,
            "covariates": list(self.covariate_cols),
            "differenced": self.model_name in DIFFERENCED,
            "scaled": self.model_name in NEEDS_SCALING,
            "lags": lags,
            "max_interpolable_gap": MAX_INTERPOLABLE_GAP,
        }


def demo() -> None:
    """Self-check: política por modelo + escalado sin leakage + linaje completo."""
    from vp_model import dataset

    ts = models.to_timeseries(dataset.load_series("mexico", "F3", "FAD"))

    tree = FeatureBuilder("xgboost")
    cov = tree.covariates(ts)
    assert cov is not None and list(cov.components) == list(tree.covariate_cols)
    assert tree.realized()["differenced"] and tree.realized()["lags"] == 24

    plain = FeatureBuilder("ets")
    assert plain.covariates(ts) is None and plain.fit_scaler(ts, 60) is None

    nn = FeatureBuilder("dlinear")
    sc = nn.fit_scaler(ts, 60)
    assert sc is not None
    z = sc.transform(ts)
    train_vals = z[:60].values()
    # el rango [0,1] del MinMax de darts se define en la ventana de train…
    assert abs(float(train_vals.max()) - 1.0) < 1e-9
    # …y la serie completa se sale de ese rango (prueba de que NO vio el futuro).
    assert float(z.values().max()) > 1.0 + 1e-6
    print(f"OK — builder v{FE_VERSION}: xgboost {len(tree.covariate_cols)} covariables; ets 0; scaler sin leakage")


if __name__ == "__main__":
    demo()
