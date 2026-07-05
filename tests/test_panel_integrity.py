"""Integrity contract for data/processed/visa_panel_long.csv.

Encodes the mega-audit invariants as hard assertions so a regression in any
scraper or in build_panel.py fails loudly. Suitable as a CI quality gate
(run after build_panel.py, before committing the data).

    ante/bin/python tests/test_panel_integrity.py

⚠️ CONTRATO plain-script (O6): el gate del cron (freeze_and_rebuild.yml) ejecuta
este archivo como `python tests/<archivo>.py` SIN pytest — nada de fixtures,
parametrize ni markers aquí: un test que dependa de pytest correría en CI pero
se rompería o saltaría en el cron sin que nadie lo note.
"""

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.csv"

from vp_data.config import BASE_EPOCH, DEAD_MONTHS, DV_RANK_PATH  # noqa: E402

# Diversity-Visa coverage floors. DV is network-scraped and its early (blob) era
# is partial, so the gate uses FLOORS (>=) that pin the established good state and
# fail on real degradation, tolerating transient ±1-2 month scrape variation.
DV_FLOOR = pd.Timestamp("2001-12-01")
DV_MIN_ROWS = 1550
DV_MIN_MONTHS = 255
DV_MIN_COMPLETE_MONTHS = 255  # months carrying all 6 DV regions

EXPECTED_COLS = [
    "country",
    "block",
    "category",
    "table",
    "bulletin_date",
    "status",
    "priority_date",
    "days_since_base",
    "raw_value",
]
VALID_STATUS = {"C", "F", "U", "UNK"}
KEY = ["country", "block", "category", "table", "bulletin_date"]
# Floor of the official online source (deep_missing_search.py).
MIN_BULLETIN = pd.Timestamp("2001-12-01")


def _panel():
    return pd.read_csv(PANEL, parse_dates=["bulletin_date", "priority_date"])


def test_schema():
    p = _panel()
    assert list(p.columns) == EXPECTED_COLS, f"columnas inesperadas: {list(p.columns)}"


def test_key_unique():
    p = _panel()
    n = int(p.duplicated(subset=KEY).sum())
    assert n == 0, f"{n} claves duplicadas"


def test_status_domain():
    p = _panel()
    bad = set(p.status.unique()) - VALID_STATUS
    assert not bad, f"estados inválidos: {bad}"


def test_priority_only_when_F():
    p = _panel()
    leak = int(p[p.status != "F"].priority_date.notna().sum())
    assert leak == 0, f"{leak} fechas de prioridad en filas no-F"


def test_days_defined_iff_F():
    p = _panel()
    mismatch = int((p.days_since_base.notna() != (p.status == "F")).sum())
    assert mismatch == 0, f"{mismatch} filas con days_since_base mal definido"


def test_days_since_base_arithmetic():
    # O1: nothing re-verified the arithmetic after the build — a BASE_EPOCH
    # change in config.py without a rebuild (or a calc regression) shifted the
    # whole dependent variable with every gate green. Recompute from scratch.
    p = _panel()
    f = p[p.status == "F"]
    expected = (f.priority_date - pd.Timestamp(BASE_EPOCH)).dt.days
    mismatches = int((f.days_since_base != expected).sum())
    assert mismatches == 0, f"{mismatches} filas con days_since_base ≠ (priority_date - t0).days"


def test_schema_epoch_matches_config():
    # H2 (audit r4): the warehouse CHECK re-hardcodes the epoch as a SQL literal
    # (DATE '1975-01-01'), which can't import BASE_EPOCH. It's fail-loud (a
    # mismatch aborts the DB load), but this catches the drift at its source: the
    # .sql literal must equal vp_data.config.BASE_EPOCH.
    schema = (ROOT / "schema.sql").read_text()
    assert f"DATE '{BASE_EPOCH}'" in schema, (
        f"schema.sql no usa el epoch canónico BASE_EPOCH={BASE_EPOCH!r} — sincroniza el literal SQL con vp_data.config."
    )


def test_no_negative_days():
    p = _panel()
    neg = int((p.days_since_base < 0).sum())
    assert neg == 0, f"{neg} días negativos"


def test_bulletin_floor():
    p = _panel()
    assert p.bulletin_date.min() >= MIN_BULLETIN, f"boletín anterior al piso esperado: {p.bulletin_date.min()}"


def test_priority_not_in_future():
    # A Final Action / Dates-for-Filing date cannot exceed its bulletin month.
    p = _panel()
    f = p[p.status == "F"]
    future = int((f.priority_date > f.bulletin_date).sum())
    assert future == 0, f"{future} fechas de prioridad posteriores al boletín"


def test_min_rows():
    # Row-count sanity gate: guard against a parser regression silently gutting
    # the panel. Current build ~27k; alert if it falls below 20k.
    p = _panel()
    assert len(p) >= 20_000, f"panel demasiado pequeño ({len(p)} filas) — posible regresión"


def test_min_trainable_fraction():
    # K4: a classify_status regression degrading F -> UNK en masse passed every
    # gate (rows stay, keys stay, months stay). The trainable fraction has never
    # dipped below ~55% historically (57.7% today); 50% is a loose floor that
    # only a real regression crosses.
    p = _panel()
    pct = (p.status == "F").mean()
    assert pct >= 0.50, f"fracción entrenable F cayó a {pct:.1%} (< 50%) — ¿regresión de classify_status?"


def test_min_series():
    # K1: the 20k row floor tolerated losing an entire country (~5k rows). The
    # structural series count is stable (194 today); a real drop means a country
    # or category family vanished from the sources.
    p = _panel()
    n = p.groupby(["country", "block", "category", "table"]).ngroups
    assert n >= 190, f"solo {n} series estructurales (< 190) — ¿desapareció un país/categoría?"


def test_min_rows_per_country():
    # K1: per-country floor — every chargeability area carries 5,098–5,596 rows
    # today; below 4,500 means a parser regression gutted that country. This is
    # the gate the audit proved missing: an empty source CSV passed everything.
    p = _panel()
    counts = p.country.value_counts()
    expected = {"mexico", "india", "china", "philippines", "all_chargeability"}
    missing = expected - set(counts.index)
    assert not missing, f"países ausentes del panel: {sorted(missing)}"
    low = {c: int(n) for c, n in counts.items() if n < 4_500}
    assert not low, f"países con volumen degradado (< 4,500 filas): {low}"


def test_no_unexpected_missing_months():
    # F1 fix: a chronically flaky month (e.g. 2007-12 hits a redirect loop and
    # fails all retries) must NOT drop silently from the daily commit. The
    # row-count gate above is too loose to notice ~65 missing rows, so check
    # month completeness exactly: every month in the panel's own span must be
    # present, except the months that are genuinely absent from the source
    # (404 + Wayback-only). Any other gap fails the gate, so the daily Action
    # aborts (does not commit) and the issue-on-failure step alerts.
    p = _panel()
    per = p.bulletin_date.dt.to_period("M")
    full = {str(m) for m in pd.period_range(per.min(), per.max(), freq="M")}
    present = {str(m) for m in per.unique()}
    missing = full - present - set(DEAD_MONTHS)
    assert not missing, f"meses ausentes no explicados (solo se esperan los muertos): {sorted(missing)}"


def test_no_missing_months_per_block_table():
    # Finer-grained companion to test_no_unexpected_missing_months. The union
    # gate above only checks a month exists in *some* block, so a transient
    # single-block loss slips through: e.g. employment 2007-12 hits a redirect
    # loop and fails all retries, yet family still carries 2007-12, so the union
    # stays complete and the ~40 dropped employment rows commit silently. Check
    # completeness within each (block, table) over ITS OWN span instead, so an
    # employment-only gap fails the gate and the daily Action aborts. Per-series
    # is intentionally NOT used: many EB-5 series are legitimately short or
    # discontinuous (category-regime changes), which would make it flap.
    p = _panel()
    dead = set(DEAD_MONTHS)
    offenders = {}
    for (block, table), g in p.groupby(["block", "table"]):
        per = g.bulletin_date.dt.to_period("M")
        full = {str(m) for m in pd.period_range(per.min(), per.max(), freq="M")}
        present = {str(m) for m in per.unique()}
        gaps = sorted(full - present - dead)
        if gaps:
            offenders[f"{block}/{table}"] = gaps
    assert not offenders, f"meses ausentes dentro del span de un (bloque,tabla): {offenders}"


def test_dv_coverage_floor():
    # Diversity Visa has its own completeness gate (the panel's gates don't see
    # it). Floors pin the established coverage so the daily Action aborts if a
    # scrape silently degrades DV — the gap the brutal audit found.
    dv = pd.read_csv(DV_RANK_PATH, parse_dates=["visa_bulletin_date"])
    assert len(dv) >= DV_MIN_ROWS, f"DV degradado: {len(dv)} filas < {DV_MIN_ROWS}"
    assert set(dv.status.unique()) <= VALID_STATUS, f"estado DV inválido: {set(dv.status.unique())}"
    months = dv["visa_bulletin_date"].dt.to_period("M")
    assert months.min() <= DV_FLOOR.to_period("M"), f"DV perdió el piso {DV_FLOOR.date()}: {months.min()}"
    assert months.nunique() >= DV_MIN_MONTHS, f"cobertura DV cayó a {months.nunique()} meses"
    complete = int((dv.groupby("visa_bulletin_date").region.nunique() == 6).sum())
    assert complete >= DV_MIN_COMPLETE_MONTHS, f"meses DV con 6 regiones cayó a {complete}"


def _run():
    if not PANEL.exists():
        print(f"✗ no existe {PANEL}; corre build_panel.py primero")
        return False
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{passed}/{passed + failed} invariantes OK" + (" ✓" if not failed else f"  ({failed} FALLAN)"))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
