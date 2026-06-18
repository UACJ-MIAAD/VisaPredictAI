"""Entrenamiento GLOBAL en GPU (AWS) — RECETA GANADORA + frontier pesado + multi-semilla.

Corre el MISMO protocolo que demostró ser el ganador en CPU/MPS local
(``run_global_deep.py``), pero abre la puerta a los modelos que en CPU son inviables o
sobreajustan: transformers de horizonte largo (Informer/Autoformer/FEDformer/PatchTST/
iTransformer/TimesNet) entrenados GLOBALES, y la confirmación MULTI-SEMILLA de los
ganadores (AutoBiTCN/BiTCN) que en CPU es lenta y en GPU es barata.

La receta (idéntica al local, NO reinventar):
  * GLOBAL: un modelo sobre TODAS las series del panel (Montero-Manso & Hyndman 2021).
  * DIFERENCIA (--diff, por defecto): entrena sobre Δy y REINTEGRA a nivel con el último
    real (1 paso, leakage-free). Es la palanca clave para series ultra-tendenciales.
  * NORMALIZACIÓN POR SERIE (--local-scaler): arregla la escala mixta familiar/EB.
  * HPO sin fuga (--auto): AutoModels con Optuna; el hold-out de 24m NUNCA entra a la búsqueda.

Salida: ``reports/global_{table}_{suffix}.csv`` (nivel real reintegrado), que el entorno
PRINCIPAL evalúa con ``vp_model.eval_neuralforecast`` usando las MISMAS métricas — así el
frontier de GPU es comparable 1:1 contra ETS/Theta (0.118 FAD) y el AutoBiTCN local (0.108).

Ejemplos (en la instancia GPU, venv con requirements.txt):
  # Frontier pesado global, FAD familiar, diferencia + norma por serie:
  python train_gpu.py --table FAD --models Informer Autoformer FEDformer PatchTST TimesNet
  # Confirmación multi-semilla del ganador con HPO amplio (lo que CPU no alcanza):
  python train_gpu.py --table FAD --auto --models AutoBiTCN --num-samples 80 --seeds 1 2 3 4 5 6 7 8 9 10
  # DFF con HPO (pendiente del lado CPU):
  python train_gpu.py --table DFF --auto --models AutoBiTCN AutoTiDE --num-samples 50 --seeds 1 2 3 4 5
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

HOLDOUT = 24
PILOT = ("mexico", "india", "china", "philippines", "all_chargeability")
MAX_GAP = 3  # huecos <= 3 meses se interpolan; los más largos cortan la serie
BASE = pd.Timestamp("1975-01-01")  # época de days_since_base (t0)


def regular_monthly(s: pd.Series, max_gap: int = MAX_GAP) -> pd.Series:
    """Serie mensual regular sin inventar datos sobre huecos largos (= run_global_deep)."""
    full = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="MS"))
    isna = full.isna().to_numpy()
    total = np.zeros(len(isna), dtype=int)
    run = 0
    for i in range(len(isna)):
        run = run + 1 if isna[i] else 0
        total[i] = run
    for i in range(len(isna) - 2, -1, -1):
        if isna[i] and isna[i + 1]:
            total[i] = total[i + 1]
    long_gap = isna & (total > max_gap)
    start = int(np.where(long_gap)[0].max()) + 1 if long_gap.any() else 0
    return full.iloc[start:].interpolate(method="linear", limit_area="inside").dropna()


def encode_regime(g: pd.DataFrame) -> pd.Series:
    """C/F/U como UNA serie continua: F=fecha de prioridad, C=mes del boletín (cutoff=hoy),
    U/UNK=NaN (puenteados como hueco corto). EVALUACIÓN sigue F-only. (= run_global_deep)."""
    g = g.sort_values("ds")
    y = g["days_since_base"].astype("float64").to_numpy().copy()
    is_c = (g["status"] == "C").to_numpy()
    y[is_c] = (g["ds"] - BASE).dt.days.to_numpy().astype("float64")[is_c]
    return pd.Series(y, index=pd.DatetimeIndex(g["ds"]))


def load_panel(panel_path: str, table: str, block: str) -> pd.DataFrame:
    """Panel largo neuralforecast (unique_id=país/bloque/categoría, ds, y), régimen codificado."""
    df = pd.read_parquet(panel_path)
    blocks = ("family", "employment") if block == "both" else (block,)
    # NO se filtra a F: las celdas C dan continuidad real (Current=mes del boletín). Eval = F-only.
    df = df[(df["table"] == table) & (df["block"].isin(blocks)) & (df["country"].isin(PILOT))].copy()
    df["unique_id"] = df["country"] + "/" + df["block"] + "/" + df["category"]
    df["ds"] = pd.to_datetime(df["bulletin_date"])
    min_len = (60 if table == "FAD" else 36) + HOLDOUT + 6
    out = []
    for uid, g in df.groupby("unique_id"):
        s = regular_monthly(encode_regime(g[["ds", "status", "days_since_base"]]))
        if len(s) >= min_len:
            out.append(pd.DataFrame({"unique_id": uid, "ds": s.index, "y": s.to_numpy()}))
    return pd.concat(out, ignore_index=True)


def difference(panel: pd.DataFrame) -> pd.DataFrame:
    """Δy por serie (descarta el primer NaN); se reintegra tras predecir."""
    parts = []
    for _uid, g in panel.groupby("unique_id"):
        g = g.sort_values("ds").copy()
        g["y"] = g["y"].diff()
        parts.append(g.iloc[1:])
    return pd.concat(parts, ignore_index=True)


def reintegrate(out: pd.DataFrame, col: str, level_map: pd.Series) -> np.ndarray:
    """nivel_pred[t] = nivel_real[t-1] + Δpred[t] (1 paso, sin leakage)."""
    prev_ds = out["ds"] - pd.DateOffset(months=1)
    prev = np.array([level_map.get(k, np.nan) for k in zip(out["unique_id"], prev_ds, strict=True)])
    return prev + out[col].to_numpy()


def _auto_config(trial):
    return {
        "input_size": trial.suggest_categorical("input_size", [18, 24, 36]),
        "learning_rate": trial.suggest_float("learning_rate", 1e-4, 1e-2, log=True),
        "max_steps": trial.suggest_categorical("max_steps", [500, 1000, 2000]),
        "scaler_type": trial.suggest_categorical("scaler_type", ["standard", "robust"]),
        "val_check_steps": 50,
    }


def build_models(names, input_size, max_steps, n_series, seed, auto, num_samples):
    """Registro frontier + Auto. Cada modelo se entrena AISLADO (un fallo no aborta el resto)."""
    if auto:
        from neuralforecast.auto import (
            AutoBiTCN,
            AutoInformer,
            AutoNHITS,
            AutoPatchTST,
            AutoTiDE,
            AutoTimesNet,
        )
        from neuralforecast.losses.pytorch import MAE

        try:
            import optuna

            sampler = optuna.samplers.TPESampler(seed=seed, multivariate=True)
        except Exception:  # noqa: BLE001
            sampler = None
        cls = {
            "AutoBiTCN": AutoBiTCN,
            "AutoTiDE": AutoTiDE,
            "AutoNHITS": AutoNHITS,
            "AutoPatchTST": AutoPatchTST,
            "AutoInformer": AutoInformer,
            "AutoTimesNet": AutoTimesNet,
        }
        reg = {
            k: (
                lambda M=v: M(
                    h=1,
                    loss=MAE(),
                    config=_auto_config,
                    num_samples=num_samples,
                    backend="optuna",
                    search_alg=sampler,
                    verbose=False,
                )
            )
            for k, v in cls.items()
        }
    else:
        from neuralforecast.models import (  # noqa: I001
            NHITS,
            BiTCN,
            FEDformer,
            Informer,
            Autoformer,
            PatchTST,
            TiDE,
            TimesNet,
            TimeMixer,
            iTransformer,
        )

        c = dict(
            h=1,
            input_size=input_size,
            max_steps=max_steps,
            scaler_type="standard",
            random_seed=seed,
            enable_progress_bar=False,
            enable_model_summary=False,
        )
        reg = {
            "Informer": lambda: Informer(**c),
            "Autoformer": lambda: Autoformer(**c),
            "FEDformer": lambda: FEDformer(**c),
            "PatchTST": lambda: PatchTST(**c),
            "TimesNet": lambda: TimesNet(**c),
            "BiTCN": lambda: BiTCN(**c),
            "TiDE": lambda: TiDE(**c),
            "NHITS": lambda: NHITS(**c),
            # multivariados (requieren n_series; pueden fallar con series de distinta longitud):
            "iTransformer": lambda: iTransformer(**c, n_series=n_series),
            "TimeMixer": lambda: TimeMixer(**c, n_series=n_series),
        }
    if names:
        unknown = set(names) - set(reg)
        if unknown:  # falla fuerte en vez de producir un CSV vacío en silencio
            raise ValueError(
                f"modelos no reconocidos: {sorted(unknown)}. "
                f"Disponibles: {sorted(reg)} (¿--auto para los Auto*? Mamba no existe en neuralforecast)"
            )
        reg = {k: v for k, v in reg.items() if k in names}
    return reg


def run_seed(panel, table, block, diff, local, names, max_steps, seed, auto, num_samples, out_dir):
    from neuralforecast import NeuralForecast

    uids = panel["unique_id"].nunique()
    train = difference(panel) if diff else panel.copy()
    input_size = 36 if table == "FAD" else 18
    builders = build_models(names, input_size, max_steps, uids, seed, auto, num_samples)
    level_map = panel.set_index(["unique_id", "ds"])["y"]
    merged = panel[["unique_id", "ds", "y"]].copy()
    for name, build in builders.items():
        try:
            nf = NeuralForecast(models=[build()], freq="MS", local_scaler_type="standard" if local else None)
            cv = nf.cross_validation(df=train, n_windows=HOLDOUT, step_size=1, refit=False).reset_index()
            meta = {"index", "unique_id", "ds", "cutoff", "y"}
            col = next(c for c in cv.columns if c not in meta and "-lo-" not in c and "-hi-" not in c)
            out = cv[["unique_id", "ds", col]].copy()
            if diff:
                out[col] = reintegrate(out, col, level_map)
            out = out.rename(columns={col: name})[["unique_id", "ds", name]]
            merged = merged.merge(out, on=["unique_id", "ds"], how="left")
            print(f"  ✓ seed {seed} {name}: {merged[name].notna().sum()} pronósticos")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ seed {seed} {name} FALLO: {type(e).__name__}: {str(e)[:120]}")

    tag = "auto" if auto else ("diff" if diff else "levels")
    suffix = f"{tag}_s{seed}" if len(names or []) != 0 else tag
    path = Path(out_dir) / f"global_{table}_{suffix}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = merged[merged.drop(columns=["unique_id", "ds", "y"]).notna().any(axis=1)]
    merged.to_csv(path, index=False)
    print(f"  guardado {path} ({len(merged)} filas)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="visa_panel_long.parquet")
    ap.add_argument("--out-dir", default="reports")
    ap.add_argument("--table", default="FAD")
    ap.add_argument("--block", default="family", choices=["family", "employment", "both"])
    ap.add_argument(
        "--no-diff", dest="diff", action="store_false", help="entrenar en NIVELES (por defecto: diferencia)"
    )
    ap.add_argument("--local-scaler", action="store_true", help="normaliza cada serie")
    ap.add_argument("--max-steps", type=int, default=1000)
    ap.add_argument("--auto", action="store_true", help="AutoModels con HPO Optuna")
    ap.add_argument("--num-samples", type=int, default=50, help="trials de HPO por modelo Auto")
    ap.add_argument("--models", nargs="+", default=None)
    ap.add_argument("--seeds", nargs="+", type=int, default=[1])
    ap.add_argument(
        "--shutdown-on-done",
        action="store_true",
        help="apaga la instancia (sudo shutdown -h +1) al terminar — red de seguridad de costo",
    )
    args = ap.parse_args()

    try:
        # load_panel DENTRO del try: si el path del panel está mal, igual se dispara el
        # shutdown (si no, un error temprano dejaría la GPU encendida cobrando).
        panel = load_panel(args.panel, args.table, args.block)
        print(
            f"panel: {panel['unique_id'].nunique()} series, {len(panel)} filas "
            f"({args.table}/{args.block}), diff={args.diff}, auto={args.auto}, seeds={args.seeds}"
        )
        for seed in args.seeds:
            run_seed(
                panel,
                args.table,
                args.block,
                args.diff,
                args.local_scaler,
                args.models,
                args.max_steps,
                seed,
                args.auto,
                args.num_samples,
                args.out_dir,
            )
    finally:
        if args.shutdown_on_done:
            # se ejecuta aunque falle, para no dejar la GPU encendida por una excepción.
            import subprocess

            print("apagando la instancia en 1 min (cancela con: sudo shutdown -c)…")
            r = subprocess.run(["sudo", "shutdown", "-h", "+1"], check=False)
            if r.returncode != 0:
                print(
                    f"⚠️ ¡EL APAGADO FALLÓ (returncode {r.returncode})! Apaga/termina la "
                    "instancia A MANO desde la consola AWS para no seguir pagando."
                )


def _selfcheck() -> None:
    """Reintegración: Δ reintegrada con el nivel previo real reconstruye el nivel."""
    lvl = pd.DataFrame(
        {"unique_id": "a", "ds": pd.date_range("2020-01-01", periods=4, freq="MS"), "y": [10.0, 13.0, 12.0, 20.0]}
    )
    lmap = lvl.set_index(["unique_id", "ds"])["y"]
    pred = lvl.iloc[1:][["unique_id", "ds"]].copy()
    pred["d"] = lvl["y"].diff().iloc[1:].to_numpy()  # Δ "predicho" perfecto
    got = reintegrate(pred, "d", lmap)
    assert np.allclose(got, lvl["y"].iloc[1:].to_numpy()), got
    print("selfcheck OK")


if __name__ == "__main__":
    import sys

    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        main()
