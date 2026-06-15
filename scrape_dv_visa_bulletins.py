"""Scrape the Diversity Visa (DV) regional rank cut-offs from every monthly U.S.
Visa Bulletin and write data/raw/dv_visa_rank_timecourse.csv.

DV is the category family the employment/family scrapers ignore. Its published
value is a regional RANK NUMBER (e.g. "AFRICA 55,000"), not a priority date, so
it cannot live in the date panel y_{p,c,b,t}; it is its own dataset, loaded into
the fact_dv_rank star table. Six regions x {FAD, DFF} x month.

    ante/bin/python scrape_dv_visa_bulletins.py
"""

import logging
import re

import pandas as pd
from tqdm import tqdm

from config import RAW_DIR
from visa_common import (
    MAX_FETCH_FAILURES,
    SITE_ROOT,
    extract_datetime_from_link,
    extract_month_links,
    get_soup,
    norm_label,
    parse_tables,
)

logger = logging.getLogger(__name__)

# Normalized region-label substring -> canonical slug. Substrings are stable
# across 20+ years of formatting ("NORTH AMERICA (BAHAMAS)", "SOUTH AMERICA, and
# the CARIBBEAN", …); the order is irrelevant since the keys are disjoint.
REGION_SLUGS = {
    "africa": "africa",
    "asia": "asia",
    "europe": "europe",
    "north america": "north_america",
    "oceania": "oceania",
    "south america": "south_america_caribbean",
}


def is_dv_section(rows) -> bool:
    """A table is the Diversity Visa section if a row mentions the DV
    chargeability header or 'diversity' (employment/family tables never do)."""
    return any(
        "dv chargeability" in row.get_text(strip=True).lower() or "diversity" in row.get_text(strip=True).lower()
        for row in rows
    )


def _region_slug(raw) -> None | str:
    s = norm_label(raw)
    return next((slug for key, slug in REGION_SLUGS.items() if key in s), None)


def classify_dv_rank(raw) -> tuple[str, None | int]:
    """Map a raw DV cell to (status, rank_cutoff).

    * a number ("55,000")     -> ('F', 55000)   — a specific cut-off is published
    * "CURRENT"/"C"           -> ('C', None)     — region is current this month
    * "U"/"Unavailable"       -> ('U', None)
    * empty / unparseable      -> ('UNK', None)
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "UNK", None
    s = str(raw).strip().upper()
    if s == "":
        return "UNK", None
    if "CURRENT" in s or s == "C":
        return "C", None
    if s == "U" or "UNAVAIL" in s:
        return "U", None
    # First number group only: some eras pack "AF  20,400  Except: Egypt 30,000"
    # into one cell, so stripping all digits would concatenate the exceptions.
    m = re.search(r"\d[\d,]*", s)
    return ("F", int(m.group().replace(",", ""))) if m else ("UNK", None)


def _best_value_col(df: pd.DataFrame, norm: list[str], candidates: list[int]) -> int:
    """Index of the column whose cells parse as DV ranks most often; the
    'chargeability' header breaks ties. Picking by content skips the 2-letter
    region-code column ("AF", "AS") that some eras insert before the number."""
    best, best_key = candidates[0], (-1, False)
    for i in candidates:
        score = sum(classify_dv_rank(v)[0] in ("F", "C", "U") for v in df.iloc[:, i])
        key = (score, "chargeability" in norm[i])
        if key > best_key:
            best, best_key = i, key
    return best


def extract_dv_data(all_tables: list[pd.DataFrame]) -> pd.DataFrame:
    """Turn the parsed DV tables into tidy region rows (region, rank, status…).

    Column access is POSITIONAL (``iloc``): some months emit several unnamed
    columns, so label access would return a Series and break. Region is column 0;
    the cut-off column is chosen by content (see ``_best_value_col``); the column
    that mentions 'Except' holds the per-country exceptions text.
    """
    out = []
    for df in all_tables:
        # DV has no Final-Action/Dates-for-Filing split. The two DV charts a
        # bulletin prints are the CURRENT month and an ADVANCE NOTIFICATION of an
        # upcoming month — different target months, not FAD/DFF. parse_tables tags
        # the first match 'final_action'; keep only that (the authoritative
        # current-month cut-off). Modeling the advance chart is future work.
        if "table_type" in df.columns and df["table_type"].iloc[0] != "final_action":
            continue
        norm = [norm_label(c) for c in df.columns]
        meta_idx = {i for i, c in enumerate(df.columns) if c in ("visa_bulletin_date", "table_type")}
        region_idx = 0
        candidates = [i for i in range(len(df.columns)) if i != region_idx and i not in meta_idx]
        if not candidates:
            continue
        value_idx = _best_value_col(df, norm, candidates)
        # Per-country exceptions: the column whose cells mention "Except".
        exc_idx = next(
            (
                i
                for i in candidates
                if i != value_idx and df.iloc[:, i].astype(str).str.contains("xcept", na=False).any()
            ),
            None,
        )

        for _, r in df.iterrows():
            slug = _region_slug(r.iloc[region_idx])
            if slug is None:
                continue
            status, rank = classify_dv_rank(r.iloc[value_idx])
            exc = ""
            if exc_idx is not None and pd.notna(r.iloc[exc_idx]):
                exc = str(r.iloc[exc_idx]).strip()
            out.append(
                {
                    "region": slug,
                    "rank_cutoff": rank,
                    "status": status,
                    "raw_value": str(r.iloc[value_idx]).strip(),
                    "exceptions": exc,
                    "visa_bulletin_date": r["visa_bulletin_date"],
                }
            )
    return pd.DataFrame(out)


def main() -> None:
    month_links = extract_month_links()

    all_tables: list[pd.DataFrame] = []
    failed = []
    for link in tqdm(month_links, desc="Extracting all diversity-visa bulletin tables"):
        try:
            all_tables.extend(parse_tables(get_soup(SITE_ROOT + link), extract_datetime_from_link(link), is_dv_section))
        except Exception as exc:
            failed.append((link, str(exc)[:60]))
    if failed:
        logger.warning("%d boletines fallaron tras reintentos (meses perdidos):", len(failed))
        for link, err in failed:
            logger.warning("   %s  %s", link.split("/")[-1], err)
        if len(failed) > MAX_FETCH_FAILURES:
            raise SystemExit(
                f"{len(failed)} boletines fallaron (> {MAX_FETCH_FAILURES}): probable "
                f"problema de la fuente, no un blip transitorio. Se aborta sin escribir."
            )

    df = extract_dv_data(all_tables)
    df = df[df["visa_bulletin_date"].notna()]
    # Deterministic order (newest first) and a unique (region, month) key.
    df = df.drop_duplicates(subset=["region", "visa_bulletin_date"], keep="first")
    df = df.sort_values(by=["visa_bulletin_date", "region"], ascending=[False, True])

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(RAW_DIR / "dv_visa_rank_timecourse.csv", index=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
