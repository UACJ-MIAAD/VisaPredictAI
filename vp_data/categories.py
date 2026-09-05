"""C1 — taxonomía migratoria del proyecto: una sola autoridad.

Antes, la misma taxonomía vivía en tres sitios: las reglas de clasificación en cada uno de
los dos scrapers y la metadatos por categoría dentro de `pipeline/build_database.py`. Tres
copias que nadie obligaba a coincidir. Aquí quedan juntas y declarativas:

* `EB_RULES` / `FAMILY_RULES` — las reglas, **en orden**, tal como se aplicaban;
* `classify_eb` / `classify_family` — funciones puras que recorren esas tablas;
* `EB_CODES` / `FAMILY_CODES` — los dominios cerrados, derivados de las reglas;
* `CATEGORY_META` — la metadatos por código de categoría del panel.

**El orden de las reglas es semántico, no cosmético.** Varias etiquetas se contienen entre
sí y el resultado depende de cuál se prueba primero: `targeted employment` contiene
`regional center`, `non-regional center` contiene `regional center`, y los set-asides
posteriores a 2022 deben ganarle al `5th` genérico. Reordenar la tabla reescribiría 20 años
de historia sin que nada más se queje; por eso `tests/test_categories.py` fija el orden y
la equivalencia caso por caso contra los clasificadores anteriores.

Stdlib puro: lo importan los scrapers, el panel y la base de datos, y ninguno debe arrastrar
pandas por leer una taxonomía.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

# Marcadores de nota al pie de la fuente. Se replica aquí (en vez de importarlo de
# `visa_common`) para que este módulo no dependa de nada: `visa_common` sí lo importa
# de vuelta, y `tests/test_categories.py` comprueba que ambos valores coinciden.
FOOTNOTE_CHARS = "*† "


class Rule(NamedTuple):
    """Una regla de clasificación: si `match` acepta la etiqueta normalizada, es `code`."""

    code: str
    match: Callable[[str], bool]
    why: str


def _eq(*forms: str) -> Callable[[str], bool]:
    return lambda s: s in forms


def _has(*needles: str) -> Callable[[str], bool]:
    return lambda s: any(n in s for n in needles)


def _starts(prefix: str) -> Callable[[str], bool]:
    return lambda s: s.startswith(prefix)


def _set_aside(*needles: str) -> Callable[[str], bool]:
    """Un set-aside de la RIA-2022 con su sabor: la etiqueta lleva ambas señales."""
    return lambda s: ("set aside" in s or "set-aside" in s) and any(n in s for n in needles)


def _bare_set_aside(s: str) -> bool:
    return "set aside" in s or "set-aside" in s


# ORDEN SEMÁNTICO — no reordenar sin una prueba que lo justifique (ver docstring).
EB_RULES: tuple[Rule, ...] = (
    Rule("EB1", _eq("1st"), "preferencia numerada"),
    Rule("EB2", _eq("2nd"), "preferencia numerada"),
    Rule("EB3", _eq("3rd"), "preferencia numerada"),
    Rule("EB4", _eq("4th"), "preferencia numerada"),
    Rule("EB3_OW", _starts("other worker"), "subcategoría de EB-3"),
    Rule("EB4_RW", _has("religious", "religiuos"), "religiosos; 'religiuos' es typo de la fuente (2004-05)"),
    Rule("EB4_TRANS", _has("translator"), "traductores iraquíes/afganos"),
    # Los set-asides de la RIA-2022 van ANTES que cualquier 'unreserved' o '5th' genérico.
    Rule("EB5_RURAL", _set_aside("rural"), "set-aside rural (20%)"),
    Rule("EB5_HIGHUNEMP", _set_aside("high unemployment"), "set-aside de alto desempleo (10%)"),
    Rule("EB5_INFRA", _set_aside("infrastructure"), "set-aside de infraestructura (2%)"),
    Rule("EB5_UNRESERVED", _bare_set_aside, "set-aside sin sabor reconocido: reservado genérico"),
    Rule("EB5_UNRESERVED", _has("unreserved"), "EB-5 no reservado"),
    # 'targeted employment' CONTIENE 'regional center': debe probarse antes.
    Rule("EB5_TEA", _has("targeted employment"), "áreas de empleo focalizado (pre-2015)"),
    Rule("EB5_PILOT", _has("pilot prog"), "programas piloto; 'prog' tolera el typo 'progams' (2009-04)"),
    # 'non-regional center' CONTIENE 'regional center': debe probarse antes.
    Rule("EB5_NONRC", _has("non-regional center"), "split 2015-2022, lado no-centro regional"),
    Rule("EB5_RC", _has("regional center"), "split 2015-2022, centro regional"),
    Rule("EB5", _eq("5th"), "quinta preferencia desnuda (2003-2011)"),
)

FAMILY_RULES: tuple[Rule, ...] = (
    Rule("1", _eq("1st", "f1"), "primera preferencia familiar"),
    Rule("2A", _eq("2a", "2nd-a", "2nda", "f2a"), "cónyuges e hijos menores de residentes"),
    Rule("2B", _eq("2b", "2nd-b", "2ndb", "f2b"), "hijos solteros mayores de residentes"),
    Rule("3", _eq("3rd", "f3"), "hijos casados de ciudadanos"),
    Rule("4", _eq("4th", "4rd", "f4"), "hermanos; '4rd' es typo de la fuente (2003-03)"),
)

EB_CODES: frozenset[str] = frozenset(r.code for r in EB_RULES)
FAMILY_CODES: frozenset[str] = frozenset(r.code for r in FAMILY_RULES)
# El panel prefija la familia con 'F'; la metadatos se indexa por ese código.
FAMILY_CATEGORY_CODES: frozenset[str] = frozenset(f"F{c}" for c in FAMILY_CODES)


def _normalize(raw: object) -> str:
    """Minúsculas, espacios colapsados y sin notas al pie. Idéntico al comportamiento
    anterior: `norm_label` seguido de `rstrip(FOOTNOTE_CHARS)`."""
    if raw is None:
        return ""
    s = " ".join(str(raw).split()).lower()
    return s.rstrip(FOOTNOTE_CHARS)


def _classify(raw: object, rules: tuple[Rule, ...]) -> str | None:
    s = _normalize(raw)
    if not s:
        return None
    for rule in rules:
        if rule.match(s):
            return rule.code
    return None


def classify_eb(raw: object) -> str | None:
    """Código canónico EB para una etiqueta cruda, o `None` si la fila no es una
    preferencia EB-1..EB-5 (Schedule A, encabezados, notas al pie sueltas)."""
    return _classify(raw, EB_RULES)


def classify_family(raw: object) -> str | None:
    """Nivel canónico de preferencia familiar (`1`, `2A`, `2B`, `3`, `4`), o `None`."""
    return _classify(raw, FAMILY_RULES)


# (parent_code, preference_level, is_subcategory, ina_basis)
CATEGORY_META: dict[str, tuple[str | None, int, bool, str]] = {
    "F1": (None, 1, False, "INA 203(a)(1)"),
    "F2A": ("F2", 2, True, "INA 203(a)(2)(A)"),
    "F2B": ("F2", 2, True, "INA 203(a)(2)(B)"),
    "F3": (None, 3, False, "INA 203(a)(3)"),
    "F4": (None, 4, False, "INA 203(a)(4)"),
    "EB1": (None, 1, False, "INA 203(b)(1)"),
    "EB2": (None, 2, False, "INA 203(b)(2)"),
    "EB3": (None, 3, False, "INA 203(b)(3)"),
    "EB3_OW": ("EB3", 3, True, "INA 203(b)(3)"),  # Other Workers
    "EB4": (None, 4, False, "INA 203(b)(4)"),
    "EB4_RW": ("EB4", 4, True, "INA 203(b)(4)"),  # Certain Religious Workers
    "EB4_TRANS": ("EB4", 4, True, "INA 203(b)(4)"),  # Iraqi/Afghan Translators
    "EB5": (None, 5, False, "INA 203(b)(5)"),
    "EB5_TEA": ("EB5", 5, True, "INA 203(b)(5)"),  # Targeted Employment Area
    "EB5_PILOT": ("EB5", 5, True, "INA 203(b)(5)"),  # Regional Center Pilot
    "EB5_RC": ("EB5", 5, True, "INA 203(b)(5)"),  # Regional Center
    "EB5_NONRC": ("EB5", 5, True, "INA 203(b)(5)"),  # Non-Regional Center
    "EB5_UNRESERVED": ("EB5", 5, True, "INA 203(b)(5)"),
    "EB5_RURAL": ("EB5", 5, True, "INA 203(b)(5)(B)(ii)"),  # RIA-2022 set-asides
    "EB5_HIGHUNEMP": ("EB5", 5, True, "INA 203(b)(5)(B)(ii)"),
    "EB5_INFRA": ("EB5", 5, True, "INA 203(b)(5)(B)(ii)"),
}
