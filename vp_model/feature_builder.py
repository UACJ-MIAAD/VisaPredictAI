"""Motor de ingeniería de características de la capa de modelado (AD1).

Antes, las transformaciones que alimentan al catálogo completo de modelos
(``config.MODEL_NAMES``, 23 + extras) vivían inline y
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

from vp_model import missingness, models, preprocess
from vp_model.config import (
    COVARIATES,
    DIFFERENCED,
    HYPERPARAMS,
    MASK_COVARIATES,
    NEEDS_SCALING,
    NN_DIFFERENCED,
)

# 2.0.0 (US-F1, 12-jul-2026): CAMBIO DE CONTRATO — la rejilla de modelado pasa de
# interpolación lineal bidireccional (usaba el bracket FUTURO del hueco: fuga hacia
# los orígenes dentro del hueco) a relleno CAUSAL forward-only (LOCF,
# preprocess.to_regular_monthly_causal), y los árboles diferenciados reciben además
# las máscaras MNAR (observed/months_since_obs) a retardo −1. Las cifras publicadas
# de la era anterior se re-derivan en la campaña F2.
FE_VERSION = "2.0.0"

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
        "title": "Rejilla mensual regular con relleno causal (LOCF)",
        "module": "vp_model/preprocess.py:to_regular_monthly_causal",
        "rationale": (
            "Los modelos exigen índice regular; los meses C/U no son objetivo. Cada "
            "hueco se rellena hacia adelante con la última observación (LOCF): el "
            "valor de un mes faltante usa SOLO observaciones anteriores, de modo que "
            "ningún origen del walk-forward ve el bracket futuro del hueco (US-F1; la "
            "interpolación lineal bidireccional previa filtraba el futuro al pasado). "
            "El relleno de continuidad jamás se puntúa (máscara F-only B1)."
        ),
    },
    {
        "id": "missingness_mask_covariates",
        "title": "Máscaras MNAR (observed, months_since_obs) como covariables de los GBM",
        "module": "vp_model/missingness.py:masking_features · vp_model/config.py:MASK_COVARIATES",
        "rationale": (
            "La ausencia es señal (MNAR): los árboles diferenciados reciben la máscara "
            "de observación y los meses desde la última observación con retardo −1 (el "
            "último mes CERRADO, conocido en el origen; el régimen del mes objetivo no "
            "se conoce antes de publicarse el boletín y dárselo a retardo 0 sería "
            "fuga). Así el modelo puede descontar los tramos arrastrados por el LOCF "
            "en lugar de tomarlos por fechas publicadas; los modelos sin covariables "
            "usan la política forward-only explícita del relleno causal (US-F1)."
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
            "Solo los árboles diferenciados reciben covariables: calendario a retardo "
            "0 y máscaras MNAR a retardo −1 (US-F1); rlinear y las NN van "
            "conscientemente sin covariables. 'year' (monótona, no acotada sobre "
            "target diferenciado) fue RETIRADA en la re-campaña AQ del 4-jul-2026 "
            "(PENDIENTES #12)."
        ),
    },
    {
        "id": "selection_fresh_mrmr",
        "title": "Selección FRESH (FDR) + des-redundancia mRMR del catálogo",
        "module": "vp_model/feature_select.py:select · experiments/build_fe_facts.py",
        "rationale": (
            "Con series de longitud corta (decenas a cientos de observaciones) cada grado de libertad cuenta. El conjunto "
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

    @property
    def mask_covariate_cols(self) -> tuple[str, ...]:
        """F1: máscaras MNAR (observed/months_since_obs) según la política del modelo."""
        return MASK_COVARIATES.get(self.model_name, ())

    def to_timeseries(self, series: pd.Series) -> TimeSeries:
        """Serie F cruda -> TimeSeries regular con la política de huecos CAUSAL (US-F1)."""
        return models.to_timeseries(series)

    def covariates(self, ts: TimeSeries, raw: pd.Series | None = None, horizon: int = 0) -> TimeSeries | None:
        """Covariables futuras según la política del modelo (None si no aplica).

        Calendario (determinista, retardo 0) + máscaras MNAR (retardo −1, US-F1).
        Las máscaras se derivan de la serie F CRUDA (``raw``): la serie rellenada ya
        no sabe qué meses fueron observados. Fail-loud: si la política del modelo
        exige máscaras y no se pasa ``raw``, se levanta ValueError — degradar en
        silencio dejaría al modelo sin la señal de ausencia sin dejar rastro.
        ``horizon`` extiende la rejilla hacia el futuro para los caminos que
        predicen más allá del último boletín (``predict(n)``).
        """
        if not self.covariate_cols and not self.mask_covariate_cols:
            return None
        idx = ts.time_index
        if horizon > 0:
            ext = pd.date_range(idx[-1] + pd.offsets.MonthBegin(1), periods=horizon, freq="MS")
            idx = idx.append(ext)
        parts: list[pd.DataFrame] = []
        if self.covariate_cols:
            parts.append(preprocess.calendar_features(idx)[list(self.covariate_cols)])
        if self.mask_covariate_cols:
            if raw is None:
                raise ValueError(
                    f"F1: la política de covariables de '{self.model_name}' incluye máscaras MNAR "
                    "(observed/months_since_obs) — pasa la serie F cruda: covariates(ts, raw)"
                )
            masks = missingness.masking_features(raw, horizon=horizon)[list(self.mask_covariate_cols)]
            parts.append(masks.reindex(idx))
        feats = pd.concat(parts, axis=1)
        assert not feats.isna().any().any(), "covariates: la rejilla de máscaras no cubre el índice de la serie"
        return TimeSeries.from_dataframe(feats)

    def fit_scaler(self, ts: TimeSeries, train_len: int) -> Scaler | None:
        """Scaler ajustado SOLO en ``ts[:train_len]`` (None si el modelo no escala)."""
        if self.model_name not in NEEDS_SCALING:
            return None
        scaler = Scaler()
        scaler.fit(ts[:train_len])
        return scaler

    def realized(self) -> dict:
        """Linaje exacto de features de este modelo (MLflow / fe_facts).

        ``differenced`` cubre las DOS familias que predicen Δy: los árboles
        (``DIFFERENCED``) y las NN locales (``NN_DIFFERENCED``, AJ1) — antes el
        linaje reportaba ``differenced=False`` para las NN porque solo conocía
        el set de árboles. ``differenced_family`` conserva la distinción.
        """
        hp_key = "trees" if self.model_name in DIFFERENCED else self.model_name
        lags = HYPERPARAMS.get(hp_key, {}).get("lags")
        family = "trees" if self.model_name in DIFFERENCED else ("nn" if self.model_name in NN_DIFFERENCED else None)
        return {
            "fe_version": FE_VERSION,
            "model": self.model_name,
            "covariates": list(self.covariate_cols),
            "mask_covariates": list(self.mask_covariate_cols),  # F1
            "differenced": self.model_name in DIFFERENCED | NN_DIFFERENCED,
            "differenced_family": family,
            "scaled": self.model_name in NEEDS_SCALING,
            "lags": lags,
            # F1: la rejilla de modelado es LOCF causal; el cap de interpolación
            # (max_interpolable_gap) quedó confinado a la rejilla descriptiva del EDA.
            "gap_policy": "locf_causal",
        }


def demo() -> None:
    """Self-check: política por modelo + escalado sin leakage + linaje completo."""
    from vp_model import dataset

    raw = dataset.load_series("mexico", "F3", "FAD")
    ts = models.to_timeseries(raw)

    tree = FeatureBuilder("xgboost")
    cov = tree.covariates(ts, raw)
    assert cov is not None and list(cov.components) == list(tree.covariate_cols) + list(tree.mask_covariate_cols)
    assert tree.realized()["differenced"] and tree.realized()["lags"] == 24
    assert tree.realized()["gap_policy"] == "locf_causal"  # F1

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
