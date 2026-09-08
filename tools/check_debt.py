"""Trinquete de deuda técnica (E3, plan auditoría 2026-07-11; honesto desde C4).

Cuenta los marcadores de deuda del código de producto y los compara contra la
baseline versionada ``docs/debt_baseline.json``: **ningún conteo puede SUBIR**
(el trinquete solo aprieta). Si un conteo BAJA, el script lo celebra y pide
actualizar la baseline en el mismo PR (decisión visible, nunca automática).

    python tools/check_debt.py            # gate (CI + make check)
    python tools/check_debt.py --update   # reescribe la baseline con los conteos actuales

Métricas: capturas amplias totales y SIN razón declarada (la política del repo:
toda captura amplia lleva ``noqa: BLE001`` o ``broad-catch:`` + su razón),
``type: ignore``, ``noqa`` y marcadores TODO/FIXME/HACK/XXX. Solo capas de
producto (vp_data, pipeline, vp_model, tools, experiments). Stdlib puro.

C4 lo hizo honesto. Antes buscaba subcadenas línea a línea, así que se contaba a
sí mismo dos veces (el texto de este docstring y la comparación de su propia
implementación) y aceptaba como «justificada» cualquier línea que contuviera dos
guiones. Ahora las capturas se cuentan con ``ast`` — código real, nunca texto — y
los marcadores de comentario con ``tokenize``, que distingue un comentario de una
cadena que lo menciona.
"""

from __future__ import annotations

import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "debt_baseline.json"
LAYERS = ("vp_data", "pipeline", "vp_model", "tools", "experiments")
# La politica es explicita: una captura amplia se justifica con la directiva BLE001.
# El regex anterior (que aceptaba tambien un guion doble o una raya) daba por
# justificada casi cualquier linea comentada en espanol: un gate fail-open.
SUPPRESSION = re.compile(r"^#\s*(type:\s*ignore|noqa)\b")
# Una captura amplia se justifica con la directiva que REALMENTE la silencia (`noqa: BLE001`)
# o, cuando el linter no dispara ahí, con el marcador explícito `broad-catch:`. Depender solo
# del `noqa` ataba este gate a las condiciones de disparo de una regla ajena: BLE001 no marca
# los handlers que re-lanzan, así que su directiva era inservible justo donde la razón importa.
JUSTIFICATION = re.compile(r"^#\s*(noqa:\s*[A-Z0-9, ]*\bBLE001\b|broad-catch:\s*\S)")
TODOISH = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
# `except_exception` mide exactamente lo que su nombre dice. Un `except:` desnudo o
# un `except BaseException` son peores, pero contarlos aqui cambiaria la unidad de
# medida y haria incomparable la baseline; van como hallazgo aparte.
BROAD = {"Exception"}
METRICS = ("except_exception", "except_exception_unjustified", "type_ignore", "noqa", "todo_class")


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """Una captura amplia: `except Exception` o `except (A, Exception)`."""
    node = handler.type
    if node is None:
        return False
    names = node.elts if isinstance(node, ast.Tuple) else [node]
    return any(isinstance(n, ast.Name) and n.id in BROAD for n in names)


def _source_files() -> list[Path]:
    return [f for layer in LAYERS for f in sorted((ROOT / layer).glob("*.py"))]


def count_file(source: str) -> dict[str, int]:
    """Los marcadores de UN archivo. Público para que las pruebas no toquen el disco."""
    c = dict.fromkeys(METRICS, 0)
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ExceptHandler) and _is_broad(node):
            c["except_exception"] += 1
            # Un `except` puede escribirse en varias líneas; la justificación puede
            # ir en cualquiera de ellas, así que se mira el rango completo del handler.
            end = node.body[0].lineno - 1 if node.body else node.lineno
            span = "\n".join(lines[node.lineno - 1 : max(end, node.lineno)])
            if not any(JUSTIFICATION.match(c) for c in _comments_in(span)):
                c["except_exception_unjustified"] += 1
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        # Solo comentarios: una directiva citada dentro de una cadena es texto, no una
        # supresion. Y solo DIRECTIVAS: un comentario que las menciona de pasada tampoco
        # suprime nada, que es como este mismo archivo llego a contarse a si mismo.
        if token.type != tokenize.COMMENT:
            continue
        comment = token.string.strip()
        if SUPPRESSION.match(comment):
            key = "type_ignore" if comment.lstrip("# ").startswith("type") else "noqa"
            c[key] += 1
        if TODOISH.search(comment):
            c["todo_class"] += 1
    return c


def _comments_in(span: str) -> list[str]:
    """Los comentarios de un fragmento, ya normalizados, sin depender de tokenize."""
    out = []
    for line in span.splitlines():
        _, sep, rest = line.partition("#")
        if sep:
            out.append(("#" + rest).strip())
    return out


def counts() -> dict[str, int]:
    total = dict.fromkeys(METRICS, 0)
    for f in _source_files():
        for key, value in count_file(f.read_text(errors="ignore")).items():
            total[key] += value
    return total


def load_baseline(raw: str) -> dict[str, int]:
    """La baseline, validada fail-closed: sin claves de más ni de menos, enteros ≥ 0."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("la baseline debe ser un objeto JSON")
    missing = sorted(set(METRICS) - set(data))
    extra = sorted(set(data) - set(METRICS))
    if missing or extra:
        raise ValueError(f"claves de la baseline inválidas (faltan {missing}, sobran {extra})")
    for key, value in data.items():
        # `bool` es subclase de `int`: `True` no es un conteo.
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key}: se esperaba un entero ≥ 0, llegó {value!r}")
    return data


def main() -> int:
    now = counts()
    if "--update" in sys.argv:
        BASELINE.write_text(json.dumps(now, indent=2) + "\n")
        print(f"baseline actualizada → {BASELINE}: {now}")
        return 0
    base = load_baseline(BASELINE.read_text())
    worse = {k: (base[k], v) for k, v in now.items() if v > base[k]}
    better = {k: (base[k], v) for k, v in now.items() if v < base[k]}
    if worse:
        print(f"✗ DEUDA CRECIÓ (el trinquete solo aprieta): {worse}")
        print("  añade la justificación/limpia el marcador, o actualiza la baseline en el PR con razón explícita")
        return 1
    msg = f"✓ Deuda dentro de baseline: {now}"
    if better:
        msg += f" · MEJORÓ {list(better)} — corre `python tools/check_debt.py --update` y commitea"
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
