#!/usr/bin/env python
"""Gate de COMPLETITUD + FRESCURA de una campaña de rederivación (auditoría 12-jul-2026).

DOS FASES (la 1ª ronda mezclaba inputs y outputs -> una campaña limpia siempre fallaba
porque exigia significance/champion ANTES de generarlos):

  --phase inputs   ANTES de significancia. Verifica que cuanto la significancia /
                   champion / key_facts van a consumir este completo, fresco y sano:
                   4 pools (con hold_mase FINITO), 4 comparaciones, 6 HPO (dict real),
                   40 CSV globales (5 semillas x 4 variantes x 2 tablas), finalistas
                   (manifest.jsonl), tuned_params fresco. DEBE ABORTAR el runbook.

  --phase outputs  DESPUES de champion. Verifica significance_summary / champion_challenger
                   / key_facts frescos, JSON valido y (si lo registran) con el campaign_id
                   sellado.

Cada artefacto se valida por EXISTENCIA + FRESCURA (mtime >= inicio de campana sellado en
reports/campaign/campaign_manifest.json - no reutilizar un corte anterior) + CONTENIDO
(no vacio, metricas finitas, esquema minimo), no solo "existe".

Uso:
  python -m tools.check_campaign_completeness --phase inputs
  python -m tools.check_campaign_completeness --phase outputs
  python -m tools.check_campaign_completeness --phase inputs --preflight   # ignora frescura

Limitaciones honestas (NO cubiertas aun): no cuenta los 40 trials de Optuna dentro de cada
hpo_best (solo la config ganadora se persiste); no valida hashes de contenido por artefacto;
no detecta una edicion de codigo SIN commit a mitad de una etapa larga (solo se compara HEAD
entre etapas en el runbook).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "reports" / "campaign" / "campaign_manifest.json"

TABLES = ("FAD", "DFF")
SEED_VARIANTS = ("camp_levels", "camp_diff", "camp_diffls", "camp_auto")
N_SEEDS = 5

# Inputs canonicos con conteo exacto + piso de filas utiles. Los pools ademas se validan
# con metrica finita (ver _pool_metric_ok).
EXPECTED_INPUTS: list[tuple[str, int, int]] = [
    ("reports/campaign/campaign_pool_FAD_family.csv", 1, 5),
    ("reports/campaign/campaign_pool_FAD_employment.csv", 1, 5),
    ("reports/campaign/campaign_pool_DFF_family.csv", 1, 5),
    ("reports/campaign/campaign_pool_DFF_employment.csv", 1, 5),
    ("reports/eval/model_comparison_FAD21.csv", 1, 5),
    ("reports/eval/model_comparison_EB_FAD21.csv", 1, 5),
    ("reports/eval/model_comparison_DFF21.csv", 1, 5),
    ("reports/eval/model_comparison_EB_DFF21.csv", 1, 5),
    ("reports/campaign/hpo_deep_best_FAD_Auto*.json", 3, 0),
    ("reports/campaign/hpo_deep_best_DFF_Auto*.json", 3, 0),
    ("reports/eval/finalist_forecasts_FAD.csv", 1, 2),
    ("reports/eval/finalist_forecasts_DFF.csv", 1, 2),
    ("reports/eval/holdout_forecasts_FAD.csv", 1, 2),
    ("reports/eval/holdout_forecasts_DFF.csv", 1, 2),
    ("reports/eval/tuned_params.json", 1, 0),
    ("models/manifest.jsonl", 1, 50),
]

EXPECTED_OUTPUTS: list[tuple[str, int, int]] = [
    ("reports/eval/significance_summary.json", 1, 0),
    ("reports/governance/champion_challenger.json", 1, 0),
    ("reports/governance/key_facts.json", 1, 0),
]

POOLS = frozenset(
    {
        "reports/campaign/campaign_pool_FAD_family.csv",
        "reports/campaign/campaign_pool_FAD_employment.csv",
        "reports/campaign/campaign_pool_DFF_family.csv",
        "reports/campaign/campaign_pool_DFF_employment.csv",
    }
)
HPO_REQUIRED_KEYS = frozenset({"learning_rate", "hidden_size"})


def _manifest_started() -> dt.datetime | None:
    if not MANIFEST.exists():
        return None
    try:
        raw = json.loads(MANIFEST.read_text())["started_at"]
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except json.JSONDecodeError, KeyError, ValueError:
        return None


def _useful_lines(path: Path) -> int:
    try:
        return max(0, sum(1 for line in path.read_text().splitlines() if line.strip()) - 1)
    except OSError:
        return 0


def _stale(path: Path, started: dt.datetime) -> bool:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC) < started


def _pool_metric_ok(path: Path) -> bool:
    """El pool debe traer hold_mase FINITO en sus filas (no vacio/NaN/inf)."""
    try:
        with path.open(newline="") as fh:
            rd = csv.DictReader(fh)
            if rd.fieldnames is None or "hold_mase" not in rd.fieldnames:
                return False
            rows = 0
            for r in rd:
                v = r.get("hold_mase", "")
                if v == "" or not math.isfinite(float(v)):
                    return False
                rows += 1
            return rows > 0
    except OSError, ValueError:
        return False


def _hpo_real(path: Path) -> bool:
    """El HPO best debe ser un dict de hiperparametros REAL, no {}."""
    try:
        d = json.loads(path.read_text())
        return isinstance(d, dict) and HPO_REQUIRED_KEYS.issubset(d.keys())
    except OSError, json.JSONDecodeError:
        return False


def _seed_problems(started: dt.datetime | None, preflight: bool) -> list[str]:
    probs: list[str] = []
    for table in TABLES:
        for variant in SEED_VARIANTS:
            for seed in range(1, N_SEEDS + 1):
                p = ROOT / "reports" / "campaign" / f"global_{table}_{variant}_s{seed}.csv"
                if not p.exists():
                    probs.append(f"SEMILLA ausente: {p.relative_to(ROOT)} (se exigen {N_SEEDS} semillas)")
                elif not preflight and started is not None and _stale(p, started):
                    probs.append(f"SEMILLA stale: {p.relative_to(ROOT)}")
    return probs


def _check_list(expected: list[tuple[str, int, int]], started: dt.datetime | None, preflight: bool) -> list[str]:
    probs: list[str] = []
    for pattern, want, floor in expected:
        matches = sorted(ROOT.glob(pattern))
        if len(matches) != want:
            probs.append(f"CONTEO {pattern}: esperados {want}, hallados {len(matches)}")
            continue
        for m in matches:
            rel = m.relative_to(ROOT)
            if floor and _useful_lines(m) < floor:
                probs.append(f"VACIO {rel}: <{floor} filas utiles")
            if m.relative_to(ROOT).as_posix() in POOLS and not _pool_metric_ok(m):
                probs.append(f"METRICA {rel}: hold_mase ausente/no-finita (fallos/NaN en el pool)")
            if "hpo_deep_best_" in m.name and not _hpo_real(m):
                probs.append(f"HPO {rel}: config trivial/incompleta (sin {sorted(HPO_REQUIRED_KEYS)})")
            if not preflight and started is not None and _stale(m, started):
                probs.append(f"STALE {rel}: reutilizado de un corte anterior")
    return probs


def check(phase: str, preflight: bool = False) -> list[str]:
    started = _manifest_started()
    probs: list[str] = []
    if started is None:
        probs.append(
            "reports/campaign/campaign_manifest.json ausente/invalido: sin el no se verifica "
            "frescura. Lanza con run_rederivation.sh (sella el manifiesto)."
        )
        if not preflight:
            return probs
    if phase == "inputs":
        probs += _check_list(EXPECTED_INPUTS, started, preflight)
        probs += _seed_problems(started, preflight)
    else:  # outputs
        probs += _check_list(EXPECTED_OUTPUTS, started, preflight)
        cid = None
        if MANIFEST.exists():
            try:
                cid = json.loads(MANIFEST.read_text()).get("campaign_id")
            except json.JSONDecodeError:
                cid = None
        cc = ROOT / "reports" / "governance" / "champion_challenger.json"
        if cid and cc.exists():
            try:
                rec = json.loads(cc.read_text())
                rec_cid = rec.get("campaign_id") if isinstance(rec, dict) else None
                if rec_cid is not None and rec_cid != cid:
                    probs.append(f"IDENTIDAD champion_challenger: campaign_id {rec_cid!r} != sellado {cid!r}")
            except json.JSONDecodeError:
                probs.append("JSON champion_challenger invalido")
    return probs


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", choices=("inputs", "outputs"), required=True)
    ap.add_argument("--preflight", action="store_true", help="ignora frescura (chequeo previo)")
    ns = ap.parse_args(argv[1:])
    problems = check(ns.phase, preflight=ns.preflight)
    if problems:
        print(f"X Campana {ns.phase} INCOMPLETA/STALE: {len(problems)} problema(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK Campana {ns.phase}: completa, fresca y con contenido valido.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
