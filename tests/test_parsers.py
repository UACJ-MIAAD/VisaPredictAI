"""Unit tests for the Visa Bulletin parsing/normalization functions.

Covers the regime classifier (C/F/U/NA), the employment category mapper (20+
years of label drift, substring disambiguation) and the family category mapper.
These are the pure functions the whole panel depends on.

Runs with pytest *or* as a plain script (no pytest required):
    ante/bin/python tests/test_parsers.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrape_family_visa_bulletins import classify_family_category  # noqa: E402
from scrape_visa_bulletins import (  # noqa: E402
    _norm_label,
    classify_eb_category,
    classify_status,
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


# ---- _norm_label -------------------------------------------------------
def test_norm_label():
    assert _norm_label("Other\nWorkers") == "other workers"
    assert _norm_label("5th\xa0\xa0Regional") == "5th regional"
    assert _norm_label("  MiXeD  Case  ") == "mixed case"
    assert _norm_label(None) == ""


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
    print(f"\n{passed}/{passed + failed} pruebas OK" +
          (" ✓" if not failed else f"  ({failed} FALLAN)"))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
