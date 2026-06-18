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

PILOT_COUNTRIES = ("mexico", "india", "china", "philippines", "all_chargeability")
TABLES = ("FAD", "DFF")

# --- Catálogo de modelos ---------------------------------------------------
# Pool ampliado tras la investigación de 181 fuentes: además de los 8 originales se
# añaden el "centro parsimonioso" que gana este régimen (ETS damped, Theta, lineales)
# y candidatos modernos defendibles (DLinear/NLinear, GBMs, N-BEATS/N-HiTS/TiDE).
MODEL_NAMES = (
    "naive",
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
CHRONOS_MODEL = "amazon/chronos-bolt-small"  # foundation zero-shot (ruteado por su clase wrapper)
# Modelos con muestreo probabilístico nativo (CRPS/PI distribucional). 'sarima' se construye
# como ARIMA estacional, así que también lo es en runtime (estaba ausente del set por omisión).
PROBABILISTIC = frozenset({"deepar", "arima", "sarima"})
# Modelos torch que operan sobre magnitudes grandes -> escalar (leakage-free).
NEEDS_SCALING = frozenset({"lstm", "deepar", "dlinear", "nlinear", "nbeats", "nhits", "tide", "tft"})
# Modelos baratos que se reentrenan en CADA paso del walk-forward (ventana expansible).
RETRAIN_EACH_STEP = frozenset(
    {
        "naive",
        "arima",
        "sarima",
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
NN_RETRAIN = 12  # las redes se reentrenan cada N meses (coste/validez)

# Hiperparámetros externalizados (antes hardcodeados dentro de build_model).
HYPERPARAMS: dict[str, dict] = {
    "arima": dict(p=2, d=1, q=2),
    "sarima": dict(p=1, d=1, q=1, seasonal_order=(1, 0, 1, SEASONAL_PERIOD)),
    # RNN estándar (lstm/deepar) y la variante corta de la cascada ARIMA-LSTM.
    "rnn": dict(input_chunk_length=24, training_length=36, hidden_dim=20, n_rnn_layers=1, n_epochs=60),
    "rnn_hybrid": dict(input_chunk_length=12, training_length=18, hidden_dim=20, n_epochs=60),
    # Árboles: lags + covariable de calendario; predicen delta (ver DIFFERENCED).
    "trees": dict(lags=24, lags_future_covariates=[0], output_chunk_length=1),
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
    import importlib.metadata
    import subprocess
    import sys

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

    sha = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    ts = datetime.datetime.now().isoformat(timespec="seconds")
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
