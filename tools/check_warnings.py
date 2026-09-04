#!/usr/bin/env python3
"""D2-C: los warnings de la suite son un CONTRATO, no ruido.

La batería corre con ``error`` global (``tests/conftest.py``, lista ``FILTERWARNINGS``):
cualquier warning nuevo hace fallar el test que lo emite. Las únicas excepciones son las
del registro positivo ``security/warnings_registry.json``, todas upstream, estrechas
(prefijo de mensaje + categoría) y con fecha de caducidad.

Este verificador es **stdlib puro** y **fail-closed**: cualquier duda es fallo. Rechaza

- JSON con **claves duplicadas** (un segundo ``"warnings"`` silenciaría al primero);
- esquema o tipos incorrectos, campos faltantes o desconocidos;
- ``id`` duplicados y filtros duplicados;
- filtros **amplios**: ``ignore`` sin mensaje, ``ignore::Categoria``, ``ignore:`` con
  prefijo vacío o demasiado corto, o cualquier categoría comodín;
- registros **expirados** (``review`` en el pasado) o con fecha malformada;
- **falta de biyección** registro ⇄ ``FILTERWARNINGS`` (sobrantes en cualquiera de los dos
  lados, o pares que no casan exactamente);
- versiones que **no coinciden con el pin exacto** declarado en ``pin_source``
  (``pyproject.toml`` o un ``locks/*.txt``): una coincidencia parcial como ``1.9.0.post1`` o
  ``1.9.0+local`` NO cuenta, y un ``pin_source`` con ruta absoluta, ``..`` o fuera de ``locks/``
  se rechaza;
- un ``deferred_debt`` que no describa EXACTAMENTE las supresiones amplias vivas en los
  productores (se detectan leyendo el código, no se confía en la lista escrita a mano).

El ``message_prefix`` se trata como TEXTO LITERAL: el filtro se construye con ``re.escape``, así
que un prefijo con ``.*`` jamás se convierte en comodín.

Uso:  python tools/check_warnings.py   (sale 0 si el contrato se cumple, 1 si no)

Alcance honesto: gobierna la SUITE y su gate de CI. Los productores conservan supresiones
amplias en tiempo de ejecución (ver ``deferred_debt`` del registro); eso es deuda posterior.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "security" / "warnings_registry.json"
CONFTEST = ROOT / "tests" / "conftest.py"
PYPROJECT = ROOT / "pyproject.toml"

SCHEMA_VERSION = 2
REQUIRED_FIELDS = {
    "id",
    "package",
    "version",
    "pin_source",
    "category",
    "message_prefix",
    "origin",
    "reason",
    "issue",
    "review",
}
TOP_LEVEL_FIELDS = {"schema_version", "note", "scope", "deferred_debt", "warnings"}
DEBT_FIELDS = {"id", "count", "sites", "note"}
#: Capas de producto donde se inventarían las supresiones amplias vivas.
PRODUCER_LAYERS = ("vp_model", "vp_data", "pipeline", "experiments", "tools")
#: `simplefilter("ignore")` / `filterwarnings("ignore")` sin mensaje ni categoría: supresión amplia.
BROAD_SUPPRESSION = re.compile(r"(?:simplefilter|filterwarnings)\(\s*[\"']ignore[\"']\s*\)")
#: Un pin exacto termina aquí; `1.9.0.post1` o `1.9.0+local` NO son el mismo pin.
PIN_TERMINATOR = r"(?=[\s\"',;\\]|$)"
#: Un prefijo más corto que esto no identifica un warning concreto: se considera amplio.
MIN_PREFIX = 20
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PIN_RE = "{}=={}"


class ContractError(Exception):
    """Violación del contrato de warnings."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ContractError(f"clave JSON duplicada: {key!r}")
        seen.add(key)
    return dict(pairs)


def load_registry(path: Path = REGISTRY) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"registro ilegible: {exc}") from exc
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ContractError(f"registro no es JSON válido: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"la raíz del registro no es un objeto ({type(data).__name__})")
    if set(data) != TOP_LEVEL_FIELDS:
        missing = sorted(TOP_LEVEL_FIELDS - set(data))
        extra = sorted(set(data) - TOP_LEVEL_FIELDS)
        raise ContractError(f"claves de nivel superior: faltan {missing}, sobran {extra}")
    for field in ("note", "scope"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise ContractError(f"`{field}` debe ser una cadena no vacía")
    if not isinstance(data["deferred_debt"], dict):
        raise ContractError(f"`deferred_debt` debe ser un objeto ({type(data['deferred_debt']).__name__})")
    if data.get("schema_version") != SCHEMA_VERSION or isinstance(data.get("schema_version"), bool):
        raise ContractError(f"schema_version {data.get('schema_version')!r} != {SCHEMA_VERSION}")
    if not isinstance(data.get("warnings"), list) or not data["warnings"]:
        raise ContractError("`warnings` debe ser una lista no vacía")
    return data


def filter_expression(entry: dict[str, Any]) -> str:
    """Filtro de pytest EXACTO de un registro, con el mensaje ESCAPADO como texto literal.

    pytest interpreta el campo de mensaje como expresión regular; sin escapar, un prefijo con
    ``.``/``*``/``(`` silenciaría más de lo declarado. ``re.escape`` lo vuelve literal.
    """
    return f"ignore:{re.escape(entry['message_prefix'])}:{entry['category']}"


def detect_broad_suppressions(root: Path) -> list[str]:
    """Inventario `archivo:línea` de las supresiones amplias VIVAS en los productores."""
    found: list[str] = []
    myself = Path(__file__).resolve()
    for layer in PRODUCER_LAYERS:
        for path in sorted((root / layer).glob("*.py")):
            # Este módulo DEFINE el patrón; su propia línea no es una supresión.
            if path.resolve() == myself:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise ContractError(f"no se pudo inventariar {path}: {exc}") from exc
            for number, line in enumerate(lines, 1):
                if BROAD_SUPPRESSION.search(line):
                    found.append(f"{layer}/{path.name}:{number}")
    return found


def _check_deferred_debt(debt: dict[str, Any], root: Path) -> None:
    """La deuda declarada debe describir EXACTAMENTE lo que hay en el código."""
    if set(debt) != DEBT_FIELDS:
        raise ContractError(
            f"deferred_debt: faltan {sorted(DEBT_FIELDS - set(debt))}, sobran {sorted(set(debt) - DEBT_FIELDS)}"
        )
    for field in ("id", "note"):
        if not isinstance(debt[field], str) or not debt[field].strip():
            raise ContractError(f"deferred_debt.{field} debe ser una cadena no vacía")
    if isinstance(debt["count"], bool) or not isinstance(debt["count"], int):
        raise ContractError(f"deferred_debt.count debe ser un entero, no {type(debt['count']).__name__}")
    sites = debt["sites"]
    if not isinstance(sites, list) or not all(isinstance(s, str) and s.strip() for s in sites):
        raise ContractError("deferred_debt.sites debe ser una lista de cadenas no vacías")
    if len(set(sites)) != len(sites):
        raise ContractError(f"deferred_debt.sites con duplicados: {sorted({s for s in sites if sites.count(s) > 1})}")
    if debt["count"] != len(sites):
        raise ContractError(f"deferred_debt.count {debt['count']} != {len(sites)} sitios listados")
    live = detect_broad_suppressions(root)
    if sorted(sites) != sorted(live):
        raise ContractError(
            f"deferred_debt desalineada con el código — solo declaradas: {sorted(set(sites) - set(live))}; "
            f"solo en el código: {sorted(set(live) - set(sites))}"
        )


def _validate_entry(entry: Any, index: int) -> None:
    if not isinstance(entry, dict):
        raise ContractError(f"warnings[{index}] no es un objeto")
    missing = REQUIRED_FIELDS - set(entry)
    extra = set(entry) - REQUIRED_FIELDS
    if missing or extra:
        raise ContractError(f"warnings[{index}]: faltan {sorted(missing)}, sobran {sorted(extra)}")
    for field, value in entry.items():
        if not isinstance(value, str) or not value.strip():
            raise ContractError(f"warnings[{index}].{field} debe ser una cadena no vacía")
    if not ID_RE.match(entry["id"]):
        raise ContractError(f"warnings[{index}].id {entry['id']!r} no es kebab-case")
    prefix = entry["message_prefix"]
    if len(prefix) < MIN_PREFIX:
        raise ContractError(
            f"{entry['id']}: message_prefix demasiado corto ({len(prefix)} < {MIN_PREFIX}): filtro amplio"
        )
    if ":" in prefix:
        raise ContractError(f"{entry['id']}: message_prefix no puede contener ':' (rompe el filtro de pytest)")
    category = entry["category"]
    if category in {"Warning", "*"} or not category.endswith("Warning"):
        raise ContractError(f"{entry['id']}: categoría {category!r} demasiado amplia o inválida")
    if not DATE_RE.match(entry["review"]):
        raise ContractError(f"{entry['id']}: review {entry['review']!r} no es YYYY-MM-DD")


def _check_expiry(entries: list[dict[str, Any]], today: _dt.date) -> None:
    expired = []
    for entry in entries:
        try:
            review = _dt.date.fromisoformat(entry["review"])
        except ValueError as exc:
            raise ContractError(f"{entry['id']}: review inválida ({exc})") from exc
        if review < today:
            expired.append(f"{entry['id']} (venció {entry['review']})")
    if expired:
        raise ContractError(f"excepciones EXPIRADAS, hay que revisarlas o renovarlas: {expired}")


def _check_pins(entries: list[dict[str, Any]], root: Path) -> None:
    """La versión declarada debe ser un pin EXACTO en su `pin_source` (fail-closed)."""
    cache: dict[str, str] = {}
    for entry in entries:
        source = entry["pin_source"]
        _check_pin_source_path(entry["id"], source)
        if source not in cache:
            path = root / source
            if not path.is_file():
                raise ContractError(f"{entry['id']}: pin_source {source!r} no existe")
            cache[source] = path.read_text(encoding="utf-8")
        pin = PIN_RE.format(entry["package"], entry["version"])
        if not re.search(rf"(?mi)^\s*[\"']?{re.escape(pin)}{PIN_TERMINATOR}", cache[source]):
            raise ContractError(
                f"{entry['id']}: {pin} no es un pin exacto en {source} "
                "(una coincidencia parcial como .post1 o +local no cuenta)"
            )
        # Si además está pinneado en pyproject, ese pin manda y debe coincidir.
        if source != "pyproject.toml":
            other = (root / "pyproject.toml").read_text(encoding="utf-8")
            found = re.search(rf"(?mi)^\s*[\"']{re.escape(entry['package'])}==([^\"'\s,]+)", other)
            if found and found.group(1) != entry["version"]:
                raise ContractError(
                    f"{entry['id']}: pyproject pinnea {entry['package']}=={found.group(1)}, el registro dice {entry['version']}"
                )


def _check_pin_source_path(entry_id: str, source: str) -> None:
    """`pin_source` sólo puede ser `pyproject.toml` o `locks/<archivo>`, sin traversal."""
    if source == "pyproject.toml":
        return
    if Path(source).is_absolute() or source.startswith("/") or "\\" in source:
        raise ContractError(f"{entry_id}: pin_source {source!r} no puede ser una ruta absoluta")
    parts = source.split("/")
    if ".." in parts or len(parts) != 2 or parts[0] != "locks" or not parts[1]:
        raise ContractError(f"{entry_id}: pin_source {source!r} fuera de pyproject.toml y locks/ (o con traversal)")


def conftest_filters(path: Path = CONFTEST) -> list[str]:
    """Los filtros declarados en ``FILTERWARNINGS`` del conftest (parseo literal, sin importar)."""
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise ContractError(f"conftest ilegible: {exc}") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "FILTERWARNINGS" for t in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError as exc:
                raise ContractError(f"FILTERWARNINGS no es una lista literal ({exc})") from exc
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ContractError("FILTERWARNINGS debe ser una lista de cadenas")
            return value
    raise ContractError("no se encontró FILTERWARNINGS en tests/conftest.py")


def _check_bijection(entries: list[dict[str, Any]], filters: list[str]) -> None:
    if not filters or filters[0] != "error":
        raise ContractError("FILTERWARNINGS debe empezar por 'error' (contrato global)")
    body = filters[1:]
    if len(set(body)) != len(body):
        raise ContractError(f"filtros duplicados en FILTERWARNINGS: {sorted({f for f in body if body.count(f) > 1})}")
    for filt in body:
        if not filt.startswith("ignore:") or filt.count(":") < 2:
            raise ContractError(f"filtro amplio o malformado en FILTERWARNINGS: {filt!r}")
        if filt.startswith("ignore::") or filt == "ignore":
            raise ContractError(f"supresión global prohibida: {filt!r}")
    expected = {filter_expression(e) for e in entries}
    got = set(body)
    if expected != got:
        raise ContractError(
            f"biyección rota — solo en el registro: {sorted(expected - got)}; solo en FILTERWARNINGS: {sorted(got - expected)}"
        )


def verify(root: Path = ROOT, today: _dt.date | None = None) -> list[dict[str, Any]]:
    data = load_registry(root / "security" / "warnings_registry.json")
    entries = data["warnings"]
    for i, entry in enumerate(entries):
        _validate_entry(entry, i)
    ids = [e["id"] for e in entries]
    if len(set(ids)) != len(ids):
        raise ContractError(f"ids duplicados: {sorted({i for i in ids if ids.count(i) > 1})}")
    exprs = [filter_expression(e) for e in entries]
    if len(set(exprs)) != len(exprs):
        raise ContractError("dos registros producen el MISMO filtro (duplicado efectivo)")
    _check_deferred_debt(data["deferred_debt"], root)
    _check_expiry(entries, today or _dt.date.today())
    _check_pins(entries, root)
    _check_bijection(entries, conftest_filters(root / "tests" / "conftest.py"))
    return entries


def main() -> int:
    try:
        entries = verify()
    except ContractError as exc:
        print(f"✗ contrato de warnings ROTO: {exc}", file=sys.stderr)
        return 1
    print(f"✓ contrato de warnings OK — 'error' global y {len(entries)} excepción(es) upstream estrechas y vigentes")
    for entry in entries:
        print(
            f"   {entry['id']}: {entry['package']}=={entry['version']} ({entry['pin_source']}) hasta {entry['review']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
