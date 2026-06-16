"""Scrape the Diversity Visa (DV) regional rank cut-offs from every monthly U.S.
Visa Bulletin and write data/raw/dv_visa_rank_timecourse.csv.

DV is the category family the employment/family scrapers ignore. Its published
value is a regional RANK NUMBER (e.g. "AFRICA 55,000"), not a priority date, so
it cannot live in the date panel y_{p,c,b,t}; it is its own dataset, loaded into
the fact_dv_rank star table (grain: 6 regions x bulletin month). The structured
table format is parsed first; the 2001-2004 single-cell "blob" format is the
fallback (see extract_dv_blob).

    ante/bin/python scrape_dv_visa_bulletins.py
"""

import logging
import re

import pandas as pd
from tqdm import tqdm

from config import RAW_DIR
from visa_common import (
    SITE_ROOT,
    extract_datetime_from_link,
    extract_month_links,
    get_soup,
    norm_label,
    parse_tables,
    report_failures,
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

# slug -> 2-letter region code, for the 2001-2004 "blob" format where a whole
# month's DV ranks live in one cell ("AFRICA:  AF 21,400 ASIA:  AS 9,500 …").
REGION_CODES = [
    ("africa", "AF"),
    ("asia", "AS"),
    ("europe", "EU"),
    ("north_america", "NA"),
    ("oceania", "OC"),
    ("south_america_caribbean", "SA"),
]


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
                exc = re.sub(r"\s+", " ", str(r.iloc[exc_idx])).strip()
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


def extract_dv_blob(soup, year_month) -> pd.DataFrame:
    """Recover the 2001-2004 single-cell DV format ("AFRICA:  AF 21,400 …").

    Used only as a fallback when the structured-table parser finds nothing. The
    whole month is one cell, so we locate that cell (region names + codes) and
    anchor on each unique 2-letter code, taking the number that follows. Requires
    >=4 regions to count as a real DV blob (avoids false positives).
    """
    blob = None
    for table in soup.find_all("table"):
        txt = table.get_text(" ", strip=True)
        if (
            "africa" in txt.lower()
            and "asia" in txt.lower()
            and re.search(r"\bAF\b", txt)
            and re.search(r"\bOC\b", txt)
        ):
            blob = txt
            break
    if blob is None:
        return pd.DataFrame()

    out = []
    for slug, code in REGION_CODES:
        m = re.search(rf"\b{code}\s+([\d,]+|CURRENT|U)\b", blob)
        if not m:
            continue
        status, rank = classify_dv_rank(m.group(1))
        out.append(
            {
                "region": slug,
                "rank_cutoff": rank,
                "status": status,
                "raw_value": m.group(1).strip(),
                "exceptions": "",
                "visa_bulletin_date": year_month,
            }
        )
    return pd.DataFrame(out) if len(out) >= 4 else pd.DataFrame()


def extract_month_rows(soup, ym) -> pd.DataFrame:
    """DV rows for one bulletin: the structured table first, the 2001-2004
    single-cell 'blob' format as fallback. Shared by main() and scrape_all.py."""
    rows = extract_dv_data(parse_tables(soup, ym, is_dv_section))
    if rows.empty:  # 2001-2004 single-cell ("blob") format
        rows = extract_dv_blob(soup, ym)
    return rows


def finalize(frames: list[pd.DataFrame]) -> None:
    """Concatenate the per-month DV frames into the deduped, sorted CSV. Shared
    by main() and the single-fetch scrape_all.py driver."""
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    df = df[df["visa_bulletin_date"].notna()]
    # Deterministic order (newest first) and a unique (region, month) key.
    df = df.drop_duplicates(subset=["region", "visa_bulletin_date"], keep="first")
    df = df.sort_values(by=["visa_bulletin_date", "region"], ascending=[False, True])

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(RAW_DIR / "dv_visa_rank_timecourse.csv", index=False)


def main() -> None:
    month_links = extract_month_links()
    frames = []
    failed = []
    for link in tqdm(month_links, desc="Extracting all diversity-visa bulletin tables"):
        try:
            rows = extract_month_rows(get_soup(SITE_ROOT + link), extract_datetime_from_link(link))
            if not rows.empty:
                frames.append(rows)
        except Exception as exc:
            failed.append((link, str(exc)[:60]))
    report_failures(failed, logger)
    finalize(frames)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
