"""Unit tests for the Visa Bulletin parsing/normalization functions.

Covers the regime classifier (C/F/U/NA), the employment category mapper (20+
years of label drift, substring disambiguation) and the family category mapper.
These are the pure functions the whole panel depends on.

Runs with pytest *or* as a plain script (no pytest required):
    ante/bin/python tests/test_parsers.py

⚠️ CONTRATO plain-script (O6): el gate del cron (freeze_and_rebuild.yml) ejecuta
este archivo como `python tests/<archivo>.py` SIN pytest — nada de fixtures,
parametrize ni markers aquí: un test que dependa de pytest correría en CI pero
se rompería o saltaría en el cron sin que nadie lo note.
"""

import sys

from pipeline.scrape_family_visa_bulletins import classify_family_category  # noqa: E402
from pipeline.scrape_visa_bulletins import classify_eb_category  # noqa: E402
from vp_data.visa_common import (  # noqa: E402
    classify_status,
    extract_datetime_from_link,
    norm_label,
    string_to_datetime,
)


# ---- classify_status (C/F/U/NA) ----------------------------------------
def test_status_basic():
    assert classify_status("C") == "C"
    assert classify_status("U") == "U"
    assert classify_status("01MAY16") == "F"
    assert classify_status("") == "UNK"
    assert classify_status(None) == "UNK"
    assert classify_status(float("nan")) == "UNK"


def test_status_whitespace_and_case():
    assert classify_status(" c ") == "C"
    assert classify_status("u") == "U"
    assert classify_status(" 01MAY16 ") == "F"
    assert classify_status("garbage") == "UNK"


def test_status_two_digit_year_boundary():
    # %y maps 00-68 -> 20xx, 69-99 -> 19xx; priority dates 1979+ all valid F.
    assert classify_status("01NOV79") == "F"
    assert classify_status("22JUN22") == "F"


# ---- string_to_datetime (I3: first direct coverage of the panel's core) -
def test_std_basic():
    from datetime import datetime

    b = datetime(2016, 5, 1)
    assert string_to_datetime("01MAY16", b) == datetime(2016, 5, 1)
    assert string_to_datetime("C", b) == b  # Current -> bulletin date (legacy)
    assert string_to_datetime("U", b) is None
    assert string_to_datetime("", b) is None
    assert string_to_datetime(float("nan"), b) is None


def test_std_century_pivot():
    # %y maps 00-68 -> 20xx; a cutoff can never postdate its bulletin, so a
    # "future" parse is a mispivoted 19xx date and gets -100 years.
    from datetime import datetime

    b = datetime(2010, 3, 1)
    assert string_to_datetime("15AUG26", b) == datetime(1926, 8, 15)  # 2026 > 2010 -> 1926
    assert string_to_datetime("01NOV79", b) == datetime(1979, 11, 1)  # 69-99 -> 19xx directo
    # una celda del PROPIO mes del boletín (día >= 2) NO se corrige (fix H1)
    assert string_to_datetime("08MAR10", b) == datetime(2010, 3, 8)


def test_std_footnote_and_garbage():
    from datetime import datetime

    b = datetime(2016, 5, 1)
    assert string_to_datetime("15JUL05*", b) == datetime(2005, 7, 15)  # footnote fallback
    assert string_to_datetime("31JUN08", b) is None  # impossible date
    assert string_to_datetime("00MAY16", b) is None
    assert string_to_datetime("15XYZ05", b) is None


# ---- extract_datetime_from_link (I3/I1: filename variants) ---------------
def test_link_canonical_and_archive_variants():
    from datetime import datetime

    # canonical since ~2003
    assert extract_datetime_from_link("visa-bulletin-for-june-2026.html") == datetime(2026, 6, 1)
    # I1: the 5 hand-recovered archive months omit the "for-" -- the old regex
    # silently ignored those real bulletins sitting in data/snapshots/
    assert extract_datetime_from_link("visa-bulletin-march-2009.html") == datetime(2009, 3, 1)
    assert extract_datetime_from_link("visa-bulletin-october-2012.html") == datetime(2012, 10, 1)


def test_link_non_bulletins_stay_out():
    # duplicate-naming oddity and a table-less announcement must NOT map
    assert extract_datetime_from_link("july-2007-visa-bulletin.html") is None
    assert extract_datetime_from_link("update-on-july-visa-availability.html") is None
    assert extract_datetime_from_link("visa-bulletin-for-notamonth-2020.html") is None


def test_status_footnoted_regime_letters():
    # J4: 'C*'/'U*' (a plausible footnote in a retrogression note) must keep
    # the regime instead of falling to UNK — same tolerance dates and category
    # labels already had.
    assert classify_status("C*") == "C"
    assert classify_status("U*") == "U"
    assert classify_status("c† ") == "C"


def test_family_footnote_parity_with_eb():
    # J3: every family level tolerates a footnote marker, not just 2A/2B.
    assert classify_family_category("F1*") == "1"
    assert classify_family_category("1st*") == "1"
    assert classify_family_category("2A*") == "2A"
    assert classify_family_category("F2B*") == "2B"
    assert classify_family_category("3rd*") == "3"
    assert classify_family_category("F4*") == "4"
    assert classify_family_category("4th*") == "4"
    assert classify_family_category("family") is None  # header row still out


def test_status_impossible_dates_are_unk():
    # J1: the token regex alone said F to date-shaped garbage while
    # string_to_datetime returned None -> one source typo (a real risk:
    # "religiuos" 2004, "4rd" 2003) killed the whole cron via the panel's
    # F-with-NaT fail-fast. Both functions must agree: garbage -> UNK.
    assert classify_status("31JUN08") == "UNK"  # June has 30 days
    assert classify_status("00MAY16") == "UNK"  # day zero
    assert classify_status("15XYZ05") == "UNK"  # not a month
    assert classify_status("15JUL05*") == "F"  # genuine footnoted date still F


# ---- classify_eb_category (16 canonical codes) -------------------------
def test_eb_numbered():
    assert classify_eb_category("1st") == "EB1"
    assert classify_eb_category("2nd") == "EB2"
    assert classify_eb_category("3rd") == "EB3"
    assert classify_eb_category("4th") == "EB4"


def test_eb_subcategories():
    assert classify_eb_category("Other Workers") == "EB3_OW"
    assert classify_eb_category("Other Workers*") == "EB3_OW"
    assert classify_eb_category("Certain Religious\nWorkers") == "EB4_RW"
    assert classify_eb_category("Iraqi & Afghani Translators") == "EB4_TRANS"


def test_eb5_eras():
    assert classify_eb_category("5th") == "EB5"
    assert classify_eb_category("Targeted Employment Areas/Regional Centers") == "EB5_TEA"
    assert classify_eb_category("5th Pilot Programs") == "EB5_PILOT"
    assert classify_eb_category("5th\xa0Regional\xa0Center\n(I5 and R5)") == "EB5_RC"
    assert classify_eb_category("5th Non-Regional\xa0Center\n(C5 and T5)") == "EB5_NONRC"
    assert classify_eb_category("5th Unreserved\n(including C5, T5, I5, R5)") == "EB5_UNRESERVED"


def test_eb5_setasides():
    assert classify_eb_category("5th Set Aside:\n(Rural: NR, RR - 20%)") == "EB5_RURAL"
    assert classify_eb_category("5th Set Aside:\nHigh Unemployment (10%)") == "EB5_HIGHUNEMP"
    assert classify_eb_category("5th Set Aside:\nInfrastructure (2%, including RI)") == "EB5_INFRA"


def test_eb5_disambiguation():
    # 'regional center' is a substring of 'non-regional center' and appears in
    # the pre-2015 TEA label; ordering must keep these distinct.
    assert classify_eb_category("Targeted Employment Areas/Regional Centers") != "EB5_RC"
    assert classify_eb_category("5th Non-Regional Center (C5 and T5)") == "EB5_NONRC"
    assert classify_eb_category("5th Regional Center (I5 and R5)") == "EB5_RC"


def test_eb_out_of_scope():
    assert classify_eb_category("Schedule A Workers") is None
    assert classify_eb_category("") is None
    assert classify_eb_category(None) is None
    assert classify_eb_category("Employment-Based") is None  # spanning header row


# ---- classify_family_category (1/2A/2B/3/4) ----------------------------
def test_family_levels():
    assert classify_family_category("1st") == "1"
    assert classify_family_category("F1") == "1"
    assert classify_family_category("2A") == "2A"
    assert classify_family_category("F2A") == "2A"
    assert classify_family_category("2A*") == "2A"
    assert classify_family_category("2B") == "2B"
    assert classify_family_category("F2B") == "2B"
    assert classify_family_category("3rd") == "3"
    assert classify_family_category("F3") == "3"
    assert classify_family_category("4th") == "4"
    assert classify_family_category("F4") == "4"


def test_family_out_of_scope():
    assert classify_family_category("family") is None
    assert classify_family_category("") is None
    assert classify_family_category(None) is None


# ---- norm_label --------------------------------------------------------
def test_norm_label():
    assert norm_label("Other\nWorkers") == "other workers"
    assert norm_label("5th\xa0\xa0Regional") == "5th regional"
    assert norm_label("  MiXeD  Case  ") == "mixed case"
    assert norm_label(None) == ""


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
    print(f"\n{passed}/{passed + failed} pruebas OK" + (" ✓" if not failed else f"  ({failed} FALLAN)"))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
