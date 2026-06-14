from typing import List, Union

import pandas as pd
from tqdm import tqdm

from visa_common import (
    SITE_ROOT, SCRAPER_COUNTRIES, MAX_FETCH_FAILURES,
    extract_datetime_from_link, get_soup, extract_month_links, parse_tables,
    string_to_datetime, classify_status, _norm_label,
)


def is_family_section(rows) -> bool:
    """A table is the family section if a row contains the 'family' substring
    (the header is 'family' / 'family- sponsored', sometimes concatenated as
    'familyall chargeability...' in 2007-2008). Employment and diversity-visa
    tables never contain it."""
    return any('family' in row.get_text(strip=True).lower() for row in rows)


def extract_tables(link: str) -> List[pd.DataFrame]:
    return parse_tables(get_soup(SITE_ROOT + link),
                        extract_datetime_from_link(link), is_family_section)

def classify_family_category(raw) -> Union[None, str]:
    """Map a raw 'Family-Sponsored' row label to a canonical level code,
    absorbing label drift ('1st'->F1 in 2006-2011, 'F1' from 2011 on, and the
    '*' footnote variants). Returns None for non-category rows (e.g. the
    'family' spanning header). Codes match the legacy values: 1, 2A, 2B, 3, 4.
    """
    s = _norm_label(raw)
    if s in ('1st', 'f1'):
        return '1'
    if s in ('2a', '2a*', '2nd-a', '2nda', 'f2a', 'f2a*'):
        return '2A'
    if s in ('2b', '2b*', '2nd-b', '2ndb', 'f2b', 'f2b*'):
        return '2B'
    if s in ('3rd', 'f3'):
        return '3'
    if s in ('4th', 'f4'):
        return '4'
    return None

def extract_country_data(country: str, all_data: List[pd.DataFrame]) -> pd.DataFrame:
        # 'row' (Rest of World) lives in the "all chargeability areas except
        # those listed" column; match 'except those listed', which is stable
        # even when older bulletins split 'chargeability' as 'charge ability'.
        search_country = 'except those listed' if country == 'row' else country

        country_data = []
        for df in all_data:
            norm = {col: _norm_label(col) for col in df.columns}

            # The family-category column is always column 0 (header is
            # 'family- sponsored', 'family', or '' across the years).
            cat_col = df.columns[0]
            country_col = next((c for c in df.columns if search_country in norm[c]), None)
            if country_col is None or country_col == cat_col:
                continue

            try:
                df_subset = df[[cat_col, country_col, 'visa_bulletin_date', 'table_type']].copy()
                df_subset.columns = ['F_level', 'final_action_dates', 'visa_bulletin_date', 'table_type']
                country_data.append(df_subset)
            except Exception:
                pass

        if not country_data:
            return pd.DataFrame(columns=['F_level', 'final_action_dates', 'visa_bulletin_date', 'visa_wait_time', 'table_type'])

        country_df = pd.concat(country_data, axis=0, ignore_index=True)

        country_df = country_df[country_df['visa_bulletin_date'].notna()]
        # Preserve the raw published cell and its C/F/U/UNK regime BEFORE the
        # cell is flattened into a date (H1 fix: keep the annotation).
        country_df['raw_value'] = country_df['final_action_dates']
        country_df['status'] = country_df['final_action_dates'].apply(classify_status)
        country_df['final_action_dates'] = country_df.apply(lambda row: string_to_datetime(row['final_action_dates'], row['visa_bulletin_date']), axis=1)
        country_df['visa_wait_time'] = country_df.apply(
            lambda row: (row['visa_bulletin_date'] - row['final_action_dates']).days / 365.25
            if pd.notna(row['final_action_dates']) and pd.notna(row['visa_bulletin_date']) else None, axis=1)

        # Map the raw 'Family-Sponsored' label to a canonical level code
        # (1, 2A, 2B, 3, 4); drop rows that are not a family category.
        country_df['F_level'] = country_df['F_level'].apply(classify_family_category)
        country_df = country_df[country_df['F_level'].notna()]

        # Keep a unique (level, month, table) key (guards against any label
        # transition putting the same category twice in one bulletin).
        country_df = country_df.drop_duplicates(
            subset=['F_level', 'visa_bulletin_date', 'table_type'], keep='first')

        return country_df

def main():
    month_links = extract_month_links()

    all_data = []
    failed = []
    for i, link in tqdm(enumerate(month_links), total=len(month_links),
                        desc="Extracting all family-sponsored visa bulletin tables"):
        try:
            table_data = extract_tables(link)
            all_data.extend(table_data)
        except Exception as exc:
            failed.append((link, str(exc)[:60]))
    if failed:
        print(f"\n⚠️  {len(failed)} boletines fallaron tras reintentos (meses perdidos):")
        for link, err in failed:
            print(f"   {link.split('/')[-1]}  {err}")
        if len(failed) > MAX_FETCH_FAILURES:
            raise SystemExit(
                f"{len(failed)} boletines fallaron (> {MAX_FETCH_FAILURES}): probable "
                f"problema de la fuente, no un blip transitorio. Se aborta sin escribir "
                f"para no publicar un panel degradado.")

    countries = SCRAPER_COUNTRIES
    for country in tqdm(countries, desc=f"Extracting data for each country and computing backlogs"):
        country_df = extract_country_data(country, all_data)
        # Deterministic order (newest first, then table then category): a fully
        # specifying key, so a transient dropped month cannot cascade-reorder the
        # rest via an unstable sort.
        country_df = country_df.sort_values(
            by=['visa_bulletin_date', 'table_type', 'F_level'],
            ascending=[False, True, True])
        country_df.to_csv(f'data/{country}_family_visa_backlog_timecourse.csv', index=False)


if __name__ == "__main__":
    main()
