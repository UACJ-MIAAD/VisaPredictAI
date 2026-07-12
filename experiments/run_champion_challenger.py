"""Corre la evaluación campeón–retador y emite el veredicto de promoción (gateado).

    ante/bin/python experiments/run_champion_challenger.py            # evalúa + reporta
    ante/bin/python experiments/run_champion_challenger.py --mlflow   # + staging MLflow
    ante/bin/python experiments/run_champion_challenger.py --promote  # aplica al manifiesto si gana un retador

Escribe ``reports/governance/champion_challenger.json`` (+ ``.md`` legible). Este veredicto es
**retrospectivo** (hold-out h=1): declara al retador ``apto en hold-out`` (campo
``holdout_pass``; antes "promotable" — renombrado por A4 para no sugerir autorización
productiva). La promoción real edita ``reports/governance/champion_manifest.json`` SOLO con
``--promote`` y desde A4 exige ADEMÁS que el gate prospectivo pre-registrado
(``vp_model/promotion.py`` → ``reports/governance/promotion_decision.json``) diga
``promote`` para esa tabla — sin decisión o con decisión distinta, se rehúsa (fail closed).
Rollback: el manifiesto está versionado (git revert + redeploy). El demostrador
(``generate_web_forecasts.py``) lee su receta de ese manifiesto.
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
            "| retador | MASE media | margen vs campeón | Wilcoxon p | Holm p | ¿apto hold-out h=1? |",
            "|---|---|---|---|---|---|",
        ]
        for c in v.challengers:
            mark = "**SÍ**" if c.get("holdout_pass", c.get("promotable")) else "no"
            lines.append(
                f"| `{c['challenger']}` | {c['mean']} | {c['mean_margin_vs_champion']:+.4f} "
                f"| {c['wilcoxon_p']} | {c['holm_p']} | {mark} |"
            )
        rec = v.promote["challenger"] if v.promote else "ninguno — se mantiene el campeón"
        lines += ["", f"**Veredicto:** {rec}.", ""]
    lines += [
        "> Margen >0 = el retador mejora la MASE media. `Apto hold-out h=1` = Holm-significativo",
        "> + margen material en el hold-out retrospectivo — NO autoriza producción. La",
        "> autorización la da el gate prospectivo pre-registrado (docs/PROMOTION_POLICY.md,",
        "> decisión en reports/governance/promotion_decision.json) sobre pares live",
        "> campeón-vs-sombra, aplicada por un humano con --promote.",
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
            # A4: el veredicto es retrospectivo (hold-out h=1) y NO autoriza producción.
            "gate_scope": "retrospective-holdout-h1 (ver docs/PROMOTION_POLICY.md)",
            "holdout_winner": v.promote,
            "promote": v.promote,  # alias deprecado (dual-read; retirar tras migrar consumidores)
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
        from vp_model import promotion

        decision_path = REPORTS / "governance" / "promotion_decision.json"
        changed = False
        for v in verdicts:
            if not v.promote:
                continue
            # A4: la aptitud retrospectiva NO basta — el gate prospectivo pre-registrado
            # debe decir "promote" para esta tabla, o la promoción se rehúsa (fail closed).
            # A-02: la autorizacion se liga al candidato EXACTO — retador y campeon por
            # nombre de receta; cualquier diferencia con la evidencia del gate la invalida.
            ok, why = promotion.authorize(
                v.table, decision_path, challenger=v.promote["challenger"], champion=v.champion
            )
            if not ok:
                print(f"  ✗ PROMOCIÓN REHUSADA [{v.table}] {v.promote['challenger']}: {why}")
                continue
            # AP4: the winning Recipe travels serialized inside the verdict — the pretty
            # display name is for humans only and is never parsed again.
            champions[v.table] = champion.recipe_from_dict(v.promote["recipe"])
            changed = True
            print(f"  ↑ PROMOVIDO {v.table}: nuevo campeón = {v.promote['challenger']} ({why})")
        if changed:
            champion.save_manifest(champions)
            print(f"  manifiesto actualizado → {champion.MANIFEST}")
        else:
            print("  --promote sin efecto: ninguna tabla con aptitud hold-out + autorización prospectiva")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
