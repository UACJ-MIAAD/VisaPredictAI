"""Offline extraction tests over saved HTML fixtures.

These exercise the *parsing* logic (parse_tables + extract_country_data) that
every H1-H5 fix touched, WITHOUT hitting the network — possible because
parse_tables is decoupled from fetching (it takes a soup). Each fixture is a
real bulletin from a distinct format era; each test pins the behavior of one
historical quirk so a future refactor can't silently regress it.

    ante/bin/python tests/test_extraction.py
"""
import sys
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).resolve().parent / "fixtures"

import scrape_family_visa_bulletins as fam  # noqa: E402
import scrape_visa_bulletins as emp  # noqa: E402
from visa_common import parse_tables  # noqa: E402

VALID_STATUS = {"C", "F", "U", "UNK"}


def _soup(name):
    return BeautifulSoup((FIX / name).read_text(encoding="utf-8"), "html.parser")


def _emp(name, ym, country="mexico"):
    tables = parse_tables(_soup(name), datetime(*ym), emp.is_employment_section)
    return emp.extract_country_data(country, [t.copy() for t in tables])


def _fam(name, ym, country="mexico"):
    tables = parse_tables(_soup(name), datetime(*ym), fam.is_family_section)
    return fam.extract_country_data(country, [t.copy() for t in tables])


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
    for name, ym in [("vb_2002_06.html", (2002, 6, 1)),
                     ("vb_2007_06.html", (2007, 6, 1)),
                     ("vb_2020_06.html", (2020, 6, 1)),
                     ("vb_2022_06.html", (2022, 6, 1))]:
        d = _emp(name, ym)
        bad = set(d.status) - VALID_STATUS
        assert not bad, f"{name}: estados inválidos {bad}"
        # every 'F' row must carry a parsed date ('C' maps to the bulletin date
        # by design in the legacy column, so we don't assert on non-F rows here).
        miss = (d.status.eq("F") & d.priority_date.isna()).sum()
        assert miss == 0, f"{name}: {miss} filas F sin fecha"


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{passed}/{passed + failed} pruebas de extracción OK" +
          (" ✓" if not failed else f"  ({failed} FALLAN)"))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
