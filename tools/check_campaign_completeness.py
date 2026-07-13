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
# Columnas de MODELO esperadas en cada CSV de semilla, por variante (verificado 13-jul).
SEED_MODELS = {
    "camp_levels": ("NHITS", "PatchTST", "TiDE", "BiTCN"),
    "camp_diff": ("NHITS", "PatchTST", "TiDE", "BiTCN"),
    "camp_diffls": ("NHITS", "PatchTST", "TiDE", "BiTCN"),
    "camp_auto": ("AutoBiTCN", "AutoTiDE", "AutoNHITS"),
}
SEED_BASE_COLS = ("unique_id", "ds", "y")

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
TUNED_GROUPS = frozenset({"FAD_family", "DFF_family", "FAD_employment", "DFF_employment"})
MANIFEST_LOCAL_FLOOR = 250  # el productor manifiesta ~300 locales
MANIFEST_GLOBAL_FLOOR = 6  # save_finalists_deep manifiesta ~10 globales (piso conservador)
MANIFEST_ENTRY_KEYS = frozenset({"model", "type", "path", "git_sha", "panel_hash"})
KEY_FACTS_REQUIRED = frozenset({"n_series_structural", "n_obs", "fad_champion_mean", "dff_champion_mean", "n_models"})
CHAMPION_TABLE_KEYS = frozenset({"champion", "champion_mean", "challengers"})

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


def _pool_rows(path: Path) -> tuple[int, int] | None:
    """(series elegibles distintas, n modelos distintos) o None si faltan columnas."""
    try:
        with path.open(newline="") as fh:
            rd = csv.DictReader(fh)
            cols = set(rd.fieldnames or ())
            if not {"country", "category", "hold_mase", "model"} <= cols:
                return None
            series, models = set(), set()
            for r in rd:
                models.add(r.get("model", ""))
                if _finite(r.get("hold_mase", "")):
                    series.add((r["country"], r["category"]))
            return len(series), len(models)
    except OSError:
        return None


def _seed_content_ok(path: Path, variant: str) -> str | None:
    """El CSV de semilla debe traer unique_id/ds/y + las columnas de modelo de su variante,
    con pronosticos FINITOS en >=1 fila. Devuelve el motivo del fallo o None si OK."""
    try:
        with path.open(newline="") as fh:
            rd = csv.DictReader(fh)
            cols = set(rd.fieldnames or ())
            need = set(SEED_BASE_COLS) | set(SEED_MODELS[variant])
            if not need <= cols:
                return f"faltan columnas {sorted(need - cols)}"
            finite_rows = 0
            for r in rd:
                if any(_finite(r.get(m, "")) for m in SEED_MODELS[variant]):
                    finite_rows += 1
            return None if finite_rows >= 1 else "0 filas con pronostico finito"
    except OSError:
        return "ilegible"


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
    got = _pool_rows(path)
    if got is None:
        return [f"POOL {rel}: faltan columnas country/category/hold_mase/model"]
    n_series, n_models = got
    probs = []
    if n_series < floor:
        probs.append(f"POOL {rel}: {n_series} series elegibles < piso {floor} (¿pool degenerado?)")
    if n_models < 10:  # el pool compara ~24 modelos; <10 es un pool truncado
        probs.append(f"POOL {rel}: solo {n_models} modelos (esperados ~24) — pool truncado")
    return probs


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
    probs = []
    for gbm in sorted(TUNED_GBM_KEYS):
        groups = d.get(gbm)
        if not isinstance(groups, dict) or not TUNED_GROUPS.issubset(groups.keys()):
            faltan = sorted(TUNED_GROUPS - set(groups.keys())) if isinstance(groups, dict) else "no-dict"
            probs.append(f"TUNED {gbm}: faltan grupos tabla/bloque {faltan} (se exigen 4 por modelo)")
    return probs


def _check_manifest(path: Path) -> list[str]:
    try:
        entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except OSError, json.JSONDecodeError:
        return [f"MANIFEST {path.relative_to(ROOT)}: JSONL invalido"]
    probs = []
    bad = sum(1 for e in entries if not isinstance(e, dict) or not MANIFEST_ENTRY_KEYS <= set(e.keys()))
    if bad:
        probs.append(f"MANIFEST: {bad} entradas sin las claves {sorted(MANIFEST_ENTRY_KEYS)}")
    missing = sum(1 for e in entries if isinstance(e, dict) and "path" in e and not (ROOT / e["path"]).exists())
    if missing:
        probs.append(f"MANIFEST: {missing} rutas de modelo no existen en disco")
    n_local = sum(1 for e in entries if isinstance(e, dict) and e.get("type") == "local")
    n_global = sum(1 for e in entries if isinstance(e, dict) and str(e.get("type", "")).startswith("global"))
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
            # sufijos como STRING: {"1".."5"} exacto. Asi s01/sOLD/s1_backup/s6 (que los
            # consumidores por prefijo `variant_s*` SI recogerian) se marcan como sobrantes.
            present = {p.stem.rsplit("_s", 1)[1] for p in camp.glob(f"global_{table}_{variant}_s*.csv")}
            want = {str(i) for i in range(1, N_SEEDS + 1)}
            if present != want:
                extra = sorted(present - want)
                missing = sorted(want - present)
                probs.append(
                    f"SEMILLAS {table}/{variant}: sufijos {sorted(present)} != {sorted(want)}"
                    + (f" (sobra {extra} — contamina la agregacion por prefijo)" if extra else "")
                    + (f" (falta {missing})" if missing else "")
                )
                continue
            for seed in want:
                p = camp / f"global_{table}_{variant}_s{seed}.csv"
                bad = _seed_content_ok(p, variant)
                if bad is not None:
                    probs.append(f"SEMILLA {p.relative_to(ROOT)}: {bad}")
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


def _sealed(field: str) -> str | None:
    if not MANIFEST.exists():
        return None
    try:
        return json.loads(MANIFEST.read_text()).get(field)
    except json.JSONDecodeError:
        return None


def _check_outputs_content(started: dt.datetime | None) -> list[str]:
    probs: list[str] = []
    # significancia: ranking Y dm deben ser dicts anidados con FAD y DFF (no {})
    sig = _load_json(ROOT / "reports" / "eval" / "significance_summary.json")
    if not isinstance(sig, dict):
        probs.append("SIGNIFICANCIA: no es un objeto")
    else:
        for key in ("ranking", "dm"):
            sub = sig.get(key)
            if not isinstance(sub, dict) or not {"FAD", "DFF"}.issubset(sub.keys()):
                probs.append(f"SIGNIFICANCIA.{key}: debe ser dict con FAD/DFF no vacio")
    # key_facts: deben estar las claves INSIGNIA (no 20 claves arbitrarias)
    kf = _load_json(ROOT / "reports" / "governance" / "key_facts.json")
    if not isinstance(kf, dict) or not KEY_FACTS_REQUIRED.issubset(kf.keys()):
        faltan = sorted(KEY_FACTS_REQUIRED - set(kf.keys())) if isinstance(kf, dict) else "no-dict"
        probs.append(f"KEY_FACTS: faltan claves insignia {faltan}")
    # champion: FAD/DFF con champion_mean FINITO + campaign_id Y git_sha == sellados
    cc = _load_json(ROOT / "reports" / "governance" / "champion_challenger.json")
    if not isinstance(cc, dict) or not {"FAD", "DFF"}.issubset(cc.keys()):
        probs.append("CHAMPION: faltan tablas FAD/DFF")
    else:
        for tbl in ("FAD", "DFF"):
            d = cc.get(tbl)
            if not isinstance(d, dict) or not CHAMPION_TABLE_KEYS.issubset(d.keys()):
                probs.append(f"CHAMPION.{tbl}: faltan {sorted(CHAMPION_TABLE_KEYS)}")
            elif not _finite(str(d.get("champion_mean", ""))):
                probs.append(f"CHAMPION.{tbl}: champion_mean no finito")
        for field in ("campaign_id", "git_sha"):
            sealed = _sealed(field)
            rec = cc.get(field)
            if sealed is None:
                probs.append(f"IDENTIDAD: el manifiesto no sella {field} (campaña sin identidad)")
            elif rec is None:
                probs.append(f"CHAMPION: sin {field} (el productor debe sellarlo)")
            elif rec != sealed:
                probs.append(f"CHAMPION: {field} {rec!r} != sellado {sealed!r}")
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
