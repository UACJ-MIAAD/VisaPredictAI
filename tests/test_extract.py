"""C2: un solo extractor por país, parametrizado, y equivalente al que ya corría.

Los dos `extract_country_data` eran el mismo algoritmo escrito dos veces: la comparación
literal solo encuentra tres diferencias —el nombre de la columna de nivel, el clasificador y
los comentarios—. Todo lo demás (el caso especial de `row`, la selección por etiqueta
normalizada, la omisión de tablas defectuosas, el esquema vacío, la anotación de fechas, la
etiqueta cruda preservada, el descarte de filas ajenas, la deduplicación y el orden) era
idéntico, así que estaba duplicado por accidente, no por necesidad.

Esta suite prueba cada función pura por separado con datos sintéticos y vigila que ninguna
implementación paralela reaparezca.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pandas as pd
import pytest

from vp_data import extract
from vp_data.categories import classify_eb, classify_family

ROOT = Path(__file__).resolve().parents[1]
EMPTY_COLS = [
    "priority_date",
    "visa_bulletin_date",
    "table_type",
    "raw_value",
    "status",
    "visa_wait_time",
    "raw_category",
]


def _table(
    cat_header: str,
    country_header: str,
    rows: list[tuple[str, str]],
    date: str = "2024-01-01",
    table_type: str = "final_action",
) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=[cat_header, country_header])
    df["visa_bulletin_date"] = pd.Timestamp(date)
    df["table_type"] = table_type
    return df


# --- select_country_columns ---------------------------------------------------------


def test_selects_by_normalized_label_across_formats() -> None:
    for header in ("Mexico", "  MEXICO ", "Me\xa0xico".replace("\xa0", ""), "mexico\n"):
        df = _table("Employment-based", header, [("1st", "01JAN20")])
        assert extract.select_country_columns(df, "mexico") == (df.columns[0], header)


def test_row_matches_the_all_chargeability_column() -> None:
    df = _table("Employment-based", "All Chargeability Areas Except Those Listed", [("1st", "C")])
    cols = extract.select_country_columns(df, extract.search_term("row"))
    assert cols is not None and cols[1] == df.columns[1]
    assert extract.search_term("row") == "except those listed"
    assert extract.search_term("mexico") == "mexico"


def test_returns_none_when_the_country_is_absent_or_is_the_category_column() -> None:
    df = _table("Employment-based", "India", [("1st", "C")])
    assert extract.select_country_columns(df, "mexico") is None
    same = pd.DataFrame([["1st", "C"]], columns=["mexico", "otra"])
    same["visa_bulletin_date"] = pd.Timestamp("2024-01-01")
    same["table_type"] = "final_action"
    assert extract.select_country_columns(same, "mexico") is None  # sería la propia col 0


# --- empty_country_frame ------------------------------------------------------------


@pytest.mark.parametrize("level_col", ["EB_level", "F_level"])
def test_empty_frame_carries_the_schema_the_panel_expects(level_col: str) -> None:
    df = extract.empty_country_frame(level_col)
    assert list(df.columns) == [level_col, *EMPTY_COLS]
    assert df.empty


# --- preserve_raw_label -------------------------------------------------------------


def test_raw_label_is_preserved_but_line_trailing_space_is_dropped() -> None:
    s = pd.Series(["  Other Workers  ", "Certain\nReligious", "linea1   \nlinea2"])
    out = extract.preserve_raw_label(s)
    assert list(out) == ["Other Workers", "Certain\nReligious", "linea1\nlinea2"]


# --- classify_rows ------------------------------------------------------------------


def test_classification_replaces_the_label_and_drops_foreign_rows() -> None:
    df = pd.DataFrame({"EB_level": ["1st", "Schedule A Workers", "Other Workers"], "x": [1, 2, 3]})
    out = extract.classify_rows(df, "EB_level", classify_eb)
    assert list(out["EB_level"]) == ["EB1", "EB3_OW"]
    assert list(out["x"]) == [1, 3]


def test_family_classification_uses_the_same_machinery() -> None:
    df = pd.DataFrame({"F_level": ["F1", "family", "2A"], "x": [1, 2, 3]})
    out = extract.classify_rows(df, "F_level", classify_family)
    assert list(out["F_level"]) == ["1", "2A"]


# --- dedupe -------------------------------------------------------------------------


def test_dedupe_keeps_the_first_row_of_a_repeated_key() -> None:
    df = pd.DataFrame(
        {
            "EB_level": ["EB5", "EB5", "EB5"],
            "visa_bulletin_date": ["2022-05-01", "2022-05-01", "2022-06-01"],
            "table_type": ["final_action", "final_action", "final_action"],
            "priority_date": ["A", "B", "C"],
        }
    )
    out = extract.dedupe(df, "EB_level")
    assert list(out["priority_date"]) == ["A", "C"]


# --- extract_country_data (integración de las piezas) -------------------------------


def _snapshot_tables() -> list[pd.DataFrame]:
    return [
        _table("Employment-based", "Mexico", [("1st", "01JAN20"), ("Other Workers", "C"), ("Schedule A Workers", "U")]),
        _table("Employment-based", "India", [("1st", "15MAR19")]),  # otro país
        _table("Employment-based", "Mexico", [("2nd", "01FEB21")], date="2024-02-01"),
    ]


def test_extract_is_parameterized_and_keeps_order() -> None:
    out = extract.extract_country_data("mexico", _snapshot_tables(), level_col="EB_level", classifier=classify_eb)
    assert list(out["EB_level"]) == ["EB1", "EB3_OW", "EB2"]
    assert list(out["raw_category"]) == ["1st", "Other Workers", "2nd"]
    assert set(EMPTY_COLS) <= set(out.columns)


def test_extract_returns_the_empty_schema_when_the_country_is_missing() -> None:
    out = extract.extract_country_data("brasil", _snapshot_tables(), level_col="F_level", classifier=classify_family)
    assert list(out.columns) == ["F_level", *EMPTY_COLS] and out.empty


def test_a_table_missing_a_required_column_is_skipped_not_fatal() -> None:
    good = _table("Employment-based", "Mexico", [("1st", "01JAN20")])
    incompleta = pd.DataFrame([["1st", "C"]], columns=["Employment-based", "Mexico"])
    incompleta["visa_bulletin_date"] = pd.Timestamp("2024-01-01")  # sin table_type -> KeyError
    out = extract.extract_country_data("mexico", [incompleta, good], level_col="EB_level", classifier=classify_eb)
    assert list(out["EB_level"]) == ["EB1"]


def test_a_duplicated_header_still_raises_exactly_as_before() -> None:
    """Comportamiento PRESERVADO, no aprobado: el comentario del código dice que una cabecera
    duplicada se omite, pero el rename que lo detectaría vive FUERA del `try`, así que revienta.
    Verificado contra el baseline: falla igual, con el mismo `ValueError`. C2 preserva conducta;
    corregirlo sería cambiarla, y eso no cabe en esta historia."""
    dup = pd.DataFrame([["1st", "C", "C"]], columns=["Employment-based", "Mexico", "Mexico"])
    dup["visa_bulletin_date"] = pd.Timestamp("2024-01-01")
    dup["table_type"] = "final_action"
    with pytest.raises(ValueError, match="Length mismatch"):
        extract.extract_country_data("mexico", [dup], level_col="EB_level", classifier=classify_eb)


def test_rows_without_a_bulletin_date_are_dropped() -> None:
    df = _table("Employment-based", "Mexico", [("1st", "01JAN20"), ("2nd", "01FEB20")])
    df.loc[1, "visa_bulletin_date"] = pd.NaT
    out = extract.extract_country_data("mexico", [df], level_col="EB_level", classifier=classify_eb)
    assert list(out["EB_level"]) == ["EB1"]


# --- guardianes estructurales -------------------------------------------------------


def test_both_scrapers_delegate_to_the_single_extractor() -> None:
    from pipeline.scrape_family_visa_bulletins import extract_country_data as fam
    from pipeline.scrape_visa_bulletins import extract_country_data as eb

    tables = _snapshot_tables()
    pd.testing.assert_frame_equal(
        eb("mexico", tables),
        extract.extract_country_data("mexico", tables, level_col="EB_level", classifier=classify_eb),
    )
    fam_tables = [_table("Family- Sponsored", "Mexico", [("F1", "01JAN20"), ("2A", "C")])]
    pd.testing.assert_frame_equal(
        fam("mexico", fam_tables),
        extract.extract_country_data("mexico", fam_tables, level_col="F_level", classifier=classify_family),
    )


def test_no_parallel_extraction_survives_in_the_scrapers() -> None:
    """El algoritmo vive en un sitio: si vuelve a un scraper, hay dos verdades otra vez."""
    for mod in ("scrape_visa_bulletins.py", "scrape_family_visa_bulletins.py"):
        src = (ROOT / "pipeline" / mod).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "extract_country_data")
        assert len(fn.body) <= 3, f"{mod}: el wrapper volvió a tener cuerpo propio"
        for needle in ("drop_duplicates", "pd.concat", "annotate_dates", "except those listed"):
            assert needle not in src, f"{mod} sigue extrayendo por su cuenta ({needle})"


def test_the_two_wrappers_only_differ_in_their_two_parameters() -> None:
    from pipeline.scrape_family_visa_bulletins import extract_country_data as fam
    from pipeline.scrape_visa_bulletins import extract_country_data as eb

    bodies = []
    for fn in (eb, fam):
        src = inspect.getsource(fn)
        bodies.append(
            src.replace("EB_level", "LEVEL")
            .replace("F_level", "LEVEL")
            .replace("classify_eb", "CLS")
            .replace("classify_family", "CLS")
        )
    stripped = ["\n".join(ln for ln in b.splitlines() if not ln.strip().startswith(("#", '"""'))) for b in bodies]
    assert stripped[0].split("return")[-1] == stripped[1].split("return")[-1]


def test_network_and_io_stayed_in_the_scrapers() -> None:
    """C2 no migra `parse_tables`, `write_csvs`, la detección de secciones ni la red."""
    src = (ROOT / "vp_data" / "extract.py").read_text(encoding="utf-8")
    for needle in ("requests", "BeautifulSoup", "to_csv", "read_html", "parse_tables", "write_csvs"):
        assert needle not in src, f"vp_data/extract.py absorbió algo que no le toca: {needle}"
    for mod in ("scrape_visa_bulletins.py", "scrape_family_visa_bulletins.py"):
        s = (ROOT / "pipeline" / mod).read_text(encoding="utf-8")
        assert "def write_csvs" in s, f"{mod} perdió su escritura de CSV"
        assert "_section(rows)" in s, f"{mod} perdió su detección de sección"
    # `parse_tables` NO estaba en los scrapers: ya vivía en `vp_data.visa_common`, y ahí sigue.
    assert "def parse_tables" in (ROOT / "vp_data" / "visa_common.py").read_text(encoding="utf-8")


def test_extract_is_declared_as_a_dvc_dependency() -> None:
    import yaml

    dag = yaml.safe_load((ROOT / "dvc.yaml").read_text(encoding="utf-8"))
    for stage in ("scrape", "panel", "database"):
        deps = dag["stages"][stage]["deps"]
        consumes = any("scrape_" in d or d.endswith("build_panel.py") or d.endswith("build_database.py") for d in deps)
        if consumes and stage == "scrape":
            assert "vp_data/extract.py" in deps, stage
