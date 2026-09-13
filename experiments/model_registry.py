"""Registro canónico de modelos por **identificador lógico**. Una sola autoridad.

M74-E encontró **tres** listas independientes que ya habían divergido:

    save_finalists.py:38       LOCAL = (ets, theta, arima, kalman, catboost, lightgbm)      ← 6
    export_forecasts.py:25     LOCAL = (ets, theta, sarima, arima, kalman, catboost, …)    ← 7
    save_finalists_deep.py:24  DET   = (BiTCN, PatchTST, TiDE, NHITS)

`sarima` **se exportaba y no se persistía nunca**. No era una decisión: medido con
``fit → joblib.dump → joblib.load → historical_forecasts(retrain=False)``, SARIMA **sí es
persistible**; su ausencia era un defecto de inventario. Derivar las listas por AST de cada
productor habría trasladado la divergencia en vez de cerrarla, así que la autoridad es **este
registro** y las **seis vistas** se **derivan** de él.

⚠️ **La clave es el identificador lógico, no la clase.** `arima` y `sarima` son ambos
``darts.ARIMA`` con parámetros distintos; `ets` y `theta` son ``AutoETS``/``AutoTheta``, que no
conservan estado reutilizable. Agrupar por clase mezclaría cosas que el protocolo distingue.

⚠️ **Stdlib puro y sin dependencias del producto:** lo importan cuatro consumidores en DOS
intérpretes (`save_finalists_deep` corre en `ante_nf`, sin `vp_data`).
"""

from __future__ import annotations

from typing import Literal, NamedTuple

Family = Literal["local", "global"]
#: De dónde sale el pronóstico hold-out de cada modelo.
#:   recomputed  — el exportador lo reajusta y lo rueda (`historical_forecasts`)
#:   walkforward — se TRANSPORTA del walk-forward oficial (§8.6 / decisión (b))
#:   campaign    — se lee de `reports/campaign/global_{table}_camp_*.csv`
ForecastSource = Literal["recomputed", "walkforward", "campaign"]


class Model(NamedTuple):
    """Un modelo del catálogo, con su procedencia declarada."""

    name: str
    family: Family
    persisted: bool
    forecast_source: ForecastSource
    note: str = ""


#: ★ LA AUTORIDAD. Todo lo demás se deriva de aquí.
REGISTRY: tuple[Model, ...] = (
    # ── locales persistibles, con pronóstico recalculado por el exportador
    Model("sarima", "local", True, "recomputed", "clase ARIMA; persistibilidad MEDIDA en M74-E"),
    Model("arima", "local", True, "recomputed"),
    Model("kalman", "local", True, "recomputed"),
    Model("catboost", "local", True, "recomputed"),
    Model("lightgbm", "local", True, "recomputed"),
    # ── locales NO persistibles: AutoETS/AutoTheta no conservan estado reutilizable, así que su
    #    pronóstico se transporta del walk-forward oficial. NO son opcionales: su ausencia bloquea.
    Model("ets", "local", False, "walkforward", "AutoETS: historical_forecasts(retrain=False) falla"),
    Model("theta", "local", False, "walkforward", "AutoTheta: ídem"),
    # ── globales profundos: se persisten y su pronóstico viene de los CSV de la campaña
    Model("BiTCN", "global", True, "campaign"),
    Model("PatchTST", "global", True, "campaign"),
    Model("TiDE", "global", True, "campaign"),
    Model("NHITS", "global", True, "campaign"),
    Model("AutoBiTCN", "global", True, "campaign", "identidad de receta; la clase instanciada es BiTCN (§8.6.1)"),
)

# ─────────────────────────────────────────────────── las SEIS vistas, DERIVADAS
PERSISTED_MODELS: tuple[str, ...] = tuple(m.name for m in REGISTRY if m.persisted)
RECOMPUTED_FORECAST_MODELS: tuple[str, ...] = tuple(m.name for m in REGISTRY if m.forecast_source == "recomputed")
TRANSPORTED_FORECAST_MODELS: tuple[str, ...] = tuple(m.name for m in REGISTRY if m.forecast_source == "walkforward")
CAMPAIGN_FORECAST_MODELS: tuple[str, ...] = tuple(m.name for m in REGISTRY if m.forecast_source == "campaign")
GLOBAL_MODELS: tuple[str, ...] = tuple(m.name for m in REGISTRY if m.family == "global")
LOCAL_MODELS: tuple[str, ...] = tuple(m.name for m in REGISTRY if m.family == "local")
#: Unión EXACTA de las tres fuentes de pronóstico: lo que el exportador debe cubrir, sin excepción.
REQUIRED_FORECAST_MODELS: tuple[str, ...] = (
    *RECOMPUTED_FORECAST_MODELS,
    *TRANSPORTED_FORECAST_MODELS,
    *CAMPAIGN_FORECAST_MODELS,
)


class RegistryError(ValueError):
    """El registro es incoherente, o alguien preguntó por un modelo que no existe."""


def get(name: str) -> Model:
    """El modelo con ese identificador. Fail-closed: un desconocido no se asume nada."""
    for m in REGISTRY:
        if m.name == name:
            return m
    raise RegistryError(f"modelo desconocido: {name!r}. No está en el registro canónico y no se infiere su procedencia")


def validate() -> None:
    """Invariantes del registro. Se comprueban **al importar**: una autoridad rota no sirve."""
    nombres = [m.name for m in REGISTRY]
    if len(nombres) != len(set(nombres)):
        dup = sorted({n for n in nombres if nombres.count(n) > 1})
        raise RegistryError(f"identificadores duplicados: {dup}")
    if set(REQUIRED_FORECAST_MODELS) != set(nombres):
        falta = sorted(set(nombres) - set(REQUIRED_FORECAST_MODELS))
        raise RegistryError(f"sin fuente de pronóstico declarada: {falta}")
    if len(REQUIRED_FORECAST_MODELS) != len(set(REQUIRED_FORECAST_MODELS)):
        raise RegistryError("un modelo declara más de una fuente de pronóstico")
    if set(TRANSPORTED_FORECAST_MODELS) != {"ets", "theta"}:
        raise RegistryError(f"sólo ets y theta se transportan; hoy: {TRANSPORTED_FORECAST_MODELS}")
    if any(m.persisted for m in REGISTRY if m.forecast_source == "walkforward"):
        raise RegistryError("un modelo transportado no puede declararse persistible")
    # ★ `CAMPAIGN_FORECAST_MODELS` y `GLOBAL_MODELS` coinciden HOY. Esa igualdad se comprueba como
    # invariante en vez de mantenerse a mano en dos sitios: dos listas que casualmente coinciden es
    # exactamente la forma que tenían las tres autoridades antes de divergir por `sarima`.
    if set(CAMPAIGN_FORECAST_MODELS) != set(GLOBAL_MODELS):
        raise RegistryError(
            "los globales y los de fuente `campaign` dejaron de coincidir: "
            f"sólo global {sorted(set(GLOBAL_MODELS) - set(CAMPAIGN_FORECAST_MODELS))} · "
            f"sólo campaign {sorted(set(CAMPAIGN_FORECAST_MODELS) - set(GLOBAL_MODELS))}. "
            "Si es deliberado, decláralo en el registro; si no, es una divergencia naciente"
        )


validate()
