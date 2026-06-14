from typing import List, Union
import re

import pandas as pd
from tqdm import tqdm

from visa_common import (
    SITE_ROOT, SCRAPER_COUNTRIES,
    extract_datetime_from_link, get_soup, extract_month_links, parse_tables,
    string_to_datetime, classify_status, _norm_label,
)


def is_employment_section(rows) -> bool:
    """A table is the employment section if a row mentions 'employment-based',
    tolerating spacing drift ('employment- based', 'employment based')."""
    return any(re.search(r'employment[\s-]*based', row.get_text(strip=True).lower())
               for row in rows)


def extract_tables(link: str) -> List[pd.DataFrame]:
    return parse_tables(get_soup(SITE_ROOT + link),
                        extract_datetime_from_link(link), is_employment_section)

def classify_eb_category(raw) -> Union[None, str]:
    """Map a raw 'Employment-based' row label to a canonical category code,
    absorbing 20+ years of label drift. Returns None for rows that are not an
    EB-1..EB-5 preference line (e.g. Schedule A, footnotes).

    Canonical codes (H3):
      EB1 EB2 EB3 EB3_OW EB4 EB4_RW EB4_TRANS EB5
      EB5_TEA EB5_PILOT EB5_RC EB5_NONRC
      EB5_UNRESERVED EB5_RURAL EB5_HIGHUNEMP EB5_INFRA

    Order matters: 'targeted employment' and 'non-regional center' must be
    tested before the bare 'regional center' substring they contain, and the
    post-2022 set-asides before the generic EB-5 checks.
    """
    s = _norm_label(raw)
    if not s:
        return None
    # Numbered preferences
    if s == '1st':
        return 'EB1'
    if s == '2nd':
        return 'EB2'
    if s == '3rd':
        return 'EB3'
    if s == '4th':
        return 'EB4'
    # EB-3 subcategory
    if s.startswith('other worker'):
        return 'EB3_OW'
    # EB-4 subcategories
    if 'religious' in s:
        return 'EB4_RW'
    if 'translator' in s:
        return 'EB4_TRANS'
    # EB-5 post-2022 set-asides
    if 'set aside' in s or 'set-aside' in s:
        if 'rural' in s:
            return 'EB5_RURAL'
        if 'high unemployment' in s:
            return 'EB5_HIGHUNEMP'
        if 'infrastructure' in s:
            return 'EB5_INFRA'
        return 'EB5_UNRESERVED'  # defensive fallback
    if 'unreserved' in s:
        return 'EB5_UNRESERVED'
    # EB-5 pre-2015 targeted-employment / pilot (TEA contains 'regional center')
    if 'targeted employment' in s:
        return 'EB5_TEA'
    if 'pilot program' in s:
        return 'EB5_PILOT'
    # EB-5 2015-2022 regional-center split ('non-regional' contains 'regional')
    if 'non-regional center' in s:
        return 'EB5_NONRC'
    if 'regional center' in s:
        return 'EB5_RC'
    # Bare 5th (2003-2011)
    if s == '5th':
        return 'EB5'
    # Schedule A workers and anything else: outside EB-1..5 scope
    return None

def extract_country_data(country: str, all_data: List[pd.DataFrame]) -> pd.DataFrame:
        # 'row' (Rest of World) lives in the "all chargeability areas except
        # those listed" column; match 'except those listed', which is stable
        # even when older bulletins split 'chargeability' as 'charge ability'.
        search_country = 'except those listed' if country == 'row' else country

        country_data = []
        for df in all_data:
            norm = {col: _norm_label(col) for col in df.columns}

            # The EB-category column is always column 0 (header is
            # 'employment-based', 'employment -based', or '' in 2001-2003).
            cat_col = df.columns[0]
            # Country column by normalized-substring match (handles \xa0, \n,
            # double spaces and case across 20+ years of bulletin formats).
            country_col = next((c for c in df.columns if search_country in norm[c]), None)
            if country_col is None or country_col == cat_col:
                continue

            try:
                df_subset = df[[cat_col, country_col, 'visa_bulletin_date', 'table_type']].copy()
                df_subset.columns = ['EB_level', 'final_action_dates', 'visa_bulletin_date', 'table_type']
                country_data.append(df_subset)
            except Exception:
                pass

        if not country_data:
            return pd.DataFrame(columns=['EB_level', 'final_action_dates', 'visa_bulletin_date',
                                         'table_type', 'raw_value', 'status', 'visa_wait_time'])

        country_df = pd.concat(country_data, axis=0, ignore_index=True)

        country_df = country_df[country_df['visa_bulletin_date'].notna()]
        # Preserve the raw published cell and its C/F/U/UNK regime BEFORE the
        # cell is flattened into a date (H1 fix: keep the annotation).
        country_df['raw_value'] = country_df['final_action_dates']
        country_df['status'] = country_df['final_action_dates'].apply(classify_status)
        # calculate backlog period length (difference in months between 'india' and 'bulletin_year_month')
        country_df['final_action_dates'] = country_df.apply(lambda row: string_to_datetime(row['final_action_dates'], row['visa_bulletin_date']), axis=1)
        country_df['visa_wait_time'] = country_df.apply(
            lambda row: (row['visa_bulletin_date'] - row['final_action_dates']).days / 365.25
            if pd.notna(row['final_action_dates']) and pd.notna(row['visa_bulletin_date']) else None, axis=1)
        
        # Map the raw 'Employment-based' label to a canonical category code
        # (EB1..EB5 + subcategories); drop rows that are not an EB preference (H3).
        country_df['EB_level'] = country_df['EB_level'].apply(classify_eb_category)
        country_df = country_df[country_df['EB_level'].notna()]

        # A label transition can put the same canonical category twice in one
        # bulletin (e.g. the May-2022 EB-5 'Unreserved' split); keep the first
        # so the (category, month, table) key stays unique.
        country_df = country_df.drop_duplicates(
            subset=['EB_level', 'visa_bulletin_date', 'table_type'], keep='first')

        return country_df

def main():
    month_links = extract_month_links()
    
    all_data = []
    failed = []
    for i, link in tqdm(enumerate(month_links), total=len(month_links),
                        desc="Extracting all employment-based visa bulletin tables"):
        try:
            table_data = extract_tables(link)
            all_data.extend(table_data)
        except Exception as exc:
            failed.append((link, str(exc)[:60]))
    if failed:
        print(f"\n⚠️  {len(failed)} boletines fallaron tras reintentos (meses perdidos):")
        for link, err in failed:
            print(f"   {link.split('/')[-1]}  {err}")

    countries = SCRAPER_COUNTRIES
    for country in tqdm(countries, desc=f"Extracting data for each country and computing backlogs"):
        country_df = extract_country_data(country, all_data)
        country_df = country_df.sort_values(by='visa_bulletin_date', ascending=False)
        country_df.to_csv(f'data/{country}_visa_backlog_timecourse.csv', index=False)


if __name__ == "__main__":
    main()
