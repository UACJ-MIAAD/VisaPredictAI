"""Agrega las corridas multi-semilla de los modelos profundos globales.

Lee los CSV ``reports/global_{TABLE}_{prefix}{seed}.csv`` (uno por semilla, producidos
por ``run_global_deep.py --seed N --suffix {prefix}{seed}``), calcula el MASE de
hold-out por serie con las MISMAS métricas del proyecto (vía ``eval_neuralforecast``),
promedia sobre el bloque familiar para obtener UN número por semilla, y reporta
media ± desv. estándar e IC 95% (t de Student) sobre las semillas.

Corre en el ENTORNO PRINCIPAL (ante/bin/python), no en ante_nf — usa vp_model.dataset.

Uso:  ante/bin/python aggregate_seeds.py --table FAD --prefix auto_s --model AutoBiTCN
"""

from __future__ import annotations

import argparse

import numpy as np

from vp_model.eval_neuralforecast import eval_global_deep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="FAD")
    ap.add_argument("--prefix", required=True, help="prefijo de la variante, p.ej. 'auto_s' o 'dff_s'")
    ap.add_argument("--model", required=True, help="columna del modelo, p.ej. 'AutoBiTCN' o 'BiTCN'")
    ap.add_argument("--block", default="family")
    args = ap.parse_args()

    df = eval_global_deep(args.table)
    df = df[(df.block == args.block) & (df.model == args.model)
            & df.variant.str.startswith(args.prefix)]
    if df.empty:
        raise SystemExit(f"sin datos para {args.model}/{args.prefix}* en {args.table}/{args.block}")

    # un MASE por semilla = media sobre las series del bloque
    per_seed = df.groupby("variant")["hold_mase"].mean().sort_index()
    vals = per_seed.to_numpy()
    n = len(vals)
    mean, sd = float(vals.mean()), float(vals.std(ddof=1))
    se = sd / np.sqrt(n)
    # IC 95% con t de Student (n pequeño)
    from scipy.stats import t

    tcrit = float(t.ppf(0.975, n - 1)) if n > 1 else float("nan")
    lo, hi = mean - tcrit * se, mean + tcrit * se

    print(f"\n=== {args.model} · {args.table}/{args.block} · {n} semillas ===")
    for v, x in per_seed.items():
        print(f"  {v:>14}: MASE {x:.4f}")
    print(f"\n  media   : {mean:.4f}")
    print(f"  desv.   : {sd:.4f}")
    print(f"  IC 95%  : [{lo:.4f}, {hi:.4f}]  (t, n={n})")
    print(f"  min/max : {vals.min():.4f} / {vals.max():.4f}")


if __name__ == "__main__":
    main()
