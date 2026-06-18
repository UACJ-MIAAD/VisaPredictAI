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

Salida: ``reports/global_{table}_{levels|diff}.csv`` (unique_id, ds, y, <modelos>),
SIEMPRE en espacio de NIVEL (la variante diff ya viene reintegrada), que el entorno
principal evalúa con ``eval_neuralforecast`` usando las MISMAS métricas.

Uso:  ante_nf/bin/python run_global_deep.py --table FAD --block both [--diff] [--max-steps 1000] [--fast]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.parquet"
PILOT = ("mexico", "india", "china", "philippines", "all_chargeability")
HOLDOUT = 24


def load_panel(table: str, block: str) -> pd.DataFrame:
    """Panel largo neuralforecast (unique_id, ds, y), F-only, mensual regular.

    ``block='both'`` apila familiar + empleo (más series → mejor aprendizaje global).
    Solo entran series con largo suficiente para la ventana + hold-out.
    """
    df = pd.read_parquet(PANEL)
    blocks = ("family", "employment") if block == "both" else (block,)
    df = df[(df["table"] == table) & (df["block"].isin(blocks)) & (df["status"] == "F")
            & (df["country"].isin(PILOT))].copy()
    df["unique_id"] = df["country"] + "/" + df["block"] + "/" + df["category"]
    df["ds"] = pd.to_datetime(df["bulletin_date"])
    min_len = (60 if table == "FAD" else 36) + HOLDOUT + 6
    out = []
    for uid, g in df[["unique_id", "ds", "days_since_base"]].groupby("unique_id"):
        s = g.set_index("ds")["days_since_base"].sort_index()
        s = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="MS")).interpolate()
        if len(s) >= min_len:
            out.append(pd.DataFrame({"unique_id": uid, "ds": s.index, "y": s.to_numpy()}))
    return pd.concat(out, ignore_index=True)


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

    c = dict(h=1, input_size=input_size, max_steps=max_steps, scaler_type="standard",
             random_seed=seed, enable_progress_bar=False, enable_model_summary=False)
    builders = {
        "NHITS": lambda: NHITS(**c),
        "PatchTST": lambda: PatchTST(**c),
        "DeepAR": lambda: DeepAR(h=1, input_size=input_size, max_steps=max_steps,
                                 scaler_type="standard", random_seed=seed,
                                 enable_progress_bar=False, enable_model_summary=False,
                                 loss=DistributionLoss(distribution="Normal", level=[95])),
        "TiDE": lambda: TiDE(**c),
        "BiTCN": lambda: BiTCN(**c),
        "KAN": lambda: KAN(**c),
        "TimeMixer": lambda: TimeMixer(**c, n_series=n_series),
        "TimesNet": lambda: TimesNet(**c),
    }
    return builders


def _auto_config(trial):
    """Espacio de búsqueda Optuna leakage-free (ventana acorde a series cortas)."""
    return {
        "input_size": trial.suggest_categorical("input_size", [18, 24, 36]),
        "learning_rate": trial.suggest_float("learning_rate", 1e-4, 1e-2, log=True),
        "max_steps": trial.suggest_categorical("max_steps", [500, 1000]),
        "scaler_type": trial.suggest_categorical("scaler_type", ["standard", "robust"]),
        "val_check_steps": 50,
    }


def _build_auto_models(num_samples: int, seed: int = 1):
    """Modelos Auto* (HPO Optuna interna, validación leakage-free) para vencer el listón."""
    from neuralforecast.auto import AutoBiTCN, AutoNHITS, AutoPatchTST, AutoTiDE
    from neuralforecast.losses.pytorch import MAE

    def mk(M):
        return lambda: M(h=1, loss=MAE(), config=_auto_config, num_samples=num_samples,
                         backend="optuna", search_alg=_optuna_sampler(seed), verbose=False)

    return {"AutoBiTCN": mk(AutoBiTCN), "AutoPatchTST": mk(AutoPatchTST),
            "AutoNHITS": mk(AutoNHITS), "AutoTiDE": mk(AutoTiDE)}


def _optuna_sampler(seed: int):
    """Sampler TPE con semilla → la búsqueda Optuna varía por --seed (multi-semilla real)."""
    import optuna

    return optuna.samplers.TPESampler(seed=seed, multivariate=True)


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
    ap.add_argument("--num-samples", type=int, default=20, help="trials de HPO por modelo Auto")
    ap.add_argument("--suffix", default=None, help="sufijo del CSV de salida (p.ej. 'auto', 'ls')")
    ap.add_argument("--seed", type=int, default=1, help="semilla (init de pesos + sampler Optuna)")
    args = ap.parse_args()

    import torch
    torch.set_num_threads(1)  # py3.14/macOS: evita segfault multihilo (igual que el env principal)
    from neuralforecast import NeuralForecast

    panel = load_panel(args.table, args.block)
    uids = panel["unique_id"].nunique()
    print(f"panel: {uids} series, {len(panel)} filas ({args.table}/{args.block}), diff={args.diff}")

    # Espacio de entrenamiento: nivel o primera diferencia (guardando el nivel para reintegrar).
    level = panel.copy()
    train = panel.copy()
    if args.diff:
        parts = []
        for _uid, g in panel.groupby("unique_id"):
            g = g.sort_values("ds").copy()
            g["y"] = g["y"].diff()
            parts.append(g.iloc[1:])  # descarta el primer NaN
        train = pd.concat(parts, ignore_index=True)

    input_size = (36 if args.table == "FAD" else 18)
    max_steps = 5 if args.fast else args.max_steps
    if args.auto:
        builders = _build_auto_models(2 if args.fast else args.num_samples, args.seed)
    else:
        builders = _build_models(input_size, max_steps, uids, args.seed)
    if args.models:
        builders = {k: v for k, v in builders.items() if k in args.models}
    local = "standard" if args.local_scaler else None

    lvl = level.set_index(["unique_id", "ds"])["y"]  # mapa (serie, mes) -> nivel real, para reintegrar
    merged = level[["unique_id", "ds", "y"]].copy()  # base en NIVEL con la y real
    for name, build in builders.items():
        try:
            nf = NeuralForecast(models=[build()], freq="MS", local_scaler_type=local)
            cv = nf.cross_validation(df=train, n_windows=HOLDOUT, step_size=1, refit=False).reset_index()
            # OJO: reset_index puede crear una columna 'index'; el pronóstico es la columna del
            # modelo (no quantiles -lo-/-hi-). Excluir todas las meta para no agarrar 'index'.
            meta = {"index", "unique_id", "ds", "cutoff", "y"}
            cands = [c for c in cv.columns if c not in meta and "-lo-" not in c and "-hi-" not in c]
            col = cands[0]
            out = cv[["unique_id", "ds", col]].copy()
            if args.diff:  # reintegrar: nivel_pred[t] = nivel_real[t-1] + Δpred[t] (1 paso, sin leakage)
                prev_ds = out["ds"] - pd.DateOffset(months=1)
                prev = np.array([lvl.get(k, np.nan) for k in zip(out["unique_id"], prev_ds, strict=True)])
                out[col] = prev + out[col].to_numpy()
            out = out.rename(columns={col: name})[["unique_id", "ds", name]]
            merged = merged.merge(out, on=["unique_id", "ds"], how="left")
            ok = merged[name].notna().sum()
            print(f"  ✓ {name}: {ok} pronósticos")
        except Exception as e:  # noqa: BLE001 — un modelo que falle no aborta el resto
            print(f"  ✗ {name} FALLO: {type(e).__name__}: {str(e)[:120]}")

    suffix = args.suffix or ("diff" if args.diff else "levels")
    out = ROOT / "reports" / f"global_{args.table}_{suffix}.csv"
    # solo las filas de hold-out (las últimas 24 por serie) llevan pronóstico
    merged = merged[merged.drop(columns=["unique_id", "ds", "y"]).notna().any(axis=1)]
    out.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out, index=False)
    print(f"guardado {out.relative_to(ROOT)} ({len(merged)} filas)")


if __name__ == "__main__":
    main()
