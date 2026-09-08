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


def _num_eq(a: str, b: str) -> bool:
    """Igualdad tolerante al formato: ``0.120 == 0.12`` (macros MASE a 3 decimales) pero
    ``0.12 != 0.13``. Cae a comparación exacta de string para valores no numéricos (fechas)."""
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


def _macro_name(k: str) -> str:
    """Nombre de la macro LaTeX de un fact key (espejo de build_key_facts.macro():
    dígitos deletreados porque LaTeX no admite dígitos en nombres de comando)."""
    digits = {
        "0": "Zero",
        "1": "One",
        "2": "Two",
        "3": "Three",
        "4": "Four",
        "5": "Five",
        "6": "Six",
        "7": "Seven",
        "8": "Eight",
        "9": "Nine",
    }
    name = "fact" + "".join(w.capitalize() for w in k.split("_"))
    return "".join(digits.get(c, c) for c in name)


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


# ── F3 · contratos .tex <-> .json ───────────────────────────────────────────────────────
# El valor admite la coma tipográfica `{,}` de LaTeX: `[^}]*` sola truncaba `27{,}911`
# en `27{,` sin fallar (el `}` de la coma cerraba el macro), y el conteo formateado
# quedaba comparándose contra un fragmento.
_MACRO_RX = re.compile(r"\\newcommand\{\\(\w+)\}\{((?:[^{}]|\{,\})*)\}")


def _authority_value(data: dict, expr: str):
    """Autoridad de una macro: ruta punteada, `len:ruta` o `first_escaped:ruta`."""
    op, _, path = expr.partition(":")
    if not path:
        op, path = "value", expr
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    if op == "len":
        if not isinstance(node, (list, dict)):
            raise TypeError(f"{path}: len: exige lista u objeto, no {type(node).__name__}")
        return len(node)
    if op == "first_escaped":
        if not isinstance(node, list):
            raise TypeError(f"{path}: first_escaped: exige lista, no {type(node).__name__}")
        return (node[0] if node else "—").replace("_", r"\_")
    return node


def _macros_contract(contract: dict, tex_text: str, data: dict) -> list[str]:
    """Cada macro del .tex tiene autoridad y cada autoridad tiene macro. Cerrado en ambos sentidos."""
    prefix, out = contract["prefix"], []
    encontradas = _MACRO_RX.findall(tex_text)
    nombres = [n for n, _ in encontradas if n.startswith(prefix)]
    for dup in sorted({n for n in nombres if nombres.count(n) > 1}):
        out.append(f"TEXJSON    {contract['tex']}  macro duplicada \\{dup}")
    tex_vals = {n: v for n, v in encontradas if n.startswith(prefix)}

    if contract["authority"] == "top_level_scalars":
        esperado = {
            f"{prefix}{_macro_name(k)[len('fact') :] if prefix == 'fact' else _macro_name(k)}": v
            for k, v in data.items()
            if not k.startswith("_") and not isinstance(v, (list, dict))
        }
    else:
        esperado = {}
        for sufijo, expr in contract["map"].items():
            try:
                esperado[f"{prefix}{sufijo}"] = _authority_value(data, expr)
            except (KeyError, TypeError) as exc:
                out.append(f"TEXJSON    {contract['json']}  autoridad inválida para \\{prefix}{sufijo}: {exc}")
    sufijo_fmt = contract.get("formatted_suffix")
    if sufijo_fmt:
        # La variante formateada comparte autoridad con su macro base: el generador solo la emite
        # para los conteos grandes, así que se declara cuando existe y se compara sin separador.
        for nombre in list(esperado):
            if f"{nombre}{sufijo_fmt}" in tex_vals:
                esperado[f"{nombre}{sufijo_fmt}"] = esperado[nombre]
    for nombre in sorted(set(tex_vals) - set(esperado)):
        out.append(f"TEXJSON    {contract['tex']}  macro \\{nombre} SIN autoridad en {contract['json']}")
    for nombre in sorted(set(esperado) - set(tex_vals)):
        out.append(f"TEXJSON    {contract['tex']}  falta la macro \\{nombre} — regenerar con {contract['generator']}")
    for nombre in sorted(set(tex_vals) & set(esperado)):
        got, want = tex_vals[nombre].replace("{,}", ""), str(esperado[nombre]).replace(",", "")
        if not _num_eq(got, want) and got != want:
            out.append(
                f"TEXJSON    {contract['tex']}  \\{nombre}={tex_vals[nombre]!r} != {contract['json']} {esperado[nombre]!r}"
                f" — regenerar con {contract['generator']}"
            )
    return out


def _table_rows(tex_text: str, sticky: bool) -> list[list[str]]:
    """Las filas entre `\\midrule` y `\bottomrule`, con la primera celda pegajosa si toca."""
    cuerpo = tex_text.split("\\midrule", 1)[-1].split("\\bottomrule", 1)[0]
    filas, anterior = [], ""
    for linea in cuerpo.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("%") or linea.startswith("\\"):
            continue
        celdas = [c.strip() for c in linea.rstrip("\\").split("&")]
        if sticky:
            if celdas[0]:
                anterior = celdas[0]
            else:
                celdas[0] = anterior
        filas.append(celdas)
    return filas


def _table_contract(contract: dict, tex_text: str, data: dict) -> list[str]:
    """Cada fila del .tex es una fila del JSON y viceversa; nada ambiguo ni duplicado."""
    cols, out = contract["columns"], []
    filas = _table_rows(tex_text, bool(contract.get("sticky_first_column")))
    for i, celdas in enumerate(filas, 1):
        if len(celdas) != len(cols):
            out.append(f"TEXJSON    {contract['tex']}  fila {i} con {len(celdas)} celdas, se esperaban {len(cols)}")
    filas = [f for f in filas if len(f) == len(cols)]
    tex_idx: dict[tuple[str, str], list[str]] = {}
    for celdas in filas:
        clave = (celdas[0], celdas[1])
        if clave in tex_idx:
            out.append(f"TEXJSON    {contract['tex']}  fila duplicada para {clave}")
        tex_idx[clave] = celdas
    json_idx = {}
    for grupo in contract["groups"]:
        nodo = data.get(grupo)
        ruta = contract["rows_from"].format(group=grupo).split(".", 1)[1]
        if not isinstance(nodo, dict) or not isinstance(nodo.get(ruta), list):
            out.append(f"TEXJSON    {contract['json']}  falta {grupo}.{ruta} o no es una lista")
            continue
        for fila in nodo[ruta]:
            json_idx[(grupo, str(fila["h"]))] = fila
    for clave in sorted(set(tex_idx) - set(json_idx)):
        out.append(f"TEXJSON    {contract['tex']}  fila {clave} SIN autoridad en {contract['json']}")
    for clave in sorted(set(json_idx) - set(tex_idx)):
        out.append(f"TEXJSON    {contract['tex']}  falta la fila {clave} — regenerar con {contract['generator']}")
    for clave in sorted(set(tex_idx) & set(json_idx)):
        celdas, fila = tex_idx[clave], json_idx[clave]
        for col, celda in zip(cols[2:], celdas[2:], strict=True):
            if col == "sig":
                esperado = "$\\checkmark$" if fila["sig"] else "--"
                if celda != esperado:
                    out.append(f"TEXJSON    {contract['tex']}  {clave} sig={celda!r} != {esperado!r}")
                continue
            if not _num_eq(celda.lstrip("+"), str(fila[col])):
                out.append(
                    f"TEXJSON    {contract['tex']}  {clave} {col}={celda!r} != {contract['json']} {fila[col]!r}"
                    f" — regenerar con {contract['generator']}"
                )
    return out


def _tex_json_violations(rules: dict) -> list[str]:
    """Los contratos declarados en `tex_json`. Un contrato cuyo .tex o .json falte es un fallo."""
    out: list[str] = []
    for contract in rules.get("tex_json", []):
        tex_path, json_path = ROOT / contract["tex"], ROOT / contract["json"]
        if not tex_path.exists() or not json_path.exists():
            falta = contract["tex"] if not tex_path.exists() else contract["json"]
            out.append(f"TEXJSON    contrato roto: falta {falta}")
            continue
        data = json.loads(json_path.read_text())
        tex_text = tex_path.read_text()
        out += (_macros_contract if contract["kind"] == "macros" else _table_contract)(contract, tex_text, data)
    return out


RULES_PATH = ROOT / "tools" / "consistency_rules.yml"


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
    rules = yaml.safe_load(RULES_PATH.read_text())
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

    # 0) TEX_JSON — contratos .tex <-> .json, CERRADOS (F3). Antes esto era un bloque
    # cableado que solo miraba key_facts: fe_facts y la tabla de horizonte podían
    # desalinearse sin que nada lo dijera, porque ninguna regla de texto mira dentro de
    # un archivo generado. Ahora los contratos se declaran y se validan por igual.
    violations += _tex_json_violations(rules)

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
    # F3: un grupo CONGELADO (la propuesta entregada) no admite reglas `required`: exigirle
    # decir algo nuevo obligaría a reescribir un documento que ya se entregó. Se vigila con
    # tripwires, no con obligaciones. El intento es un fallo del propio contrato, no del texto.
    congelados = set(rules.get("frozen", []))
    for r in rules.get("required", []):
        invasores = sorted(congelados.intersection(r["in"]))
        if invasores:
            violations.append(
                f"CONTRATO   regla `required` sobre grupo(s) congelado(s) {invasores} "
                f"(fact {r['fact']!r}) — un documento entregado no se reescribe; usa un tripwire"
            )
            continue
        val = facts.get(r["fact"], "")
        # acepta el literal O la macro derivada (\factXxx, opcionalmente con {}) en el MISMO
        # contexto de cada forma: la prosa macro-izada satisface el REQUIRED sin re-teclear el valor.
        mac = r"\\" + _macro_name(r["fact"]) + r"(?:\{\})?"
        forms = []
        for fr in r["forms"]:
            forms.append(re.compile(fr.replace("{" + r["fact"] + "}", re.escape(fmt(val))), re.IGNORECASE))
            forms.append(re.compile(fr.replace("{" + r["fact"] + "}", mac), re.IGNORECASE))
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
        if want is None:  # fact ausente (p.ej. fe_facts sin generar) — no hay verdad que comparar
            continue
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
        raw = facts.get(r["fact"])
        if raw is None:
            continue
        want = float(raw)
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
