"""Fuente única de configuración de la capa de modelado (sin hardcode disperso).

Reúne las constantes y los hiperparámetros que antes vivían repartidos en
``dataset``, ``eda``, ``preprocess``, ``models``, ``walkforward``, ``metrics`` e
``intervals``. Cambiar el protocolo de evaluación o un hiperparámetro se hace AQUÍ,
en un solo lugar. Módulo dependency-light (sin darts/torch) para que cualquiera
pueda importarlo sin pagar el costo del stack de modelado.
"""

from __future__ import annotations

import logging
import os
import random

# --- Dominio ---------------------------------------------------------------
SEASONAL_PERIOD = 12  # estacionalidad anual (año fiscal de visas de EE.UU.)
DAYS_PER_YEAR = 365.25  # conversión días -> años para las figuras
RANDOM_SEED = 42  # semilla única para todo lo estocástico

# AD3: el epoch t0 tiene UNA fuente (vp_data.config.BASE_EPOCH); antes `1975 +
# days/365.25` estaba tipeado a mano en 8+ scripts de figuras/deep — un cambio
# de epoch los habría dejado en silencio sobre la base vieja.
from vp_data.config import BASE_EPOCH  # noqa: E402  (re-export deliberado)

BASE_EPOCH_YEAR = int(BASE_EPOCH.split("-")[0])  # derivado, no tipeado (t0 es 1-ene)


def days_to_year(days):  # noqa: ANN001, ANN201 — acepta escalar/Series/ndarray por igual
    """días desde BASE_EPOCH -> año calendario fraccional (ejes de figuras)."""
    return BASE_EPOCH_YEAR + days / DAYS_PER_YEAR


PILOT_COUNTRIES = ("mexico", "india", "china", "philippines", "all_chargeability")
TABLES = ("FAD", "DFF")

# --- Catálogo de modelos ---------------------------------------------------
# Pool ampliado tras la investigación de 181 fuentes: además de los 8 originales se
# añaden el "centro parsimonioso" que gana este régimen (ETS damped, Theta, lineales)
# y candidatos modernos defendibles (DLinear/NLinear, GBMs, N-BEATS/N-HiTS/TiDE).
MODEL_NAMES = (
    "naive",
    "naive1",  # AI1: naïve no estacional (random walk) — piso honesto del pool
    "drift",  # AI1: random walk con deriva — piso honesto para series con tendencia
    "arima",
    "sarima",
    "prophet",  # originales estadísticos
    "ets",
    "theta",
    "kalman",  # nuevos estadísticos parsimoniosos
    "lstm",
    "deepar",
    "arima_lstm",  # redes recurrentes (originales)
    "dlinear",
    "nlinear",
    "nbeats",
    "nhits",
    "tide",  # MLP/lineales modernos
    "rlinear",  # regresión lineal (ridge, forma cerrada)
    "xgboost",
    "lightgbm",
    "catboost",  # árboles (predicen delta: fix de extrapolación)
    "tft",  # Temporal Fusion Transformer (panel/covariables; transformer defendible)
    "chronos",  # foundation model zero-shot (Amazon Chronos-Bolt): transferencia, sin entrenar
)
# AI5: bolt-base (~205M) replaces bolt-small — better zero-shot accuracy at the same
# zero-training cost; the pipeline cache is keyed by model name (ChronosForecaster).
CHRONOS_MODEL = "amazon/chronos-bolt-base"
# Modelos con muestreo probabilístico nativo (CRPS/PI distribucional). 'sarima' se construye
# como ARIMA estacional, así que también lo es en runtime (estaba ausente del set por omisión).
PROBABILISTIC = frozenset({"deepar", "arima", "sarima"})
# Modelos torch que operan sobre magnitudes grandes -> escalar (leakage-free).
NEEDS_SCALING = frozenset({"lstm", "deepar", "dlinear", "nlinear", "nbeats", "nhits", "tide", "tft"})
# Modelos baratos que se reentrenan en CADA paso del walk-forward (ventana expansible).
RETRAIN_EACH_STEP = frozenset(
    {
        "naive",
        "naive1",  # AI1
        "drift",  # AI1
        "arima",
        "sarima",
        "auto_arima",  # AI3: monthly retrain like arima/sarima (order fixed pre-hold-out)
        "prophet",
        "ets",
        "theta",
        "kalman",
        "rlinear",
        "xgboost",
        "lightgbm",
        "catboost",
    }
)
# Modelos de árboles que predicen la PRIMERA DIFERENCIA (delta mensual) en vez del
# nivel: los árboles no extrapolan fuera del rango de train y, sobre el nivel, se
# saturan al máximo histórico (bug confirmado). Diferenciar es el fix gratuito.
DIFFERENCED = frozenset({"xgboost", "lightgbm", "catboost"})
# AJ1: local NNs also predict the first difference. Their MinMax scaler is fitted
# ONCE on the initial window and never re-fitted, so on the level they spent ~15
# years predicting outside [0, 1]; the diff of the (affine-)scaled series stays
# bounded and stationary, which is what the nets can actually learn. Kept separate
# from DIFFERENCED (trees) because tune/feature-lineage treat that set as "the GBMs".
NN_DIFFERENCED = frozenset({"lstm", "deepar", "dlinear", "nlinear", "nbeats", "nhits", "tide", "tft"})
NN_RETRAIN = 12  # las redes se reentrenan cada N meses (coste/validez)
# AJ2: models whose forecast is a draw from a fitted likelihood. With the darts
# default num_samples=1 a SINGLE stochastic draw was serving as the point forecast;
# the walk-forward now samples the predictive distribution and uses its median.
LIKELIHOOD_MODELS = frozenset({"deepar", "tft"})
NUM_SAMPLES_POINT = 500  # samples drawn to form the median point forecast (AJ2)

# AD8: política de covariables POR MODELO — explícita, no un accidente del código.
# Hoy solo los árboles diferenciados reciben calendario (la campaña canónica se
# derivó así); rlinear y las NN van conscientemente sin covariables (añadirlas
# invalidaría las cifras publicadas). ⚠️ 'year' (monótona, no acotada) sobre un
# target diferenciado es un smell documentado: candidata a eliminarse en la
# PRÓXIMA re-campaña (PENDIENTES), no antes — provenance de las cifras vigentes.
COVARIATE_COLS = ("month_sin", "month_cos", "fiscal_sin", "fiscal_cos", "year")
COVARIATES: dict[str, tuple[str, ...]] = {m: COVARIATE_COLS for m in sorted(DIFFERENCED)}

# Hiperparámetros externalizados (antes hardcodeados dentro de build_model).
HYPERPARAMS: dict[str, dict] = {
    "arima": dict(p=2, d=1, q=2),
    "sarima": dict(p=1, d=1, q=1, seasonal_order=(1, 0, 1, SEASONAL_PERIOD)),
    # RNN estándar (lstm/deepar) y la variante corta de la cascada ARIMA-LSTM.
    "rnn": dict(input_chunk_length=24, training_length=36, hidden_dim=20, n_rnn_layers=1, n_epochs=60),
    "rnn_hybrid": dict(input_chunk_length=12, training_length=18, hidden_dim=20, n_epochs=60),
    # Árboles: lags + covariable de calendario; predicen delta (ver DIFFERENCED).
    # AJ5: learning_rate/n_estimators are the slow-learning BRIDGE defaults (library
    # defaults — e.g. xgboost lr=0.3 — overfit 60–270-month windows and made the
    # model-pool table compare a tuned ETS against untuned GBMs). When
    # reports/eval/tuned_params.json has a winning entry for (model, table), the
    # factory overrides these with the HPO winners (models._tree_params).
    "trees": dict(lags=24, lags_future_covariates=[0], output_chunk_length=1, learning_rate=0.02, n_estimators=200),
    # Regresión lineal (ridge): forma cerrada, sin SGD ni early-stopping (sin leakage).
    "rlinear": dict(lags=24, output_chunk_length=1),
    # MLP/lineales y MLP residuales modernos (torch): ventana de entrada modesta.
    "mlp": dict(input_chunk_length=24, output_chunk_length=1, n_epochs=60),
    # Temporal Fusion Transformer: índice relativo en vez de exigir covariables futuras.
    "tft": dict(
        input_chunk_length=24,
        output_chunk_length=1,
        hidden_size=16,
        lstm_layers=1,
        num_attention_heads=4,
        n_epochs=60,
        add_relative_index=True,
    ),
}

# --- Protocolo de validación walk-forward ----------------------------------
MIN_TRAIN = {"FAD": 60, "DFF": 36}  # ventana inicial por tabla
HOLDOUT = 24  # meses finales reservados (evaluación independiente)
MIN_BACKTEST_BUFFER = 6  # colchón extra para que una serie sea evaluable

# --- EDA / preprocesamiento / intervalos -----------------------------------
MIN_TRAINABLE_EVALUABLE = MIN_TRAIN["FAD"] + HOLDOUT  # 84: ventana + holdout
MAX_INTERPOLABLE_GAP = 3  # huecos <= 3 meses se interpolan; más largos, NaN
ALPHA = 0.05  # intervalos de predicción al 95%
# Banda de predicción al 80 % del demostrador web = half95 * BAND80_RATIO.
# La banda 80 % conforme directa sub-cubre (P80(|resid|) ≪ P97.5 con cola pesada).
# BAND80_RATIO se calibra en un split temporal DISJUNTO (las añadas BAND80_CAL_VINTAGES)
# y se VALIDA en las añadas restantes → la cobertura 80 % reportada es out-of-sample, NO
# circular. Re-derivar con `experiments/derive_band80_ratio.py` cuando crezca el histórico.
BAND80_RATIO = 0.4744
BAND80_CAL_VINTAGES = ("2024-07", "2025-01")  # añadas de calibración (excluidas del cov80 honesto)


def seed_everything(seed: int = RANDOM_SEED) -> None:
    """Siembra todas las fuentes de aleatoriedad para reproducibilidad bit a bit.

    Cubre ``random`` y ``PYTHONHASHSEED``; numpy y torch se siembran si están
    instalados (no se importan a la fuerza para mantener el módulo ligero).
    """
    os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    try:
        import numpy as np

        np.random.seed(seed)
    except ImportError:
        pass
    try:
        import torch

        torch.manual_seed(seed)
    except ImportError:
        pass


def run_metadata() -> dict:
    """Procedencia completa de una corrida, para reproducibilidad auditable (C3).

    Captura el commit git (y si el árbol está sucio), versiones de librerías, semilla,
    parámetros del walk-forward e hiperparámetros. Sin esto, una fila de resultados no
    puede atarse al código/config que la produjo.
    """
    import datetime
    import hashlib
    import importlib.metadata
    import subprocess
    import sys
    from pathlib import Path

    def _ver(pkg: str) -> str | None:
        try:
            return importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            return None

    def _git(*args: str) -> str | None:
        try:
            return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
        except subprocess.CalledProcessError, FileNotFoundError:
            return None

    def _md5(path: Path) -> str | None:
        # AO2: 12-hex md5, same convention as build_model_card._panel_hash so
        # lineage stays joinable across governance artifacts. git sha != data:
        # without these hashes a run could not be tied to the exact panel it saw.
        return hashlib.md5(path.read_bytes()).hexdigest()[:12] if path.exists() else None

    sha = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    root = Path(__file__).resolve().parent.parent
    return {
        "run_id": ts.replace(":", "").replace("-", "") + (f"-{sha[:7]}" if sha else ""),
        "timestamp": ts,
        "git_sha": sha,
        "git_dirty": bool(dirty) if dirty is not None else None,
        "python": sys.version.split()[0],
        "seed": RANDOM_SEED,
        "libs": {
            p: _ver(p) for p in ("darts", "torch", "xgboost", "prophet", "statsmodels", "scipy", "pandas", "duckdb")
        },
        "walkforward": {"min_train": MIN_TRAIN, "holdout": HOLDOUT, "nn_retrain": NN_RETRAIN},
        "hyperparams": HYPERPARAMS,
        # AD9: linaje de FE — sin esto una fila de métricas no puede atarse a las
        # features que la produjeron (la versión viene de feature_builder).
        "features": {
            "covariates": {m: list(c) for m, c in COVARIATES.items()},
            "differenced": sorted(DIFFERENCED),
            "nn_differenced": sorted(NN_DIFFERENCED),  # AJ1
            "scaled": sorted(NEEDS_SCALING),
            "max_interpolable_gap": MAX_INTERPOLABLE_GAP,
            "base_epoch": BASE_EPOCH,
        },
        # AO2: data lineage — the code sha alone cannot reproduce a run.
        "data_lineage": {
            "panel_parquet_md5": _md5(root / "data" / "processed" / "visa_panel_long.parquet"),
            "dvc_lock_md5": _md5(root / "dvc.lock"),
        },
    }


def get_logger(name: str) -> logging.Logger:
    """Logger de módulo con un handler propio en stderr y formato uniforme.

    Adjunta el handler al logger raíz del paquete (``vp_model``) y lo aísla de la
    cadena global (``propagate=False``) para que las líneas INFO de progreso no las
    silencie el ``lastResort`` ni la reconfiguración de logging de torch/lightning
    durante corridas largas. Idempotente.
    """
    import sys

    root = logging.getLogger("vp_model")
    if not any(getattr(h, "_vp_model", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler._vp_model = True  # type: ignore[attr-defined]  # marca de idempotencia
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False
    return logging.getLogger(name)
