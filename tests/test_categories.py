"""C1: una sola taxonomía migratoria, y equivalente a la que ya corría.

`vp_data/categories.py` concentra lo que estaba repartido entre dos scrapers y
`pipeline/build_database.py`: las reglas de clasificación (EB y familia), los dominios
cerrados y la metadatos por categoría. Esta suite fija tres cosas:

1. **Equivalencia exhaustiva**: para todo el universo de etiquetas que la fuente ha
   publicado en 24 años —y para las variantes ambiguas que el orden de reglas resuelve—
   la función nueva devuelve exactamente lo que devolvían los clasificadores anteriores.
2. **Precedencia**: las reglas que se contienen entre sí (`targeted employment` incluye
   `regional center`; `non-regional center` incluye `regional center`) se prueban por
   separado, porque un reordenamiento silencioso cambiaría 20 años de historia.
3. **Autoridad única**: nadie más puede declarar el dominio ni la metadatos.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from vp_data import categories as cat

ROOT = Path(__file__).resolve().parents[1]

# El universo de etiquetas EB que la fuente publicó, con el código que el clasificador
# ANTERIOR devolvía. Es la tabla de equivalencia: si algo aquí cambia, cambia la historia.
EB_CASES = [
    ("1st", "EB1"),
    ("2nd", "EB2"),
    ("3rd", "EB3"),
    ("4th", "EB4"),
    ("5th", "EB5"),
    ("  1ST  ", "EB1"),
    ("4th*", "EB4"),
    ("4th†", "EB4"),
    ("Other Workers", "EB3_OW"),
    ("other workers (owo)", "EB3_OW"),
    ("Certain Religious Workers", "EB4_RW"),
    ("Certain Religiuos Workers", "EB4_RW"),
    ("Certain Iraqi/Afghan Translators", "EB4_TRANS"),
    ("5th Set Aside: Rural (20%)", "EB5_RURAL"),
    ("5th Set-Aside: High Unemployment (10%)", "EB5_HIGHUNEMP"),
    ("5th Set Aside: Infrastructure (2%)", "EB5_INFRA"),
    ("5th Set Aside: Unspecified", "EB5_UNRESERVED"),
    ("5th Unreserved (including C5, T5, I5, R5)", "EB5_UNRESERVED"),
    ("5th Targeted Employment Areas/Regional Centers", "EB5_TEA"),
    ("5th Pilot Programs", "EB5_PILOT"),
    ("5th Pilot Progams", "EB5_PILOT"),
    ("5th Non-Regional Center", "EB5_NONRC"),
    ("5th Regional Center", "EB5_RC"),
    ("Schedule A Workers", None),
    ("", None),
    ("   ", None),
    (None, None),
    ("Employment-based", None),
    # Verificado contra el clasificador anterior: una etiqueta que MENCIONA "other workers"
    # pero no EMPIEZA por ahí devuelve None. Yo había supuesto EB3_OW; la historia dice que no.
    ("*Employment Third Preference Other Workers", None),
    ("5th Unreserved Set Aside", "EB5_UNRESERVED"),
]

FAMILY_CASES = [
    ("1st", "1"),
    ("F1", "1"),
    ("f1", "1"),
    ("1st*", "1"),
    ("2A", "2A"),
    ("2nd-A", "2A"),
    ("2ndA", "2A"),
    ("F2A", "2A"),
    ("2A*", "2A"),
    ("2B", "2B"),
    ("2nd-B", "2B"),
    ("2ndB", "2B"),
    ("F2B", "2B"),
    ("2B*", "2B"),
    ("3rd", "3"),
    ("F3", "3"),
    ("3rd†", "3"),
    ("4th", "4"),
    ("4rd", "4"),
    ("F4", "4"),
    ("4th*", "4"),
    ("Family- Sponsored", None),
    ("family", None),
    ("", None),
    (None, None),
    ("5th", None),
]


# --- 1. equivalencia con los clasificadores anteriores -------------------------------


@pytest.mark.parametrize(("raw", "expected"), EB_CASES)
def test_eb_classification_matches_history(raw, expected) -> None:
    assert cat.classify_eb(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), FAMILY_CASES)
def test_family_classification_matches_history(raw, expected) -> None:
    assert cat.classify_family(raw) == expected


def test_scraper_wrappers_delegate_to_the_single_authority() -> None:
    """Los scrapers conservan su nombre público, pero ya no llevan reglas propias."""
    from pipeline.scrape_family_visa_bulletins import classify_family_category
    from pipeline.scrape_visa_bulletins import classify_eb_category

    for raw, expected in EB_CASES:
        assert classify_eb_category(raw) == expected == cat.classify_eb(raw)
    for raw, expected in FAMILY_CASES:
        assert classify_family_category(raw) == expected == cat.classify_family(raw)


def test_no_rule_logic_survives_in_the_scrapers() -> None:
    """Si una regla vuelve a un scraper, hay dos autoridades otra vez."""
    for mod, needle in (("scrape_visa_bulletins.py", "regional center"), ("scrape_family_visa_bulletins.py", "2nd-a")):
        src = (ROOT / "pipeline" / mod).read_text(encoding="utf-8").lower()
        assert needle not in src, f"{mod} sigue clasificando por su cuenta"


# --- 2. precedencia de las reglas que se contienen -----------------------------------


@pytest.mark.parametrize(
    ("raw", "expected", "porque"),
    [
        (
            "5th Targeted Employment Areas/Regional Centers",
            "EB5_TEA",
            "'targeted employment' CONTIENE 'regional center' y debe ganar",
        ),
        ("5th Non-Regional Center", "EB5_NONRC", "'non-regional center' CONTIENE 'regional center' y debe ganar"),
        ("5th Set Aside: Rural (20%)", "EB5_RURAL", "los set-asides post-2022 se prueban antes que el 5th genérico"),
        ("5th Unreserved Set Aside", "EB5_UNRESERVED", "un set-aside sin sabor cae al reservado genérico, no a 5th"),
    ],
)
def test_ambiguous_labels_resolve_by_declared_order(raw, expected, porque) -> None:
    assert cat.classify_eb(raw) == expected, porque


def test_rule_order_is_declared_not_incidental() -> None:
    """El orden vive en la tabla, y la tabla es lo que la función recorre."""
    assert isinstance(cat.EB_RULES, tuple) and isinstance(cat.FAMILY_RULES, tuple)
    codes = [r.code for r in cat.EB_RULES]
    assert codes.index("EB5_TEA") < codes.index("EB5_RC")
    assert codes.index("EB5_NONRC") < codes.index("EB5_RC")
    assert codes.index("EB5_RURAL") < codes.index("EB5")


def test_footnotes_and_source_typos_stay_supported() -> None:
    assert cat.classify_eb("Certain Religiuos Workers") == "EB4_RW"  # typo 2004-05
    assert cat.classify_eb("5th Pilot Progams") == "EB5_PILOT"  # typo 2009-04
    assert cat.classify_family("4rd") == "4"  # typo 2003-03
    for marker in ("*", "†", " "):
        assert cat.classify_family(f"2A{marker}") == "2A"
        assert cat.classify_eb(f"4th{marker}") == "EB4"


# --- 3. dominios y metadatos: autoridad única ---------------------------------------


def test_domains_are_closed_and_derived_from_the_rules() -> None:
    assert cat.EB_CODES == frozenset(r.code for r in cat.EB_RULES)
    assert cat.FAMILY_CODES == frozenset(r.code for r in cat.FAMILY_RULES)
    assert len(cat.EB_CODES) == 16 and cat.FAMILY_CODES == frozenset({"1", "2A", "2B", "3", "4"})
    for produced in (cat.classify_eb(r) for r, _ in EB_CASES):
        assert produced is None or produced in cat.EB_CODES
    for produced in (cat.classify_family(r) for r, _ in FAMILY_CASES):
        assert produced is None or produced in cat.FAMILY_CODES


def test_metadata_covers_exactly_the_domain() -> None:
    assert set(cat.CATEGORY_META) == set(cat.EB_CODES) | set(cat.FAMILY_CATEGORY_CODES)
    for code, meta in cat.CATEGORY_META.items():
        parent, level, is_sub, ina = meta
        assert isinstance(level, int) and 1 <= level <= 5
        assert isinstance(is_sub, bool)
        assert (parent is None) != is_sub or code in ("F2A", "F2B")
        assert ina and ina.startswith("INA 203(")
        if parent is not None:
            assert parent in cat.CATEGORY_META or parent == "F2", code


def test_database_imports_the_metadata_instead_of_redeclaring_it() -> None:
    src = (ROOT / "pipeline" / "build_database.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    assigns = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name) and t.id == "CATEGORY_META"
    ]
    assert not assigns, "build_database vuelve a declarar CATEGORY_META"
    assert "from vp_data.categories import" in src
    from pipeline import build_database as bd

    assert bd.CATEGORY_META is cat.CATEGORY_META


def test_country_list_has_a_single_authority() -> None:
    """`SCRAPER_COUNTRIES` deja de ser una lista paralela: se deriva del canónico,
    conservando el ORDEN REAL en que se escriben los CSV por país."""
    from vp_data.config import CANONICAL_COUNTRY
    from vp_data.visa_common import SCRAPER_COUNTRIES

    assert list(SCRAPER_COUNTRIES) == ["india", "china", "mexico", "philippines", "row"]
    assert set(SCRAPER_COUNTRIES) == set(CANONICAL_COUNTRY)
    src = (ROOT / "vp_data" / "visa_common.py").read_text(encoding="utf-8")
    assert '"philippines"' not in src.split("SCRAPER_COUNTRIES")[1][:200], (
        "SCRAPER_COUNTRIES sigue siendo una lista literal"
    )


def test_panel_validates_by_membership_not_by_prefix() -> None:
    src = (ROOT / "pipeline" / "build_panel.py").read_text(encoding="utf-8")
    # Se mira el CRITERIO, no la cadena: el comentario que explica por qué murió el prefijo
    # sí puede nombrarlo. Lo que no puede sobrevivir es el `str.match` que lo aplicaba.
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "str.match" not in code, "el panel seguía aceptando categorías por prefijo"
    assert '- {"1", "2A", "2B", "3", "4"}' not in code, "el dominio familiar seguía escrito a mano"
    assert "set(EB_CODES)" in code and "set(FAMILY_CODES)" in code
    assert "fuera de dominio" in code, "el mensaje fail-closed se conserva"


def test_a_prefixed_impostor_is_rejected() -> None:
    """`EB1_INVENTADO` pasaba el regex de prefijo; contra el dominio cerrado, no."""
    assert "EB1_INVENTADO" not in cat.EB_CODES
    assert re.match(r"^EB[1-5]", "EB1_INVENTADO")  # el criterio viejo lo habría aceptado


def test_module_stays_dependency_light() -> None:
    src = (ROOT / "vp_data" / "categories.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not (imported & {"pandas", "numpy", "duckdb", "pyarrow"}), imported
