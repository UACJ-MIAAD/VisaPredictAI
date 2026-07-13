#!/usr/bin/env python
"""Gate de COMPLETITUD + FRESCURA + CONTRATO de una campana de rederivacion.

DOS FASES (auditoria 12/13-jul-2026). La 1a ronda mezclaba inputs y outputs (una campana
limpia siempre fallaba); la 2a validaba solo forma (existencia/mtime/piso) y chocaba con
los productores. Ahora cada artefacto se valida contra el CONTRATO REAL de quien lo produce.

  --phase inputs   ANTES de significancia. ABORTA el runbook (exit 4). Verifica:
    - 4 pools: >= piso de SERIES ELEGIBLES con hold_mase FINITO (los no-finitos son series
      inelegibles/no-convergidas, legitimos; run_comparison los deja a proposito).
    - 4 comparaciones no vacias.
    - 6 HPO best: dict con las claves REALES POR MODELO (BiTCN/NHITS/TiDE difieren; NHITS
      no tiene hidden_size -> exigirlo rechazaba un AutoNHITS valido).
    - Semillas: EXACTAMENTE {s1..s5} por variante (una s6 vieja contamina la agregacion),
      cada una no vacia y fresca.
    - finalists/holdout no vacios; tuned_params con las 3 llaves GBM; manifest con modelos
      LOCALES y GLOBALES (deep) frescos.
  --phase outputs  DESPUES de champion. Verifica significance {ranking,dm}, champion
    {FAD,DFF} + campaign_id OBLIGATORIO == sellado, key_facts no-trivial; todos frescos.

Frescura = mtime >= inicio de campana sellado en reports/campaign/campaign_manifest.json.

Uso: python -m tools.check_campaign_completeness --phase {inputs|outputs} [--preflight]

Limitaciones honestas: los pisos de elegibilidad por bloque son conservadores; el ideal es
un MANIFIESTO DE COBERTURA emitido por run_comparison (n_elegibles por serie). No cuenta los
40 trials de Optuna dentro de cada hpo_best (solo la config ganadora se persiste), ni valida
hashes de contenido, ni detecta una edicion de codigo SIN commit a mitad de una etapa larga.
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

# HPO best: claves REALES por modelo (verificadas 13-jul contra run_global_deep).
_HPO_COMMON = frozenset({"learning_rate", "max_steps", "input_size", "scaler_type"})
HPO_KEYS = {
    "AutoBiTCN": _HPO_COMMON | {"hidden_size", "dropout"},
    "AutoNHITS": _HPO_COMMON | {"n_pool_kernel_size", "n_freq_downsample"},
    "AutoTiDE": _HPO_COMMON | {"hidden_size", "decoder_output_dim"},
}

# Series ELEGIBLES (distintas, con hold_mase finito) esperadas por pool. La piloto familiar
# es 5x5=25; empleo es mas esparso por series EB cortas. Pisos conservadores bajo lo medido
# (FAD_fam 25 / FAD_emp 30 / DFF_fam 25 / DFF_emp 16) para tolerar variacion y cazar un pool
# degenerado/vacio sin exigir cobertura total (que rechazaria los no-finitos legitimos).
POOL_ELIGIBLE_FLOOR = {
    "campaign_pool_FAD_family.csv": 20,
    "campaign_pool_DFF_family.csv": 20,
    "campaign_pool_FAD_employment.csv": 12,
    "campaign_pool_DFF_employment.csv": 8,
}
TUNED_GBM_KEYS = frozenset({"catboost", "lightgbm", "xgboost"})
MANIFEST_LOCAL_FLOOR = 50
MANIFEST_GLOBAL_FLOOR = 1  # al menos un modelo global_deep manifestado
KEY_FACTS_MIN_KEYS = 20
SIG_REQUIRED = frozenset({"ranking", "dm"})

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
    ("models/manifest.jsonl", 1, MANIFEST_LOCAL_FLOOR),
]
EXPECTED_OUTPUTS: list[tuple[str, int, int]] = [
    ("reports/eval/significance_summary.json", 1, 0),
    ("reports/governance/champion_challenger.json", 1, 0),
    ("reports/governance/key_facts.json", 1, 0),
]


def _manifest_started() -> dt.datetime | None:
    if not MANIFEST.exists():
        return None
    try:
        raw = json.loads(MANIFEST.read_text())["started_at"]
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except json.JSONDecodeError, KeyError, ValueError:
        return None


def _sealed_campaign_id() -> str | None:
    if not MANIFEST.exists():
        return None
    try:
        return json.loads(MANIFEST.read_text()).get("campaign_id")
    except json.JSONDecodeError:
        return None


def _useful_lines(path: Path) -> int:
    try:
        return max(0, sum(1 for line in path.read_text().splitlines() if line.strip()) - 1)
    except OSError:
        return 0


def _stale(path: Path, started: dt.datetime) -> bool:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC) < started


def _finite(v: str) -> bool:
    try:
        return math.isfinite(float(v))
    except ValueError:
        return False


def _eligible_series(path: Path) -> int:
    """Series distintas (country,category) con hold_mase FINITO en el pool."""
    try:
        with path.open(newline="") as fh:
            rd = csv.DictReader(fh)
            if rd.fieldnames is None or not {"country", "category", "hold_mase"} <= set(rd.fieldnames):
                return -1
            return len({(r["country"], r["category"]) for r in rd if _finite(r.get("hold_mase", ""))})
    except OSError:
        return -1


def _seed_metric_ok(path: Path) -> bool:
    """Un CSV de semilla no vacio: header + >=1 fila de datos."""
    return _useful_lines(path) >= 1


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        return None


def _check_pool(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    floor = POOL_ELIGIBLE_FLOOR.get(path.name)
    if floor is None:
        return []
    n = _eligible_series(path)
    if n < 0:
        return [f"POOL {rel}: columnas country/category/hold_mase ausentes"]
    if n < floor:
        return [f"POOL {rel}: {n} series elegibles (hold_mase finito) < piso {floor} (¿pool degenerado?)"]
    return []


def _check_hpo(path: Path) -> list[str]:
    model = next((m for m in HPO_KEYS if path.name.endswith(f"{m}.json")), None)
    if model is None:
        return [f"HPO {path.relative_to(ROOT)}: modelo no reconocido"]
    d = _load_json(path)
    if not isinstance(d, dict) or not HPO_KEYS[model].issubset(d.keys()):
        faltan = sorted(HPO_KEYS[model] - set(d.keys())) if isinstance(d, dict) else "no-dict"
        return [f"HPO {path.relative_to(ROOT)} ({model}): faltan claves {faltan}"]
    return []


def _check_tuned(path: Path) -> list[str]:
    d = _load_json(path)
    if not isinstance(d, dict) or not TUNED_GBM_KEYS.issubset(d.keys()):
        return [f"TUNED {path.relative_to(ROOT)}: faltan llaves GBM {sorted(TUNED_GBM_KEYS)}"]
    return []


def _check_manifest(path: Path) -> list[str]:
    try:
        entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except OSError, json.JSONDecodeError:
        return [f"MANIFEST {path.relative_to(ROOT)}: JSONL invalido"]
    if any(not isinstance(e, dict) or "model" not in e or "type" not in e for e in entries):
        return [f"MANIFEST {path.relative_to(ROOT)}: entradas sin model/type"]
    n_local = sum(1 for e in entries if e.get("type") == "local")
    n_global = sum(1 for e in entries if str(e.get("type", "")).startswith("global"))
    probs = []
    if n_local < MANIFEST_LOCAL_FLOOR:
        probs.append(f"MANIFEST: {n_local} modelos locales < {MANIFEST_LOCAL_FLOOR}")
    if n_global < MANIFEST_GLOBAL_FLOOR:
        probs.append(
            f"MANIFEST: {n_global} modelos globales (deep) < {MANIFEST_GLOBAL_FLOOR} (¿falta save_finalists_deep?)"
        )
    return probs


def _seed_problems(started: dt.datetime | None, preflight: bool) -> list[str]:
    probs: list[str] = []
    camp = ROOT / "reports" / "campaign"
    for table in TABLES:
        for variant in SEED_VARIANTS:
            present = {
                int(p.stem.rsplit("_s", 1)[1])
                for p in camp.glob(f"global_{table}_{variant}_s*.csv")
                if p.stem.rsplit("_s", 1)[1].isdigit()
            }
            want = set(range(1, N_SEEDS + 1))
            if present != want:
                extra = sorted(present - want)
                missing = sorted(want - present)
                probs.append(
                    f"SEMILLAS {table}/{variant}: conjunto {sorted(present)} != {sorted(want)}"
                    + (f" (sobra {extra})" if extra else "")
                    + (f" (falta {missing})" if missing else "")
                )
                continue
            for seed in want:
                p = camp / f"global_{table}_{variant}_s{seed}.csv"
                if not _seed_metric_ok(p):
                    probs.append(f"SEMILLA vacia: {p.relative_to(ROOT)}")
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
            if m.name in POOL_ELIGIBLE_FLOOR:
                probs += _check_pool(m)
            if "hpo_deep_best_" in m.name:
                probs += _check_hpo(m)
            if m.name == "tuned_params.json":
                probs += _check_tuned(m)
            if m.name == "manifest.jsonl":
                probs += _check_manifest(m)
            if not preflight and started is not None and _stale(m, started):
                probs.append(f"STALE {rel}: reutilizado de un corte anterior")
    return probs


def _check_outputs_content(started: dt.datetime | None) -> list[str]:
    probs: list[str] = []
    sig = _load_json(ROOT / "reports" / "eval" / "significance_summary.json")
    if not isinstance(sig, dict) or not SIG_REQUIRED.issubset(sig.keys()):
        probs.append(f"SIGNIFICANCIA: faltan claves {sorted(SIG_REQUIRED)}")
    kf = _load_json(ROOT / "reports" / "governance" / "key_facts.json")
    if not isinstance(kf, dict) or len(kf) < KEY_FACTS_MIN_KEYS:
        probs.append(f"KEY_FACTS: trivial (<{KEY_FACTS_MIN_KEYS} claves)")
    cc = _load_json(ROOT / "reports" / "governance" / "champion_challenger.json")
    sealed = _sealed_campaign_id()
    if not isinstance(cc, dict) or not {"FAD", "DFF"}.issubset(cc.keys()):
        probs.append("CHAMPION: faltan tablas FAD/DFF")
    elif sealed is not None:
        rec_cid = cc.get("campaign_id")
        if rec_cid is None:
            probs.append("CHAMPION: sin campaign_id (el productor debe sellarlo)")
        elif rec_cid != sealed:
            probs.append(f"CHAMPION: campaign_id {rec_cid!r} != sellado {sealed!r}")
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
    else:
        probs += _check_list(EXPECTED_OUTPUTS, started, preflight)
        probs += _check_outputs_content(started)
    return probs


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", choices=("inputs", "outputs"), required=True)
    ap.add_argument("--preflight", action="store_true", help="ignora frescura (chequeo previo)")
    ns = ap.parse_args(argv[1:])
    problems = check(ns.phase, preflight=ns.preflight)
    if problems:
        print(f"X Campana {ns.phase} INCOMPLETA/INVALIDA: {len(problems)} problema(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK Campana {ns.phase}: completa, fresca y valida contra el contrato.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
