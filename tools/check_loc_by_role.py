"""C9 · LOC por rol: cada archivo gobernado tiene exactamente un papel, y se mide.

El repositorio ya sabía clasificar entrypoints de ``experiments/`` y poner pisos de cobertura por
capa, pero **nunca había contado su propio tamaño**: la afirmación que abrió esta épica —que el
tooling llegó a pesar 1.85 veces el producto— se midió a mano una tarde y no volvió a comprobarse.

El universo es lo que ``git ls-files`` enumera: lo GOBERNADO. Los entornos (``ante/``, ``.vp_envs/``)
no aparecen porque no están versionados, y eso se comprueba en vez de suponerse. Las reglas viven en
``docs/loc_roles.json`` en dos niveles: las EXACTAS declaran la excepción y ganan sobre las de GLOB;
entre globs no se admite empate. Fail-closed en los cuatro modos: archivo sin regla, dos globs que lo
reclaman, dos reglas exactas para la misma ruta, y regla exacta sin archivo en disco.

La razón de la política mira SOLO líneas de código (``.py``, ``.sh``, ``.sql``): un contrato JSON
cuenta como archivo de su rol, pero no infla numerador ni denominador. El único número escrito a mano
es el techo de esa razón; todo lo demás se mide.

    python tools/check_loc_by_role.py            # gate (CI + make check)
    python tools/check_loc_by_role.py --json     # la medición, para consumirla
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "docs" / "loc_roles.json"
CODE_SUFFIXES = frozenset({".py", ".sh", ".sql"})
# Los directorios de entorno NO deben estar gobernados. Si alguno apareciera, la medida entera
# perdería sentido (millones de líneas de terceros), así que se comprueba antes de contar.
ENV_PREFIXES = ("ante/", "ante_nf/", "ante_tab/", "ante_tfm/", ".vp_envs/")


def governed_files(root: Path) -> list[str]:
    """Las rutas versionadas, ordenadas: el universo es lo que el repositorio gobierna."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True).stdout
    return sorted(p for p in out.split("\0") if p)


def matches(path: str, pattern: str) -> bool:
    """Glob por segmentos, con `**` = cero o más segmentos. Determinista y sin tocar el disco."""
    return _match_segments(path.split("/"), pattern.split("/"))


def _match_segments(path: list[str], pat: list[str]) -> bool:
    if not pat:
        return not path
    if pat[0] == "**":
        # `**` consume 0..n segmentos; se prueba de menos a más para terminar pronto.
        return any(_match_segments(path[i:], pat[1:]) for i in range(len(path) + 1))
    if not path:
        return False
    return fnmatch.fnmatchcase(path[0], pat[0]) and _match_segments(path[1:], pat[1:])


def load_rules(raw: str) -> dict[str, Any]:
    """Las reglas, validadas fail-closed: sin roles inventados ni reglas exactas duplicadas."""
    data = json.loads(raw)
    roles = data.get("roles")
    rules = data.get("rules")
    if not isinstance(roles, dict) or not isinstance(rules, list):
        raise ValueError("loc_roles.json: se esperaban objetos 'roles' y 'rules'")
    techo = data.get("policy", {}).get("tooling_ratio_max")
    if not isinstance(techo, (int, float)) or isinstance(techo, bool) or techo <= 0:
        raise ValueError(f"policy.tooling_ratio_max inválido: {techo!r}")
    vistos: set[str] = set()
    for rule in rules:
        glob, role = rule.get("glob"), rule.get("role")
        if not isinstance(glob, str) or not glob:
            raise ValueError(f"regla sin glob: {rule!r}")
        if role not in roles:
            raise ValueError(f"{glob}: rol {role!r} no declarado (válidos: {sorted(roles)})")
        if "*" not in glob and glob in vistos:
            raise ValueError(f"regla exacta duplicada: {glob}")
        vistos.add(glob)
    return data


def classify(paths: list[str], rules: list[dict[str, Any]]) -> tuple[dict[str, str], list[str]]:
    """Cada ruta a su rol. Devuelve también los problemas, sin lanzar: el gate los imprime todos."""
    exactas = {r["glob"]: r["role"] for r in rules if "*" not in r["glob"]}
    globs = [r for r in rules if "*" in r["glob"]]
    problemas: list[str] = []
    for fantasma in sorted(set(exactas) - set(paths)):
        problemas.append(f"FANTASMA: la regla exacta {fantasma} no corresponde a ningún archivo gobernado")
    asignado: dict[str, str] = {}
    for path in paths:
        if path in exactas:
            asignado[path] = exactas[path]
            continue
        candidatos = sorted({r["role"] for r in globs if matches(path, r["glob"])})
        reclaman = sorted(r["glob"] for r in globs if matches(path, r["glob"]))
        if not candidatos:
            problemas.append(f"SIN CLASIFICAR: {path} — declara su rol en docs/loc_roles.json")
        elif len(reclaman) > 1:
            problemas.append(f"CLASIFICADO DOS VECES: {path} — lo reclaman {reclaman}")
        else:
            asignado[path] = candidatos[0]
    return asignado, problemas


def measure(root: Path, asignado: dict[str, str], roles: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Archivos y líneas de código por rol. Un archivo ilegible como texto cuenta como 0 líneas."""
    tabla = {rol: {"files": 0, "code_files": 0, "code_lines": 0} for rol in roles}
    for path, rol in sorted(asignado.items()):
        tabla[rol]["files"] += 1
        if Path(path).suffix not in CODE_SUFFIXES:
            continue
        tabla[rol]["code_files"] += 1
        tabla[rol]["code_lines"] += len((root / path).read_bytes().decode("utf-8", "replace").splitlines())
    return tabla


def report(root: Path = ROOT, rules_path: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """La medición y sus problemas. `root`/`rules_path` son inyectables para poder ejercitar los
    cuatro modos de fallo sobre un repositorio de juguete, sin tocar este."""
    data = load_rules((rules_path or RULES).read_text())
    paths = governed_files(root)
    problemas = [
        f"ENTORNO GOBERNADO: {p} — un entorno versionado invalida la medida"
        for p in paths
        if p.startswith(ENV_PREFIXES)
    ]
    asignado, mas = classify(paths, data["rules"])
    problemas += mas
    tabla = measure(root, asignado, data["roles"])
    producto = tabla["producto"]["code_lines"]
    herramientas = tabla["herramientas"]["code_lines"]
    techo = data["policy"]["tooling_ratio_max"]
    razon = herramientas / producto if producto else float("inf")
    if razon > techo:
        problemas.append(
            f"TOOLING DESBORDADO: herramientas {herramientas} vs producto {producto} = {razon:.3f} × (techo {techo})"
        )
    return {
        "por_rol": tabla,
        "ratio_herramientas_producto": round(razon, 4),
        "techo": techo,
        "archivos": len(paths),
    }, problemas


def main() -> int:
    medicion, problemas = report()
    if "--json" in sys.argv:
        print(json.dumps(medicion, indent=2, ensure_ascii=False))
    if problemas:
        print(f"✗ LOC POR ROL ROTO ({len(problemas)}):")
        for p in problemas:
            print(f"  - {p}")
        return 1
    if "--json" not in sys.argv:
        filas = ", ".join(f"{rol} {v['code_lines']}" for rol, v in medicion["por_rol"].items() if v["code_lines"])
        print(f"✓ {medicion['archivos']} archivos gobernados con rol único · líneas de código: {filas}")
        print(f"  herramientas/producto = {medicion['ratio_herramientas_producto']} × (techo {medicion['techo']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
