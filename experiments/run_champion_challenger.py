"""Corre la evaluación campeón–retador y emite el veredicto de promoción (gateado).

    ante/bin/python experiments/run_champion_challenger.py            # evalúa + reporta
    ante/bin/python experiments/run_champion_challenger.py --mlflow   # + staging MLflow
    ante/bin/python experiments/run_champion_challenger.py --promote  # aplica al manifiesto si gana un retador

Escribe ``reports/governance/champion_challenger.json`` (+ ``.md`` legible). La promoción real edita
``reports/governance/champion_manifest.json`` SOLO con ``--promote`` y SOLO si el retador es promovible
(Holm-significativo + margen medio material). El demostrador (``generate_web_forecasts.py``)
lee su receta de ese manifiesto, así que promover = un cambio de config versionado y auditado.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vp_data import tracking  # noqa: E402
from vp_model import champion  # noqa: E402

REPORTS = champion.REPORTS


def _markdown(verdicts: list[champion.Verdict]) -> str:
    lines = ["# Campeón–retador — veredicto de promoción", ""]
    for v in verdicts:
        crps = champion.crps_champion(v.table)
        crps_note = f" · CRPS {crps} (informativo)" if crps is not None else ""
        lines += [
            f"## {v.table} — campeón `{v.champion}` "
            f"(MASE media {v.champion_mean} · mediana {v.champion_median}{crps_note})",
            "",
            "| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿promovible? |",
            "|---|---|---|---|---|---|",
        ]
        for c in v.challengers:
            mark = "**SÍ**" if c["promotable"] else "no"
            lines.append(
                f"| `{c['challenger']}` | {c['mean']} | {c['mean_margin_vs_champion']:+.4f} "
                f"| {c['wilcoxon_p']} | {c['holm_p']} | {mark} |"
            )
        rec = v.promote["challenger"] if v.promote else "ninguno — se mantiene el campeón"
        lines += ["", f"**Veredicto:** {rec}.", ""]
    lines += [
        "> Margen >0 = el retador mejora la MASE media. La promoción exige Holm-significancia",
        "> + margen material. La confirmación PROSPECTIVA (ledger congelado) requiere despliegue",
        "> en sombra del retador; hoy el ledger solo califica al campeón desplegado.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true", help="loguea el veredicto al staging MLflow")
    ap.add_argument("--promote", action="store_true", help="aplica el retador ganador al manifiesto")
    args = ap.parse_args()

    champions = champion.load_manifest()
    verdicts = [champion.evaluate(t, champions[t]) for t in ("FAD", "DFF")]

    payload = {
        v.table: {
            "champion": v.champion,
            "champion_mean": v.champion_mean,
            "champion_median": v.champion_median,
            # AM5: informative probabilistic metric (None when the CRPS CSV is absent).
            # NOT a gate — promotion still rides on point MASE + Wilcoxon/Holm.
            "champion_crps": champion.crps_champion(v.table),
            "challengers": v.challengers,
            "promote": v.promote,
        }
        for v in verdicts
    }
    (REPORTS / "governance").mkdir(parents=True, exist_ok=True)
    (REPORTS / "governance" / "champion_challenger.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )
    (REPORTS / "governance" / "champion_challenger.md").write_text(_markdown(verdicts))

    for v in verdicts:
        rec = v.promote["challenger"] if v.promote else "mantener campeón"
        print(f"[{v.table}] campeón={v.champion} MASE={v.champion_mean} → {rec}")
        if args.mlflow:
            tracking.log_run(
                experiment="champion_challenger",
                run_name=f"{v.table}-{v.champion}",
                params={"table": v.table, "champion": v.champion},
                metrics={"champion_mean_mase": v.champion_mean, "champion_median_mase": v.champion_median},
                tags={"promote": str(bool(v.promote))},
                artifacts=[str(REPORTS / "governance" / "champion_challenger.json")],
            )

    if args.promote:
        changed = False
        for v in verdicts:
            if v.promote:
                # AP4: the winning Recipe travels serialized inside the verdict — the pretty
                # display name is for humans only and is never parsed again.
                champions[v.table] = champion.recipe_from_dict(v.promote["recipe"])
                changed = True
                print(f"  ↑ PROMOVIDO {v.table}: nuevo campeón = {v.promote['challenger']}")
        if changed:
            champion.save_manifest(champions)
            print(f"  manifiesto actualizado → {champion.MANIFEST}")
        else:
            print("  --promote sin efecto: ningún retador es promovible")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
