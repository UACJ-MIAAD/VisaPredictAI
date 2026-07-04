#!/usr/bin/env python3
"""Guardián de consistencia entre TODOS los artefactos (la máxima del proyecto).

Verifica que web / LaTeX entregable / paper / READMEs / docs digan el MISMO número y no
arrastren claims viejos. Fuente de verdad: ``reports/governance/key_facts.json`` (generada por
``experiments/build_key_facts.py``). Reglas: ``tools/consistency_rules.yml``.

Falla (exit 1) si: (a) un patrón `forbidden` aparece, (b) un `required` falta, o (c) un
número etiquetado (`numeric`) no concuerda con la fuente de verdad. El repo web es opcional
(se chequea si existe en `../VisaPredictAI_web`; en CI se omite con aviso si no está).

Uso:  python tools/check_consistency.py        (o `make consistency`)
      python tools/check_consistency.py --quiet
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
# En CI el repo web se checkouta en otra ruta; VP_WEB_DIR la reubica (default: hermano local).
WEB_DIR = os.environ.get("VP_WEB_DIR", "../VisaPredictAI_web")


def _digits(s: str) -> str:
    """Normaliza un número de prosa/LaTeX (27{,}289, 27\\,289, 27,289) a dígitos puros."""
    return re.sub(r"[^0-9]", "", s)


def _resolve(globs: list[str]) -> list[Path]:
    out: list[Path] = []
    for g in globs:
        g = g.replace("../VisaPredictAI_web", WEB_DIR)
        pattern = g if Path(g).is_absolute() else str(ROOT / g)
        for p in glob.glob(pattern):
            fp = Path(p)
            if fp.is_file():
                out.append(fp)
    return out


def main() -> int:
    quiet = "--quiet" in sys.argv
    kf_facts = json.loads((ROOT / "reports" / "governance" / "key_facts.json").read_text())
    facts = dict(kf_facts)
    # AH5: los hechos del catálogo FE entran al espacio de reglas (el web cita "44 → 1"),
    # pero NO al check KEYFACTS (build_key_facts no emite macros para ellos — fe_facts.tex sí).
    fe_fp = ROOT / "reports" / "fe" / "fe_facts.json"
    if fe_fp.exists():
        fs = json.loads(fe_fp.read_text()).get("feature_selection", {})
        facts.setdefault("fe_sel_in", fs.get("n_features_in"))
        facts.setdefault("fe_sel_final", fs.get("n_selected"))
    rules = yaml.safe_load((ROOT / "tools" / "consistency_rules.yml").read_text())
    sets = {name: _resolve(globs) for name, globs in rules["artifacts"].items()}

    # aviso si el repo web no está montado (CI del repo de datos solo, p. ej.)
    web_missing = "web" in rules["artifacts"] and not sets.get("web")
    if web_missing and not quiet:
        print("⚠ repo web ausente (../VisaPredictAI_web) — se omiten sus chequeos.")

    def files_for(groups: list[str]) -> list[Path]:
        return [f for g in groups for f in sets.get(g, [])]

    def fmt(v: object) -> str:
        return str(v)

    violations: list[str] = []

    # 0) key_facts.tex == key_facts.json (AH1): la prosa macro-izada del deliverable
    # confía en el .tex de macros; si éste se desalinea del .json (edición manual,
    # regeneración parcial), NINGUNA regla de texto lo cazaría — verificar aquí.
    kf_tex = ROOT / "reports" / "latex" / "key_facts.tex"
    if kf_tex.exists():
        def _macro(k: str) -> str:
            return "fact" + "".join(w.capitalize() for w in k.split("_"))

        tex_vals = dict(re.findall(r"\\newcommand\{\\(fact\w+)\}\{([^}]*(?:\{,\}[^}]*)*)\}", kf_tex.read_text()))
        for k, v in kf_facts.items():
            if k.startswith("_") or isinstance(v, (list, dict)):
                continue
            got = tex_vals.get(_macro(k))
            if got is None or got.replace("{,}", "") != str(v).replace(",", ""):
                violations.append(
                    f"KEYFACTS   reports/latex/key_facts.tex  macro \\{_macro(k)}={got!r} != json {k}={v!r} "
                    f"— regenerar con experiments/build_key_facts.py"
                )

    # 1) FORBIDDEN — el patrón no debe aparecer
    for r in rules.get("forbidden", []):
        rx = re.compile(r["pattern"], re.IGNORECASE)
        for f in files_for(r["in"]):
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if line.lstrip().startswith("%"):  # comentarios LaTeX no cuentan
                    continue
                if rx.search(line):
                    violations.append(
                        f"FORBIDDEN  {f.relative_to(ROOT)}:{i}  /{r['pattern']}/  — {r['reason']}\n    > {line.strip()[:120]}"
                    )

    # 2) REQUIRED — al menos una forma debe aparecer en el grupo
    for r in rules.get("required", []):
        val = facts.get(r["fact"], "")
        forms = [re.compile(fr.replace("{" + r["fact"] + "}", re.escape(fmt(val))), re.IGNORECASE) for fr in r["forms"]]
        for g in r["in"]:
            # strip LaTeX comment lines (igual que forbidden/numeric) — un claim requerido
            # NO debe contar como presente si solo vive en una línea comentada con %.
            blobs = [
                "\n".join(ln for ln in f.read_text(errors="ignore").splitlines() if not ln.lstrip().startswith("%"))
                for f in sets.get(g, [])
            ]
            if not blobs:
                continue
            joined = "\n".join(blobs)
            if not any(fx.search(joined) for fx in forms):
                violations.append(
                    f"REQUIRED   grupo '{g}'  falta fact '{r['fact']}'={val}  (formas: {r['forms']}) — {r['reason']}"
                )

    # 3) NUMERIC — todo número etiquetado debe igualar la fuente de verdad
    for r in rules.get("numeric", []):
        want = facts.get(r["fact"])
        rx = re.compile(r["label"], re.IGNORECASE)
        for f in files_for(r["in"]):
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if line.lstrip().startswith("%"):
                    continue
                for m in rx.finditer(line):
                    got = _digits(m.group(1))
                    if got and int(got) != int(want):
                        violations.append(
                            f"NUMERIC    {f.relative_to(ROOT)}:{i}  '{r['fact']}' esperado {want}, encontrado {got}  — {r['reason']}\n    > {line.strip()[:120]}"
                        )

    # 4) DECIMAL — como numeric pero para hechos con decimales (MASE, coberturas):
    # int() truncaría 0.114 a 0, así que se compara como float con tolerancia de
    # redondeo a los decimales del claim (0.090 == 0.09; 0.114 != 0.121).
    for r in rules.get("decimal", []):
        want = float(facts.get(r["fact"]))
        rx = re.compile(r["label"], re.IGNORECASE)
        for f in files_for(r["in"]):
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if line.lstrip().startswith("%"):
                    continue
                for m in rx.finditer(line):
                    try:
                        got_f = float(m.group(1))
                    except ValueError:
                        continue
                    if abs(got_f - want) > 5e-4:  # tolera el redondeo del 3er decimal
                        violations.append(
                            f"DECIMAL    {f.relative_to(ROOT)}:{i}  '{r['fact']}' esperado {want}, encontrado {got_f}  — {r['reason']}\n    > {line.strip()[:120]}"
                        )

    n_files = sum(len(v) for v in sets.values())
    if violations:
        print(f"\n✗ CONSISTENCIA ROTA — {len(violations)} violación(es) en {n_files} archivos:\n")
        for v in violations:
            print("  " + v)
        print("\nReconcilia los artefactos a reports/governance/key_facts.json (la fuente de verdad) y reintenta.")
        return 1
    print(
        f"✓ Consistencia OK — {n_files} artefactos alineados con reports/governance/key_facts.json"
        + (" (repo web omitido)" if web_missing else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
