"""Pisos de cobertura POR MÓDULO crítico y POR CAPA (G3 + E1, planes 2026-07-11/12).

El promedio no puede esconder módulos críticos ni denominadores selectivos:

* **Modo módulos** (default, gate G3): lee el JSON de coverage del job de modelado y
  aplica los pisos por archivo de ``docs/coverage_floors.json`` → ``modules``
  (ledger/promoción/bandas/métricas/…).

* **Modo capas** (``--layers``, gate E1): sobre el JSON de la instrumentación AMPLIA
  (vp_data+pipeline+vp_model+experiments+tools) agrega cobertura por capa con
  DENOMINADORES visibles (capa → archivos → stmts → cubiertas → %), valida los pisos
  por capa + el piso global de ``coverage_floors.json`` → ``layers``/``global``, y
  opcionalmente emite un artefacto JSON (``--json-out``) y un resumen Markdown para
  ``GITHUB_STEP_SUMMARY`` (``--summary``). El resumen SIEMPRE contrasta la cobertura
  del gate selectivo (los 3 módulos de parsing del ``addopts`` de pyproject, piso
  ``fail_under``) contra la global amplia — que nadie vuelva a leer "74%" como
  cobertura del repo.

Los pisos SOLO SUBEN editando ``docs/coverage_floors.json`` en un PR (trinquete
explícito, como debt_baseline); la política de ratchet vive en ese JSON.

    # gate por módulo (job model-tests, tras `make test-model`)
    coverage json -o /tmp/cov.json && python tools/check_coverage_floors.py /tmp/cov.json

    # gate por capa (tras la corrida amplia)
    python tools/check_coverage_floors.py /tmp/cov_broad.json --layers \
        --json-out /tmp/coverage_by_layer.json --summary "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FLOORS = ROOT / "docs" / "coverage_floors.json"
PYPROJECT = ROOT / "pyproject.toml"


def _load_floors() -> dict[str, Any]:
    floors = json.loads(FLOORS.read_text())
    # Compat: el formato pre-E1 era un dict plano {módulo: piso}.
    if "modules" not in floors:
        floors = {"modules": floors}
    return floors


def _check_modules(cov: dict[str, Any], module_floors: dict[str, float]) -> int:
    by_name = {f.split("/")[-1]: v["summary"]["percent_covered"] for f, v in cov["files"].items()}
    problems, report = [], []
    for name, floor in sorted(module_floors.items()):
        got = by_name.get(name)
        if got is None:
            problems.append(f"{name}: sin datos de cobertura (¿salió del cov target?)")
            continue
        mark = "✓" if got >= floor else "✗"
        report.append(f"  {mark} {name}: {got:.1f}% (piso {floor}%)")
        if got < floor:
            problems.append(f"{name}: {got:.1f}% < piso {floor}%")
    print("\n".join(report))
    if problems:
        print(f"✗ PISOS DE COBERTURA ROTOS ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ {len(module_floors)} módulos críticos sobre su piso")
    return 0


def _selective_gate(cov: dict[str, Any]) -> dict[str, Any]:
    """Cobertura del gate por defecto (los --cov= del addopts de pyproject) derivada
    del MISMO json amplio — el numerador/denominador selectivo, sin re-medir."""
    with PYPROJECT.open("rb") as fh:
        py = tomllib.load(fh)
    addopts: list[str] = py["tool"]["pytest"]["ini_options"]["addopts"]
    modules = [a.removeprefix("--cov=").replace(".", "/") + ".py" for a in addopts if a.startswith("--cov=")]
    fail_under = py["tool"]["coverage"]["report"]["fail_under"]
    stmts = covered = 0
    for mod in modules:
        summary = cov["files"].get(mod, {}).get("summary")
        if summary is None:
            continue  # el módulo no aparece en esta medición; el gate real vive en pytest
        stmts += summary["num_statements"]
        covered += summary["covered_lines"]
    pct = 100.0 * covered / stmts if stmts else 0.0
    return {
        "modules": modules,
        "statements": stmts,
        "covered": covered,
        "percent": round(pct, 2),
        "fail_under": fail_under,
    }


def _check_layers(cov: dict[str, Any], floors: dict[str, Any], json_out: Path | None, summary: Path | None) -> int:
    layer_floors: dict[str, Any] = floors.get("layers", {})
    if not layer_floors:
        print("✗ docs/coverage_floors.json no define 'layers'")
        return 1

    stats: dict[str, dict[str, Any]] = {name: {"files": 0, "statements": 0, "covered": 0} for name in layer_floors}
    unmatched: list[str] = []
    for path, data in cov["files"].items():
        file_summary = data["summary"]
        for name, spec in layer_floors.items():
            if any(path.startswith(prefix) for prefix in spec["paths"]):
                st = stats[name]
                st["files"] += 1
                st["statements"] += file_summary["num_statements"]
                st["covered"] += file_summary["covered_lines"]
                break
        else:
            unmatched.append(path)

    problems: list[str] = []
    rows: list[tuple[str, ...]] = []
    for name, spec in layer_floors.items():
        st = stats[name]
        pct = 100.0 * st["covered"] / st["statements"] if st["statements"] else 0.0
        st["percent"] = round(pct, 2)
        st["floor"] = spec["floor"]
        st["ok"] = pct >= spec["floor"]
        if not st["ok"]:
            problems.append(f"capa {name}: {pct:.2f}% < piso {spec['floor']}%")
        rows.append(
            (
                "✓" if st["ok"] else "✗",
                name,
                str(st["files"]),
                str(st["statements"]),
                str(st["covered"]),
                f"{pct:.2f}%",
                f"{spec['floor']}%",
            )
        )

    totals = cov["totals"]
    global_pct = totals["percent_covered"]
    global_floor = floors.get("global", {}).get("floor", 0)
    global_ok = global_pct >= global_floor
    if not global_ok:
        problems.append(f"global amplia: {global_pct:.2f}% < piso {global_floor}%")

    selective = _selective_gate(cov)

    # --- tabla al log (denominadores visibles) ---
    print("Cobertura por capa (instrumentación amplia):")
    print(f"  {'':1s} {'capa':12s} {'files':>5s} {'stmts':>6s} {'cubiertas':>9s} {'%':>7s} {'piso':>5s}")
    for mark, name, files, stmts, cvd, pct, floor in rows:
        print(f"  {mark} {name:12s} {files:>5s} {stmts:>6s} {cvd:>9s} {pct:>7s} {floor:>5s}")
    print(
        f"  · global amplia: {totals['covered_lines']}/{totals['num_statements']} stmts"
        f" = {global_pct:.2f}% (piso {global_floor}%)"
    )
    print(
        f"  · gate selectivo (pyproject, {len(selective['modules'])} módulos de parsing):"
        f" {selective['covered']}/{selective['statements']} stmts = {selective['percent']:.2f}%"
        f" (fail_under {selective['fail_under']}%) — NO es la cobertura del repo"
    )
    if unmatched:
        print(f"  ⚠ {len(unmatched)} archivos medidos sin capa asignada: {unmatched[:5]}")

    if json_out is not None:
        payload = {
            "note": (
                "Cobertura por capa sobre la instrumentación AMPLIA (todas las capas). "
                "El gate por defecto de pytest (fail_under de pyproject) cubre SOLO los "
                "módulos de parsing listados en selective_gate — no leer su % como cobertura del repo."
            ),
            "ratchet_policy": floors.get("ratchet_policy", ""),
            "selective_gate": selective,
            "global": {
                "statements": totals["num_statements"],
                "covered": totals["covered_lines"],
                "percent": round(global_pct, 2),
                "floor": global_floor,
                "ok": global_ok,
            },
            "layers": stats,
        }
        json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"  · artefacto: {json_out}")

    if summary is not None:
        md = [
            "## Cobertura honesta por capa (E1) — denominadores visibles",
            "",
            "| Alcance | Stmts | Cubiertas | % | Piso |",
            "|---|---:|---:|---:|---:|",
            (
                f"| Gate selectivo ({len(selective['modules'])} módulos de parsing, pyproject) "
                f"| {selective['statements']} | {selective['covered']} | {selective['percent']:.2f}% "
                f"| {selective['fail_under']}% |"
            ),
            (
                f"| **Global amplia (todas las capas)** | {totals['num_statements']} "
                f"| {totals['covered_lines']} | {global_pct:.2f}% | {global_floor}% |"
            ),
            "",
            (
                f"⚠️ El {selective['percent']:.1f}% del gate por defecto cubre SOLO "
                f"{selective['statements']} sentencias de parsing — **no** es la cobertura del repo."
            ),
            "",
            "| Capa | Archivos | Stmts | Cubiertas | % | Piso | |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for mark, name, files, stmts, cvd, pct, floor in rows:
            md.append(f"| {name} | {files} | {stmts} | {cvd} | {pct} | {floor} | {mark} |")
        with summary.open("a") as fh:
            fh.write("\n".join(md) + "\n")

    if problems:
        print(f"✗ PISOS POR CAPA ROTOS ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ {len(layer_floors)} capas + global sobre su piso")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cov_json", nargs="?", default="/tmp/cov.json", type=Path)
    parser.add_argument("--layers", action="store_true", help="valida pisos POR CAPA (json amplio)")
    parser.add_argument("--json-out", type=Path, default=None, help="artefacto coverage_by_layer.json")
    parser.add_argument("--summary", type=Path, default=None, help="append Markdown (GITHUB_STEP_SUMMARY)")
    args = parser.parse_args()

    cov = json.loads(args.cov_json.read_text())
    floors = _load_floors()
    if args.layers:
        return _check_layers(cov, floors, args.json_out, args.summary)
    return _check_modules(cov, floors["modules"])


if __name__ == "__main__":
    raise SystemExit(main())
