"""Offline extraction tests over saved HTML fixtures.

These exercise the *parsing* logic (parse_tables + extract_country_data) that
every H1-H5 fix touched, WITHOUT hitting the network — possible because
parse_tables is decoupled from fetching (it takes a soup). Each fixture is a
real bulletin from a distinct format era; each test pins the behavior of one
historical quirk so a future refactor can't silently regress it.

    ante/bin/python tests/test_extraction.py

⚠️ CONTRATO plain-script (O6): el gate del cron (freeze_and_rebuild.yml) ejecuta
este archivo como `python tests/<archivo>.py` SIN pytest — nada de fixtures,
parametrize ni markers aquí: un test que dependa de pytest correría en CI pero
se rompería o saltaría en el cron sin que nadie lo note.
"""

import sys
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"

from pipeline import scrape_dv_visa_bulletins as dv  # noqa: E402
from pipeline import scrape_family_visa_bulletins as fam  # noqa: E402
from pipeline import scrape_visa_bulletins as emp  # noqa: E402
from vp_data.visa_common import parse_tables  # noqa: E402

VALID_STATUS = {"C", "F", "U", "UNK"}


def _soup(name):
    return BeautifulSoup((FIX / name).read_text(encoding="utf-8"), "html.parser")


def _emp(name, ym, country="mexico"):
    tables = parse_tables(_soup(name), datetime(*ym), emp.is_employment_section)
    return emp.extract_country_data(country, [t.copy() for t in tables])


def _fam(name, ym, country="mexico"):
    tables = parse_tables(_soup(name), datetime(*ym), fam.is_family_section)
    return fam.extract_country_data(country, [t.copy() for t in tables])


def _dv(name, ym):
    # label_by_marker=False: igual que producción (J2) — las 2 tablas DV van por
    # ordinal; el heading FAD/DFF más cercano pertenece a la sección familiar.
    return dv.extract_dv_data(parse_tables(_soup(name), datetime(*ym), dv.is_dv_section, label_by_marker=False))


def _dv_blob(name, ym):
    return dv.extract_dv_blob(_soup(name), datetime(*ym))


DV_REGIONS = {"africa", "asia", "europe", "north_america", "oceania", "south_america_caribbean"}


# --- 2002-06: empty-name category column (col 0) + RoW recovery (H4) -----
def test_2002_empty_col0_format():
    d = _emp("vb_2002_06.html", (2002, 6, 1))
    cats = set(d.EB_level)
    assert {"EB1", "EB2", "EB3", "EB4"} <= cats, f"faltan EB básicas: {cats}"
    assert set(d.table_type) == {"final_action"}, "2002 no debería tener DFF"
    row = _emp("vb_2002_06.html", (2002, 6, 1), country="row")
    assert len(row) > 0, "RoW (All Chargeability) no parseó en formato 2002"


# --- 2007-06: concatenated header + EB4 translators + family 'familyall' --
def test_2007_concatenated_header():
    d = _emp("vb_2007_06.html", (2007, 6, 1))
    assert {"EB1", "EB2", "EB3", "EB4"} <= set(d.EB_level)
    assert "EB4_TRANS" in set(d.EB_level), "no capturó 'Iraqi & Afghani Translators'"
    f = _fam("vb_2007_06.html", (2007, 6, 1))
    assert set(f.F_level) == {"1", "2A", "2B", "3", "4"}, f"familia 2007: {set(f.F_level)}"


# --- 2020-06: employment FAD + DFF (H2) + regional-center mapping (H3) ----
def test_2020_employment_dff_and_regional_center():
    d = _emp("vb_2020_06.html", (2020, 6, 1))
    assert set(d.table_type) == {"final_action", "dates_for_filing"}, "falta DFF de empleo"
    assert {"EB5_RC", "EB5_NONRC"} <= set(d.EB_level), "no mapeó Regional/Non-Regional Center"
    f = _fam("vb_2020_06.html", (2020, 6, 1))
    assert "dates_for_filing" in set(f.table_type), "falta DFF familiar"


# --- 2022-06: post-reform EB-5 set-asides (H3) ---------------------------
def test_2022_eb5_setasides():
    d = _emp("vb_2022_06.html", (2022, 6, 1))
    setasides = {"EB5_RURAL", "EB5_HIGHUNEMP", "EB5_INFRA", "EB5_UNRESERVED"} & set(d.EB_level)
    assert len(setasides) >= 2, f"no capturó set-asides EB-5: {set(d.EB_level)}"


# --- subcategories present in a modern bulletin (H3) ---------------------
def test_subcategories_present():
    d = _emp("vb_2020_06.html", (2020, 6, 1))
    assert "EB3_OW" in set(d.EB_level), "falta Other Workers"
    assert "EB4_RW" in set(d.EB_level), "falta Certain Religious Workers"


# --- status annotation domain across all fixtures (H1/H5) ----------------
def test_status_domain_offline():
    for name, ym in [
        ("vb_2002_06.html", (2002, 6, 1)),
        ("vb_2007_06.html", (2007, 6, 1)),
        ("vb_2020_06.html", (2020, 6, 1)),
        ("vb_2022_06.html", (2022, 6, 1)),
    ]:
        d = _emp(name, ym)
        bad = set(d.status) - VALID_STATUS
        assert not bad, f"{name}: estados inválidos {bad}"
        # every 'F' row must carry a parsed date ('C' maps to the bulletin date
        # by design in the legacy column, so we don't assert on non-F rows here).
        miss = (d.status.eq("F") & d.priority_date.isna()).sum()
        assert miss == 0, f"{name}: {miss} filas F sin fecha"


# --- J2: a note-table can no longer shift the FAD/DFF labels --------------
def test_note_table_does_not_shift_fad_dff_labels():
    # Labels used to be pure ordinal (1st match = FAD, 2nd = DFF): a note-table
    # slipped in BEFORE the real ones mislabeled everything and the hard 2-table
    # break evicted the real DFF. With marker-based labeling the real tables
    # keep their type and the DFF survives.
    html = """
    <html><body>
    <table><tr><th>Employment-based note</th></tr><tr><td>prose, not data</td></tr></table>
    <p>A. FINAL ACTION DATES FOR EMPLOYMENT-BASED PREFERENCE CASES</p>
    <table><tr><th>Employment-based</th><th>All Chargeability Areas Except Those Listed</th></tr>
    <tr><td>1st</td><td>C</td></tr></table>
    <p>B. DATES FOR FILING OF EMPLOYMENT-BASED VISA APPLICATIONS</p>
    <table><tr><th>Employment-based</th><th>All Chargeability Areas Except Those Listed</th></tr>
    <tr><td>1st</td><td>01MAY16</td></tr></table>
    </body></html>
    """
    dfs = parse_tables(BeautifulSoup(html, "html.parser"), datetime(2026, 7, 1), emp.is_employment_section)
    types = [d["table_type"].iloc[0] for d in dfs if not d.empty]
    assert "dates_for_filing" in types, "la DFF real fue expulsada por la tabla-nota"
    real = [d for d in dfs if not d.empty and "1st" in d.iloc[:, 0].values]
    assert [d["table_type"].iloc[0] for d in real] == ["final_action", "dates_for_filing"]


# --- Diversity Visa: 6 regions, rank cut-offs (F) vs CURRENT (C) ----------
def test_dv_2007_ranks():
    d = _dv("vb_2007_06.html", (2007, 6, 1))
    assert set(d.region) == DV_REGIONS, f"regiones DV 2007: {set(d.region)}"
    assert set(d.status) == {"F"}, "DV jun-2007 son cortes específicos (F)"
    assert (d.rank_cutoff > 0).all(), "rangos DV 2007 deben ser positivos"


def test_dv_2020_current():
    d = _dv("vb_2020_06.html", (2020, 6, 1))
    assert set(d.region) == DV_REGIONS
    assert set(d.status) == {"C"}, "DV jun-2020 estaba CURRENT"
    assert d.rank_cutoff.isna().all(), "CURRENT no lleva rango"


def test_dv_2002_blob():
    # 2001-2004 single-cell format: "AFRICA:  AF 21,400 ASIA:  AS 9,500 …".
    d = _dv_blob("vb_2002_06.html", (2002, 6, 1))
    assert set(d.region) == DV_REGIONS, f"blob 2002 no recuperó las 6 regiones: {set(d.region)}"
    assert set(d.status) == {"F"} and (d.rank_cutoff > 0).all(), "rangos del blob 2002 deben ser F positivos"


def test_dv_status_domain_and_rank_iff_F():
    for name, ym in [
        ("vb_2007_06.html", (2007, 6, 1)),
        ("vb_2020_06.html", (2020, 6, 1)),
        ("vb_2022_06.html", (2022, 6, 1)),
    ]:
        d = _dv(name, ym)
        assert set(d.status) <= VALID_STATUS, f"{name}: estado DV inválido"
        mismatch = int((d.status.eq("F") != d.rank_cutoff.notna()).sum())
        assert mismatch == 0, f"{name}: {mismatch} filas DV con rank/F inconsistente"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{passed}/{passed + failed} pruebas de extracción OK" + (" ✓" if not failed else f"  ({failed} FALLAN)"))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
