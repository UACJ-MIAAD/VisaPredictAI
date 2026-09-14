"""Agrega las corridas multi-semilla de los modelos profundos globales.

Lee los CSV ``reports/campaign/global_{TABLE}_{prefix}{seed}.csv`` (uno por semilla, producidos
por ``experiments/run_global_deep.py --seed N --suffix {prefix}{seed}``), calcula el MASE de
hold-out por serie con las MISMAS métricas del proyecto (vía ``eval_neuralforecast``),
promedia sobre el bloque familiar para obtener UN número por semilla, y reporta
media ± desv. estándar e IC 95% (t de Student) sobre las semillas.

Corre en el ENTORNO PRINCIPAL (ante/bin/python), no en ante_nf — usa vp_model.dataset.

Uso:  ante/bin/python experiments/aggregate_seeds.py --table FAD --prefix auto_s --model AutoBiTCN
"""

from __future__ import annotations

import argparse
import hashlib
import math
from pathlib import Path

import numpy as np

N_SEEDS = 5
ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def acreditar_grupo(table: str, prefix: str, camp_dir: Path, *, campaign_id: str, source_git_sha: str) -> None:
    """M74-E-R6 · acredita el GRUPO EXACTO de CSV por semilla contra la campaña, ANTES de leerlos.

    `eval_global_deep` lee por glob `global_<tabla>_*.csv`: un CSV de otra corrida con el conteo
    correcto se agregaba igual. Cada semilla necesita su sidecar con la identidad de la campaña en
    curso y el sha256 de los bytes que hay en disco.
    """
    from vp_model.artifact_receipt import loads_strict

    if not prefix.endswith("_s"):
        raise SystemExit(f"agregación {table}/{prefix}*: el prefijo de semilla debe terminar en `_s`")
    variante = prefix[:-2]
    esperados = {f"global_{table}_{prefix}{i}.csv" for i in range(1, N_SEEDS + 1)}
    presentes = {q.name for q in Path(camp_dir).glob(f"global_{table}_{prefix}*.csv")}
    problemas = [] if presentes == esperados else [f"grupo {sorted(presentes)} != {sorted(esperados)}"]
    for i in range(1, N_SEEDS + 1):
        csv, side = (
            Path(camp_dir) / f"global_{table}_{prefix}{i}.csv",
            Path(camp_dir) / f"coverage_{table}_{variante}_s{i}.json",
        )
        if not csv.is_file() or not side.is_file():
            problemas.append(f"s{i}: sin CSV o sin sidecar")
            continue
        d = loads_strict(side.read_text(encoding="utf-8"))
        for k, v in {
            "campaign_id": campaign_id,
            "source_git_sha": source_git_sha,
            "table": table,
            "variant": variante,
            "seed": i,
        }.items():
            if d.get(k) != v:
                problemas.append(f"s{i}: {k}={d.get(k)!r} ≠ {v!r}")
        if d.get("csv_sha256") != "sha256:" + hashlib.sha256(csv.read_bytes()).hexdigest():
            problemas.append(f"s{i}: el CSV cambió después de sellar su sidecar")
    if problemas:
        raise SystemExit(f"agregación {table}/{prefix}*: grupo NO acreditado — " + " · ".join(problemas))


def aggregate(df, *, prefix: str, model: str, block: str, n_seeds: int = N_SEEDS) -> dict:
    """Agrega las ``n_seeds`` corridas de ``model`` en ``block`` a un MASE medio ± IC 95 %.

    Fail-closed (auditoría 13-jul-2026 ronda 8 — antes solo abortaba con las 5 semillas Inf/NaN):

    * exige EXACTAMENTE ``{prefix}1..{prefix}n`` (``startswith`` recogía ``s01/sOLD/s6`` y
      contaminaba la agregación);
    * **valida finitud ANTES de agregar**: rechaza cualquier ``hold_mase`` no finito en el
      subconjunto — ``groupby.mean()`` omite los NaN en silencio y produce un promedio sobre un
      subconjunto DISTINTO por semilla (no comparable) que salía con exit 0;
    * **valida finitud DESPUÉS de agregar**: cinco valores ~1e308 son finitos individualmente
      pero su suma desborda el float64 (media/IC = ``Inf``) y pasarían el chequeo de arriba.
    """
    want = {f"{prefix}{i}" for i in range(1, n_seeds + 1)}
    # (paso 4.2) detectar variantes con el prefijo ANTES de filtrar con isin: s6/s01/s1_backup
    # deben ABORTAR la agregación, no descartarse en silencio (`isin(want)` las escondía).
    prefixed = set(df.loc[df.variant.astype(str).str.startswith(prefix), "variant"])
    if prefixed != want:
        raise SystemExit(
            f"agregación {model}/{prefix}* en {block}: variantes con prefijo {sorted(prefixed)} "
            f"!= las {n_seeds} esperadas {sorted(want)} (extra {sorted(prefixed - want)}, "
            f"falta {sorted(want - prefixed)})"
        )
    sub = df[(df.block == block) & (df.model == model) & df.variant.isin(want)]
    got = set(sub.variant.unique())
    if got != want:
        raise SystemExit(
            f"agregación {model}/{prefix}* en {block}: semillas {sorted(got)} != las {n_seeds} "
            f"esperadas {sorted(want)} (falta {sorted(want - got)}, sobra {sorted(got - want)})"
        )
    # (paso 4.1) universo evaluado IDÉNTICO entre semillas: cada semilla debe evaluar EXACTAMENTE
    # el mismo conjunto de series (country, category). Un CSV parcial evalúa menos series y daría
    # un promedio no comparable; dos archivos con series distintas NO son equivalentes.
    universe: frozenset | None = None
    for variant, group in sub.groupby("variant"):
        keys = frozenset(zip(group["country"], group["category"], strict=True))
        if universe is None:
            universe = keys
        elif keys != universe:
            raise SystemExit(
                f"agregación {model}/{prefix}* en {block}: las semillas evalúan series DISTINTAS "
                f"(universo no idéntico); {variant} difiere en {sorted(keys ^ universe)[:4]}"
            )
    raw = sub["hold_mase"].to_numpy(dtype=float)
    if raw.size == 0 or not np.all(np.isfinite(raw)):
        n_bad = int((~np.isfinite(raw)).sum())
        raise SystemExit(
            f"agregación {model}/{prefix}* en {block}: {n_bad}/{raw.size} hold_mase no "
            f"finito(s) — semillas inutilizables, no se agrega (fail-closed)"
        )

    per_seed = sub.groupby("variant")["hold_mase"].mean().sort_index()
    vals = per_seed.to_numpy()
    n = len(vals)
    mean, sd = float(vals.mean()), float(vals.std(ddof=1))
    se = sd / np.sqrt(n)
    # El overflow de valores enormes (media/desv./error) se detecta SIN scipy, ANTES de
    # importarlo: así el fail-closed no depende de un extra que el perfil `dev` de CI no
    # instala (el import de scipy vive abajo, solo para el t crítico del IC).
    if not all(math.isfinite(x) for x in (mean, sd, se)):
        raise SystemExit(
            f"agregación {model}/{prefix}* en {block}: media/desv. no finitas "
            f"(media={mean}, sd={sd}) — ¿valores enormes desbordan el promedio?"
        )
    from scipy.stats import t

    tcrit = float(t.ppf(0.975, n - 1)) if n > 1 else float("nan")
    lo, hi = mean - tcrit * se, mean + tcrit * se
    if not all(math.isfinite(x) for x in (lo, hi)):
        raise SystemExit(f"agregación {model}/{prefix}* en {block}: IC no finito ([{lo}, {hi}])")
    return {
        "per_seed": per_seed,
        "n": n,
        "mean": mean,
        "sd": sd,
        "se": se,
        "lo": lo,
        "hi": hi,
        "min": float(vals.min()),
        "max": float(vals.max()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="FAD")
    ap.add_argument("--prefix", required=True, help="prefijo de la variante, p.ej. 'auto_s' o 'dff_s'")
    ap.add_argument("--model", required=True, help="columna del modelo, p.ej. 'AutoBiTCN' o 'BiTCN'")
    ap.add_argument("--block", default="family")
    ap.add_argument("--mlflow", action="store_true", help="loguear el agregado multi-semilla a tracking")
    args = ap.parse_args()

    from vp_model import artifact_receipt
    from vp_model.eval_neuralforecast import eval_global_deep

    campaign_id, sha, _panel = artifact_receipt.campaign_identity(REPORTS, code_root=ROOT)
    acreditar_grupo(args.table, args.prefix, REPORTS / "campaign", campaign_id=campaign_id, source_git_sha=sha)
    df = eval_global_deep(args.table)
    st = aggregate(df, prefix=args.prefix, model=args.model, block=args.block)

    print(f"\n=== {args.model} · {args.table}/{args.block} · {st['n']} semillas ===")
    for v, x in st["per_seed"].items():
        print(f"  {v:>14}: MASE {x:.4f}")
    print(f"\n  media   : {st['mean']:.4f}")
    print(f"  desv.   : {st['sd']:.4f}")
    print(f"  IC 95%  : [{st['lo']:.4f}, {st['hi']:.4f}]  (t, n={st['n']})")
    print(f"  min/max : {st['min']:.4f} / {st['max']:.4f}")

    if args.mlflow:
        from vp_data import tracking

        tracking.log_run(
            f"deep_global_{args.table}",
            f"{args.model}/{args.prefix}/{args.table}/{args.block}",
            params={
                "model": args.model,
                "variant": args.prefix,
                "table": args.table,
                "block": args.block,
                "n_seeds": st["n"],
                "layer": "deep_global",
            },
            metrics={
                "mase_mean": st["mean"],
                "mase_std": st["sd"],
                "mase_ci_lo": st["lo"],
                "mase_ci_hi": st["hi"],
                "mase_min": st["min"],
                "mase_max": st["max"],
            },
            tags={"layer": "deep_global"},
        )
        print("  -> logueado a MLflow (deep_global)")


if __name__ == "__main__":
    main()
