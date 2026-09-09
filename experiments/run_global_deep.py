"""Entrenamiento GLOBAL a escala de modelos profundos (mandato nocturno).

Objetivo: vencer a los parsimoniosos (ETS/Theta 0.118 FAD) con deep learning entrenado
GLOBALMENTE sobre el panel apilado (Montero-Manso & Hyndman 2021) — la estrategia que en
el smoke rescató a los profundos (NHITS 2.5 local → 0.13 global). Corre en el venv
aislado ``ante_nf`` (neuralforecast, pandas<3).

Dos variantes por su efecto sobre la tendencia fuerte:
  * NIVELES: y tal cual (el escalado estándar no resuelve la extrapolación de tendencia).
  * DIFERENCIADO (--diff): se entrena sobre Δy y se REINTEGRA a nivel con el último real
    (1 paso, leakage-free), igual que el truco que hizo ganar a CatBoost sobre árboles.

Modelos univariados-globales (toleran series de distinta longitud): NHITS, PatchTST,
DeepAR (probabilístico), TiDE, BiTCN, KAN, TimeMixer, NBEATSx, TimesNet. Cada uno se
entrena aislado (un fallo no aborta el resto). Validación = ``cross_validation`` con
n_windows=24, step=1, refit=False → idéntica al hold-out de 24 meses del pool local.

Salida: ``reports/campaign/global_{table}_{levels|diff}.csv`` (unique_id, ds, y, <modelos>),
SIEMPRE en espacio de NIVEL (la variante diff ya viene reintegrada), que el entorno
principal evalúa con ``eval_neuralforecast`` usando las MISMAS métricas.

HPO (AK8): ``--auto`` corre UNA búsqueda Optuna por modelo con espacios que
incluyen ARQUITECTURA + early stopping como presupuesto real, y persiste la
config ganadora en ``reports/campaign/hpo_deep_best_{table}_{modelo}.json``;
``--config`` re-entrena esa ganadora determinística con ``--seed`` (búsqueda 1x
+ 5 re-entrenos de semilla, en vez de 5 búsquedas independientes).

Uso:  ante_nf/bin/python experiments/run_global_deep.py --table FAD --block both [--diff] [--max-steps 1000] [--fast]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
# AK8e: vp_model.config / vp_data.config son dependency-light (stdlib) e importables
# también desde el venv aislado ante_nf — mueren los hardcodes de HOLDOUT/BASE.
# F1: vp_model.preprocess también es dependency-light (numpy+pandas) — la rejilla
# causal LOCF es la función CANÓNICA, no una réplica.
sys.path.insert(0, str(ROOT))
from vp_data.config import BASE_EPOCH  # noqa: E402
from vp_model.config import HOLDOUT  # noqa: E402
from vp_model.deck import cargar_deck  # noqa: E402
from vp_model.preprocess import to_regular_monthly_causal  # noqa: E402

PANEL = ROOT / "data" / "processed" / "visa_panel_long.parquet"
COHORTS = ROOT / "reports" / "eval" / "series_cohorts.json"
PILOT = ("mexico", "india", "china", "philippines", "all_chargeability")
BASE = pd.Timestamp(BASE_EPOCH)  # t0 de days_since_base (build_panel)
MAX_STEPS_HPO = 2000  # techo fijo alto: el presupuesto REAL es el early stopping (AK8b)
EARLY_STOP_PATIENCE = 10  # checks de validación sin mejora antes de parar (AK8b)
VAL_CHECK_STEPS = 25  # pasos de entrenamiento entre checks de validación (AK8b)
VAL_SIZE = 12  # meses de validación por serie para el early stopping (solo HPO/re-entreno)


def encode_regime(g: pd.DataFrame) -> pd.Series:
    """Codifica el régimen C/F/U como UNA serie continua (en vez de tirar huecos o interpolar).

    * F → ``days_since_base`` de la fecha de prioridad (cutoff real).
    * C (Current) → ``days_since_base`` del MES DEL BOLETÍN: "Current" significa que el cutoff
      es el presente, así que el valor real es la fecha del propio boletín (no una rampa sintética).
      Captura la dinámica real (p. ej. la retrogresión Current→F) en lugar de inventarla.
    * U/UNK → se dejan NaN: ``regular_monthly`` los rellena con LOCF causal (F1).
      (Semánticamente U = espera infinita = cutoff mínimo; trato dedicado en empleo.)
    """
    g = g.sort_values("ds")
    y = g["days_since_base"].astype("float64").to_numpy().copy()
    is_c = (g["status"] == "C").to_numpy()
    y[is_c] = (g["ds"] - BASE).dt.days.to_numpy().astype("float64")[is_c]
    return pd.Series(y, index=pd.DatetimeIndex(g["ds"]))


def regular_monthly(s: pd.Series) -> pd.Series:
    """Serie mensual regular con relleno CAUSAL (LOCF forward-only, F1).

    MISMA política que ``models.to_timeseries`` (delega en la función canónica
    ``vp_model.preprocess.to_regular_monthly_causal``): todo mes de hueco arrastra
    la ÚLTIMA observación anterior, sin tope — mutar cualquier valor posterior a un
    origen no cambia ningún insumo de entrenamiento en/antes de ese origen. Los NaN
    de ``encode_regime`` (meses U/UNK) se descartan antes de regridear. La versión
    previa (interpolación bidireccional <=3 meses + corte del segmento en huecos
    largos) queda solo como evidencia congelada de la campaña F1.
    """
    return to_regular_monthly_causal(s.dropna())


def cohorte_uids(table: str, cohort: str, ruta: Path = COHORTS) -> set[str]:
    """``unique_id`` del universo EVALUABLE de E1 para esa tabla y cohorte.

    Siempre devuelve un conjunto; quien decide si restringir es el llamador, según
    ``--universe``. Con ``--universe evaluable`` el lane solo ve las series del catálogo
    congelado de E1, así que una serie ajena a la cohorte no puede colarse ni en el
    entrenamiento ni en la evaluación.
    """
    catalogo = json.loads(ruta.read_text())["series"]
    return {
        f"{s['country']}/{s['block']}/{s['category']}"
        for s in catalogo
        if s["table"] == table and (cohort == "all" or s["cohort"] == cohort)
    }


def load_panel(table: str, block: str, *, cohort: str = "all", universe: str = "pilot") -> pd.DataFrame:
    """Panel largo neuralforecast (unique_id, ds, y), F-only, mensual regular.

    ``block='both'`` apila familiar + empleo (más series → mejor aprendizaje global).
    Solo entran series con largo suficiente para la ventana + hold-out.
    """
    df = pd.read_parquet(PANEL)
    blocks = ("family", "employment") if block == "both" else (block,)
    # NO se filtra a status==F: necesitamos las celdas C (y U) para codificar el régimen y
    # darle continuidad REAL a la serie. La EVALUACIÓN sigue siendo F-only (hold-out = fechas).
    df = df[(df["table"] == table) & (df["block"].isin(blocks)) & (df["country"].isin(PILOT))].copy()
    df["unique_id"] = df["country"] + "/" + df["block"] + "/" + df["category"]
    df["ds"] = pd.to_datetime(df["bulletin_date"])
    min_len = (60 if table == "FAD" else 36) + HOLDOUT + 6
    permitidos = cohorte_uids(table, cohort) if universe == "evaluable" else None
    if permitidos is not None:
        df = df[df["unique_id"].isin(permitidos)]
    out = []
    for uid, g in df.groupby("unique_id"):
        # exige un mínimo de meses F REALES (no solo largo C-encoded): una serie con <24 F
        # no puede llenar el hold-out ni dar una escala naïve fiable (p. ej. china/EB5_TEA, 5 F).
        if (g["status"] == "F").sum() < HOLDOUT:
            continue
        s = regular_monthly(encode_regime(g[["ds", "status", "days_since_base"]]))
        if len(s) >= min_len:
            out.append(pd.DataFrame({"unique_id": uid, "ds": s.index, "y": s.to_numpy()}))
    return pd.concat(out, ignore_index=True)


def _accelerator() -> str:
    """AK8d: intenta MPS (Apple Silicon) con fallback documentado a CPU.

    ``VP_DEEP_ACCEL=cpu`` fuerza CPU si MPS falla (torch/MPS en macOS ha dado
    segfaults con multi-hilo antes; ``set_num_threads(1)`` se mantiene igual).
    """
    forced = os.environ.get("VP_DEEP_ACCEL")
    if forced:
        return forced
    import torch

    return "mps" if torch.backends.mps.is_available() else "cpu"


def _build_models(input_size: int, max_steps: int, n_series: int, seed: int = 1):
    """Conjunto de modelos univariados-globales (uno por NeuralForecast, aislados)."""
    from neuralforecast.losses.pytorch import DistributionLoss
    from neuralforecast.models import (
        KAN,
        NHITS,
        BiTCN,
        DeepAR,
        PatchTST,
        TiDE,
        TimeMixer,
        TimesNet,
    )

    c = dict(
        h=1,
        input_size=input_size,
        max_steps=max_steps,
        scaler_type="standard",
        random_seed=seed,
        enable_progress_bar=False,
        enable_model_summary=False,
        logger=False,  # sin lightning_logs/ huérfanos en la raíz (T2)
        accelerator=_accelerator(),  # AK8d: mps si está disponible, cpu si no
    )
    builders = {
        "NHITS": lambda: NHITS(**c),
        "PatchTST": lambda: PatchTST(**c),
        "DeepAR": lambda: DeepAR(**c, loss=DistributionLoss(distribution="Normal", level=[95])),
        "TiDE": lambda: TiDE(**c),
        "BiTCN": lambda: BiTCN(**c),
        "KAN": lambda: KAN(**c),
        "TimeMixer": lambda: TimeMixer(**c, n_series=n_series),
        "TimesNet": lambda: TimesNet(**c),
    }
    return builders


def build_recipe(receta, input_size: int, seed: int):
    """Construye EXACTAMENTE el modelo que declara la baraja. Nada más entra por aquí.

    El acelerador se fija a ``cpu`` a mano: esta máquina tiene MPS y ``_accelerator()`` lo
    elegiría, pero la campaña de E3 es de CPU y eso se comprueba en las pruebas.
    """
    from neuralforecast.losses.pytorch import MAE, DistributionLoss
    from neuralforecast.models import NHITS, BiTCN, DeepAR, PatchTST, TiDE

    p = receta.params
    comun = dict(
        h=1,
        input_size=input_size,
        max_steps=int(p.get("max_steps") or 1000),
        scaler_type=p.get("scaler_type") or "standard",
        random_seed=seed,
        enable_progress_bar=False,
        enable_model_summary=False,
        logger=False,
        accelerator="cpu",
    )
    for clave in ("learning_rate", "early_stop_patience_steps", "gradient_clip_val"):
        if p.get(clave) is not None:
            comun[clave] = p[clave]
    if p.get("valid_loss") == "MAE":
        comun["valid_loss"] = MAE()
    clases = {"DeepAR": DeepAR, "BiTCN": BiTCN, "PatchTST": PatchTST, "TiDE": TiDE, "NHITS": NHITS}
    if receta.model not in clases:
        raise ValueError(f"la baraja pide un modelo que este corredor no construye: {receta.model}")
    if receta.model == "DeepAR":
        distro = p.get("distribution") or "Normal"
        return clases[receta.model](**comun, loss=DistributionLoss(distribution=distro, level=[95]))
    comun.pop("valid_loss", None)
    return clases[receta.model](**comun)


def _base_config(trial):
    """Núcleo del espacio de búsqueda (optimización + presupuesto por early stop, AK8b).

    ``max_steps`` es un techo fijo alto: el presupuesto REAL lo controla el
    early stopping (paciencia x val_check_steps), no una malla de max_steps.
    """
    return {
        "input_size": trial.suggest_categorical("input_size", [18, 24, 36]),
        "learning_rate": trial.suggest_float("learning_rate", 1e-4, 1e-2, log=True),
        "scaler_type": trial.suggest_categorical("scaler_type", ["standard", "robust"]),
        "max_steps": MAX_STEPS_HPO,
        "early_stop_patience_steps": EARLY_STOP_PATIENCE,
        "val_check_steps": VAL_CHECK_STEPS,
        "logger": False,  # el config del trial es la única fuente de kwargs de los Auto* (T2)
        "accelerator": _accelerator(),  # AK8d
    }


# AK8a: espacios CON arquitectura por modelo — antes los 4 Auto* compartían un
# espacio de solo-optimización (lr/steps/scaler) y jamás movían la arquitectura.
_NHITS_POOLS = {"2-2-1": [2, 2, 1], "4-4-1": [4, 4, 1], "8-4-1": [8, 4, 1], "16-8-1": [16, 8, 1]}
_NHITS_FREQS = {"12-4-1": [12, 4, 1], "24-12-1": [24, 12, 1], "1-1-1": [1, 1, 1]}


def _cfg_bitcn(trial):
    # NOTE: this neuralforecast BiTCN takes no kernel_size — unknown keys leak into
    # pl.Trainer kwargs and blow up every trial (caught live in the AQ campaign).
    return _base_config(trial) | {
        "hidden_size": trial.suggest_categorical("hidden_size", [8, 16, 32]),
        "dropout": trial.suggest_float("dropout", 0.0, 0.3),
    }


def _mapped(mapping: dict, suggested):
    """Map a suggested KEY back to its list value, tolerating NF's MockTrial.

    neuralforecast's optuna backend introspects the config fn at __init__ with a
    MockTrial whose ``suggest_categorical`` returns the CHOICES LIST itself — the
    plain dict lookup then raised "cannot use 'list' as a dict key" (caught live
    in the AQ campaign). Lists become tuples so downstream hashing is safe.
    """
    if isinstance(suggested, (list, tuple)):
        suggested = suggested[0]
    return tuple(mapping[suggested])


def _cfg_nhits(trial):
    # Optuna no acepta listas como categóricas: se sugiere la LLAVE y se mapea.
    return _base_config(trial) | {
        "n_pool_kernel_size": _mapped(_NHITS_POOLS, trial.suggest_categorical("pool_key", list(_NHITS_POOLS))),
        "n_freq_downsample": _mapped(_NHITS_FREQS, trial.suggest_categorical("freq_key", list(_NHITS_FREQS))),
    }


def _cfg_tide(trial):
    return _base_config(trial) | {
        "hidden_size": trial.suggest_categorical("hidden_size", [32, 64, 128]),
        "decoder_output_dim": trial.suggest_categorical("decoder_output_dim", [8, 16, 32]),
        "dropout": trial.suggest_float("dropout", 0.0, 0.3),
    }


def _cfg_patchtst(trial):
    return _base_config(trial) | {
        "hidden_size": trial.suggest_categorical("hidden_size", [16, 32, 64]),
        "n_heads": trial.suggest_categorical("n_heads", [2, 4]),
        "patch_len": trial.suggest_categorical("patch_len", [4, 6, 8]),
        "dropout": trial.suggest_float("dropout", 0.0, 0.3),
    }


_AUTO_CONFIGS = {
    "AutoBiTCN": _cfg_bitcn,
    "AutoNHITS": _cfg_nhits,
    "AutoTiDE": _cfg_tide,
    "AutoPatchTST": _cfg_patchtst,
}


def _build_auto_models(num_samples: int, seed: int = 1):
    """Modelos Auto* (HPO Optuna interna, validación leakage-free) para vencer el listón."""
    from neuralforecast.auto import AutoBiTCN, AutoNHITS, AutoPatchTST, AutoTiDE
    from neuralforecast.losses.pytorch import MAE

    def mk(M, cfg):
        return lambda: M(
            h=1,
            loss=MAE(),
            config=cfg,
            num_samples=num_samples,
            backend="optuna",
            search_alg=_optuna_sampler(seed),
            verbose=False,
        )

    classes = {"AutoBiTCN": AutoBiTCN, "AutoPatchTST": AutoPatchTST, "AutoNHITS": AutoNHITS, "AutoTiDE": AutoTiDE}
    return {name: mk(cls, _AUTO_CONFIGS[name]) for name, cls in classes.items()}


# Llaves del config ganador que NO son kwargs del constructor determinista (o que el
# re-entreno fija por su cuenta): h/loss vienen del builder; las llaves *_key son los
# alias categóricos que _cfg_nhits mapea a listas.
_CONFIG_DROP = {"h", "loss", "valid_loss", "pool_key", "freq_key"}


def _dump_best_config(nf, name: str, table: str) -> None:
    """AK8c: persiste la config ganadora del Auto* + su historial de trials.

    ``reports/campaign/hpo_deep_best_{table}_{name}.json`` alimenta los
    re-entrenos multi-semilla (``--config``); el CSV de trials es procedencia.
    """
    out_dir = ROOT / "reports" / "campaign"
    try:
        study = nf.models[0].results  # backend optuna -> optuna.Study
        best = dict(study.best_trial.user_attrs.get("ALL_PARAMS", {})) or dict(study.best_params)
        # Fallback path loses the mapped NHITS architecture silently (audit): the
        # *_key aliases are in best_params but their list values are not — remap.
        if "pool_key" in best and "n_pool_kernel_size" not in best:
            best["n_pool_kernel_size"] = list(_NHITS_POOLS[best["pool_key"]])
        if "freq_key" in best and "n_freq_downsample" not in best:
            best["n_freq_downsample"] = list(_NHITS_FREQS[best["freq_key"]])
        best = {
            k: v
            for k, v in best.items()
            if k not in _CONFIG_DROP and (v is None or isinstance(v, (bool, int, float, str, list, tuple)))
        }
        (out_dir / f"hpo_deep_best_{table}_{name}.json").write_text(json.dumps(best, indent=2))
        study.trials_dataframe().to_csv(out_dir / f"hpo_deep_trials_{table}_{name}.csv", index=False)
        print(f"  · config ganadora -> hpo_deep_best_{table}_{name}.json ({len(study.trials)} trials)")
    except Exception as e:  # noqa: BLE001 — el dump no debe tirar la corrida
        print(f"  · WARN sin dump de config para {name}: {type(e).__name__}: {str(e)[:100]}")


def _build_from_config(names: list[str], template: str, seed: int):
    """AK8c: re-construye el ganador del HPO determinísticamente para una semilla.

    ``template`` es la ruta del JSON con ``{model}`` como placeholder (p. ej.
    ``reports/campaign/hpo_deep_best_FAD_Auto{model}.json``). La columna de
    salida CONSERVA el nombre Auto* para que la agregación multi-semilla
    (``aggregate_seeds --model AutoBiTCN --prefix camp_auto_s``) y los contratos
    de key_facts sigan intactos: el re-entreno ES el producto del pipeline Auto.
    """
    from neuralforecast.models import NHITS, BiTCN, PatchTST, TiDE

    classes = {"BiTCN": BiTCN, "NHITS": NHITS, "TiDE": TiDE, "PatchTST": PatchTST}
    builders = {}
    for name in names:
        path = Path(template.format(model=name))
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            print(f"  ✗ sin config ganadora para {name} ({path.name}) — se omite")
            continue
        cfg = {k: v for k, v in json.loads(path.read_text()).items() if k not in _CONFIG_DROP}
        params = {
            **cfg,
            "h": 1,
            "random_seed": seed,
            "enable_progress_bar": False,
            "enable_model_summary": False,
            "logger": False,
            "accelerator": _accelerator(),
            "early_stop_patience_steps": EARLY_STOP_PATIENCE,
            "val_check_steps": VAL_CHECK_STEPS,
        }
        builders[f"Auto{name}"] = lambda cls=classes[name], p=params: cls(**p)
    return builders


def _optuna_sampler(seed: int):
    """Sampler TPE con semilla → la búsqueda Optuna varía por --seed (multi-semilla real)."""
    import optuna

    return optuna.samplers.TPESampler(seed=seed, multivariate=True)


def _escribir_receipt(args, deck, receta, panel, merged, salida: Path, estado: dict, t0: float) -> Path:
    """Recibo del lane: qué se corrió, con qué, cuánto costó y qué quedó escrito.

    Un fallo o una no convergencia también se escriben: son resultado, no un intento fallido que
    se repita en silencio.
    """
    import platform
    import resource

    import torch

    escritos = [
        {
            "path": str(f.resolve().relative_to(ROOT)),
            "bytes": f.stat().st_size,
            "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
        }
        for f in [salida]
        if f.exists()
    ]
    recibo = {
        "lane": f"{args.recipe or 'legacy-cli'}|{args.table}|{args.cohort}",
        "recipe": args.recipe,
        "recipe_role": receta.role if receta else None,
        "recipe_model": receta.model if receta else None,
        "recipe_space": receta.space if receta else None,
        "recipe_params": receta.params if receta else None,
        "deck_version": deck.version,
        "cohort": args.cohort,
        "table": args.table,
        "block": args.block,
        "universe": args.universe,
        "seed": args.seed,
        "device": "cpu",
        "accelerator_requested": "cpu",
        "cuda_available": bool(torch.cuda.is_available()),
        "mps_available": bool(torch.backends.mps.is_available()),
        "torch_num_threads": torch.get_num_threads(),
        "n_series": int(panel["unique_id"].nunique()),
        "n_rows_panel": int(len(panel)),
        "n_rows_written": int(len(merged)),
        "inputs": deck.inputs,
        "env": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "torch": torch.__version__,
            "prefix": sys.prefix,
        },
        "lock": {
            "path": "locks/deep-macos-arm64.txt",
            "sha256": deck.inputs.get("locks/deep-macos-arm64.txt"),
        },
        "timing": {"seconds": round(time.time() - t0, 3)},
        "rss_max_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "status": estado["status"],
        "models": estado["models"],
        "warnings": sorted(set(estado["warnings"])),
        "exception": estado["exception"],
        "outputs": escritos,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(recibo, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"receipt {args.receipt.name}: status={recibo['status']} {recibo['timing']['seconds']}s")
    return args.receipt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="FAD")
    ap.add_argument("--block", default="both", choices=["family", "employment", "both"])
    ap.add_argument("--diff", action="store_true", help="entrenar sobre Δy y reintegrar")
    ap.add_argument("--max-steps", type=int, default=1000)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--models", nargs="+", default=None)
    ap.add_argument("--local-scaler", action="store_true", help="normaliza cada serie (escala mixta/DeepAR)")
    ap.add_argument("--auto", action="store_true", help="usa AutoModels (HPO Optuna)")
    ap.add_argument("--num-samples", type=int, default=40, help="trials de HPO por modelo Auto (AK8c: 1 búsqueda)")
    ap.add_argument("--suffix", default=None, help="sufijo del CSV de salida (p.ej. 'auto', 'ls')")
    ap.add_argument("--seed", type=int, default=1, help="semilla (init de pesos + sampler Optuna)")
    ap.add_argument("--cohort", default="all", choices=["estable", "no_estable", "all"])
    ap.add_argument("--universe", default="pilot", choices=["pilot", "evaluable"])
    ap.add_argument("--recipe", default=None, help="receta de docs/cohort_deck.json (lane de E3)")
    ap.add_argument("--receipt", type=Path, default=None, help="ruta de la receipt del lane")
    ap.add_argument(
        "--config",
        default=None,
        help="plantilla JSON del ganador del HPO con {model} (AK8c): re-entrena determinístico con --seed",
    )
    args = ap.parse_args()

    import torch

    torch.set_num_threads(1)  # py3.14/macOS: evita segfault multihilo (igual que el env principal)
    from neuralforecast import NeuralForecast

    print("gap_policy=locf_causal (F1)")  # procedencia de campaña: rejilla causal LOCF
    deck = cargar_deck() if args.recipe or args.receipt else None
    receta = deck.receta(args.recipe) if (deck and args.recipe) else None
    congelado = deck.frozen if deck else {}
    if receta is not None:
        # La receta manda: el espacio de entrenamiento sale de la baraja, no de la línea de
        # órdenes, para que un lane no pueda correr en un espacio distinto del pre-registrado.
        args.diff = receta.space == "diff"
        args.models = None
        args.auto = False
        args.config = None
    panel = load_panel(args.table, args.block, cohort=args.cohort, universe=args.universe)
    uids = panel["unique_id"].nunique()
    print(f"panel: {uids} series, {len(panel)} filas ({args.table}/{args.block}), diff={args.diff}")

    # Espacio de entrenamiento: nivel o primera diferencia (guardando el nivel para reintegrar).
    level = panel.copy()
    train = panel.copy()
    if args.diff:
        parts = []
        for _uid, g in panel.groupby("unique_id"):
            g = g.sort_values("ds").copy()
            # Contrato canónico = vp_model.preprocess.difference/undifference (AD2);
            # re-tipeado aquí porque este venv no tiene vp_model (test lo ancla).
            g["y"] = g["y"].diff()
            parts.append(g.iloc[1:])  # descarta el primer NaN
        train = pd.concat(parts, ignore_index=True)

    input_size = 36 if args.table == "FAD" else 18
    max_steps = 5 if args.fast else args.max_steps
    if args.config:  # AK8c: re-entreno determinista del ganador del HPO (columna Auto*)
        builders = _build_from_config(args.models or ["BiTCN", "TiDE", "NHITS"], args.config, args.seed)
    elif args.auto:
        builders = _build_auto_models(2 if args.fast else args.num_samples, args.seed)
    elif receta is not None:
        builders = {receta.name: lambda: build_recipe(receta, input_size, args.seed)}
    else:
        builders = _build_models(input_size, max_steps, uids, args.seed)
    if args.models and not args.config:
        builders = {k: v for k, v in builders.items() if k in args.models}
    local = "standard" if args.local_scaler else None
    # AK8b: el early stopping (Auto* y re-entrenos --config) exige una cola de
    # validación por serie; las corridas deterministas clásicas siguen sin ella.
    val_size = VAL_SIZE if (args.auto or args.config) else 0
    if receta is not None:
        # La baraja congela la cola de validación del lane (cv.val_size): sin ella la parada
        # temprana no tiene métrica que vigilar y el ajuste revienta.
        val_size = int(congelado["cv"]["val_size"])

    lvl = level.set_index(["unique_id", "ds"])["y"]  # mapa (serie, mes) -> nivel real, para reintegrar
    merged = level[["unique_id", "ds", "y"]].copy()  # base en NIVEL con la y real
    estado: dict = {"status": "ok", "models": {}, "warnings": [], "exception": None}
    t0 = time.time()
    for name, build in builders.items():
        capturados: list[str] = []
        try:
            with warnings.catch_warnings(record=True) as capturas:
                warnings.simplefilter("always")
                nf = NeuralForecast(models=[build()], freq="MS", local_scaler_type=local)
                cv = nf.cross_validation(
                    df=train, n_windows=HOLDOUT, step_size=1, refit=False, val_size=val_size
                ).reset_index()
            capturados = sorted({f"{w.category.__name__}: {str(w.message)[:90]}" for w in capturas})
            if args.auto:
                _dump_best_config(nf, name, args.table)  # AK8c: ganadora + trials (procedencia)
            # OJO: reset_index puede crear una columna 'index'; el pronóstico es la columna del
            # modelo (no quantiles -lo-/-hi-). Excluir todas las meta para no agarrar 'index'.
            meta = {"index", "unique_id", "ds", "cutoff", "y"}
            cands = [c for c in cv.columns if c not in meta and "-lo-" not in c and "-hi-" not in c]
            # Congelado en la baraja: para DeepAR se puntúa la MEDIANA, no la media de las
            # trayectorias — que es lo que hizo divergir a la corrida histórica a escala 10^4.
            mediana = f"{name}-median" if receta is None else "DeepAR-median"
            col = mediana if mediana in cv.columns else cands[0]
            out = cv[["unique_id", "ds", col]].copy()
            if args.diff:  # reintegrar: nivel_pred[t] = nivel_real[t-1] + Δpred[t] (1 paso, sin leakage)
                prev_ds = out["ds"] - pd.DateOffset(months=1)
                prev = np.array([lvl.get(k, np.nan) for k in zip(out["unique_id"], prev_ds, strict=True)])
                out[col] = prev + out[col].to_numpy()
            out = out.rename(columns={col: name})[["unique_id", "ds", name]]
            merged = merged.merge(out, on=["unique_id", "ds"], how="left")
            ok = int(merged[name].notna().sum())
            # NaN/Inf SOBRE LO PRODUCIDO por el modelo. Contarlos sobre el marco completo daría
            # miles de «NaN» que solo son filas de entrenamiento sin pronóstico, y eso no es un
            # problema numérico: es el diseño (solo el hold-out lleva pronóstico).
            producido = out[name].to_numpy(dtype="float64")
            estado["models"][name] = {
                "forecasts": ok,
                "column_used": col,
                "n_predicted": int(len(producido)),
                "n_nan": int(np.isnan(producido).sum()),
                "n_inf": int(np.isinf(producido).sum()),
                "converged": bool(ok > 0),
                "warnings": capturados,
            }
            estado["warnings"].extend(capturados)
            print(f"  ✓ {name}: {ok} pronósticos (columna {col})")
        except Exception as e:  # noqa: BLE001 — un modelo que falle no aborta el resto
            import traceback

            estado["status"] = "failed"
            estado["exception"] = f"{type(e).__name__}: {str(e)[:300]}"
            estado["models"][name] = {
                "forecasts": 0,
                "converged": False,
                "warnings": capturados,
                "exception": estado["exception"],
            }
            print(f"  ✗ {name} FALLO: {type(e).__name__}: {str(e)[:120]}")
            traceback.print_exc()

    suffix = args.suffix or (receta.name if receta else ("diff" if args.diff else "levels"))
    # Los lanes de E3 escriben en su PROPIO directorio: pisar `reports/campaign/global_*.csv`
    # destruiría la procedencia de la campaña sellada, que es la línea base de la comparación.
    destino = (
        ROOT / "reports" / "campaign" / "e3" / f"e3_{args.table}_{args.cohort}_{suffix}.csv"
        if receta is not None
        else ROOT / "reports" / "campaign" / f"global_{args.table}_{suffix}.csv"
    )
    out = destino
    # solo las filas de hold-out (las últimas 24 por serie) llevan pronóstico
    merged = merged[merged.drop(columns=["unique_id", "ds", "y"]).notna().any(axis=1)]
    out.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out, index=False)
    print(f"guardado {out.relative_to(ROOT)} ({len(merged)} filas)")
    if args.receipt is not None:
        _escribir_receipt(args, deck, receta, panel, merged, out, estado, t0)


def _selfcheck() -> None:
    """Valida el encoding de régimen y el relleno causal LOCF (F1)."""
    base = BASE
    # C se codifica como el mes del boletín (days_since_base del propio mes), F tal cual, U=NaN
    ds = pd.date_range("2020-01-01", periods=4, freq="MS")
    g = pd.DataFrame({"ds": ds, "status": ["F", "C", "U", "F"], "days_since_base": [16000.0, np.nan, np.nan, 16100.0]})
    s = encode_regime(g)
    assert s.iloc[0] == 16000.0 and s.iloc[3] == 16100.0
    assert s.iloc[1] == (ds[1] - base).days  # C -> mes del boletín
    assert np.isnan(s.iloc[2])  # U -> NaN (lo rellena regular_monthly con LOCF)
    # F1: LOCF causal — el hueco arrastra el bracket IZQUIERDO, jamás una rampa al futuro,
    # y mutar el futuro no cambia el pasado (propiedad metamórfica del walk-forward).
    idx = pd.date_range("2020-01-01", periods=20, freq="MS")
    v = np.arange(20, dtype=float)
    v[[5, 6, 7, 8, 9]] = np.nan  # hueco de 5: LOCF lo arrastra (ya no corta el segmento)
    reg = regular_monthly(pd.Series(v, index=idx).dropna())
    assert len(reg) == 20 and (reg.iloc[5:10] == v[4]).all()
    mut = v.copy()
    mut[15:] += 1_000.0  # reescribe SOLO el futuro
    reg_mut = regular_monthly(pd.Series(mut, index=idx).dropna())
    assert (reg_mut.iloc[:15] == reg.iloc[:15]).all(), "LOCF: el futuro mutado cambió el pasado"
    print("selfcheck OK (encoding C/F/U + relleno causal LOCF)")


if __name__ == "__main__":
    import sys

    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        main()
