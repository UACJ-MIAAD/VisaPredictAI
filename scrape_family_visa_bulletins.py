from typing import List, Union
import requests
import re
import time
from datetime import datetime

from bs4 import BeautifulSoup
import pandas as pd
from tqdm import tqdm


BASE_URL = "https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html"

def extract_datetime_from_link(link: str) -> Union[None, datetime]:
    pattern = r'visa-bulletin-for-(\w+)-(\d{4})\.html$'
    match = re.search(pattern, link)

    if not match:
        return None

    month_str, year = match.groups()

    month_map = {
        'january': 1,
        'february': 2,
        'march': 3,
        'april': 4,
        'may': 5,
        'june': 6,
        'july': 7,
        'august': 8,
        'september': 9,
        'october': 10,
        'november': 11,
        'december': 12
    }

    month = month_map.get(month_str.lower())

    if not month:
        return None

    dt = datetime(year=int(year), month=month, day=1)

    return dt

def get_soup(url: str, retries: int = 4) -> BeautifulSoup:
    # Retry with backoff so a transient HTTP blip does not silently drop a whole
    # month (a bare requests.get + `except: pass` in main was losing bulletins).
    last = None
    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last

def extract_month_links() -> List[str]:
    soup = get_soup(BASE_URL)
    month_links = []

    accordion_sections = soup.find_all('div', class_='accordion parbase section')

    for section in accordion_sections:
        link_container = section.find('div', class_='tsg-rwd-accordion-copy')

        if link_container:
            links = link_container.find_all('a', href=True)

            for link in links:
                month_links.append(link['href'])

    return month_links


def extract_tables(link: str) -> List[pd.DataFrame]:
    year_month = extract_datetime_from_link(link)
    soup = get_soup('https://travel.state.gov/' + link)

    tables = soup.find_all('table')

    dfs = []
    family_table_count = 0

    for table in tables:
        rows = table.find_all('tr')
        # Detect the family section by the 'family' substring (the header is
        # 'family' / 'family- sponsored', sometimes concatenated with the next
        # cell as 'familyall chargeability...' in 2007-2008). The employment and
        # diversity-visa tables never contain it.
        if any('family' in row.get_text(strip=True).lower() for row in rows):
            family_table_count += 1
            table_type = "final_action" if family_table_count == 1 else "dates_for_filing"

            table_data = []
            for row in rows:
                th_cols = row.find_all('th')
                td_cols = row.find_all('td')
                all_cols = th_cols + td_cols
                cols = [ele.text.strip() for ele in all_cols]
                table_data.append(cols)

            if len(table_data[0]) == 1:
                columns = table_data[1]
                table_body = table_data[2:]
            else:
                columns = table_data[0]
                table_body = table_data[1:]

            df = pd.DataFrame(table_body, columns=columns)
            df['visa_bulletin_date'] = year_month
            df['table_type'] = table_type
            df.columns = df.columns.str.replace('\n', '').str.replace('- ', '-')
            df.columns = df.columns.str.lower()
            dfs.append(df)

            if family_table_count >= 2:
                break

    return dfs


def string_to_datetime(date_str: str, bulletin_date: datetime) -> Union[None, datetime]:
    if date_str == 'C':
        return bulletin_date
    elif date_str == 'U':
        return None
    elif pd.isna(date_str):
        return None

    try:
        return datetime.strptime(date_str, '%d%b%y')
    except ValueError:
        return None


def classify_status(date_str) -> str:
    """Annotate the published cell as its visa-bulletin regime:
        'F'  a specific final-action / dates-for-filing date is published
        'C'  Current  -- no backlog this month
        'U'  Unavailable -- no numbers available this month
        'UNK' empty or unparseable cell

    Preserves the regime annotation that is otherwise lost when 'C' is mapped
    to the bulletin date and 'U' to NaN. Per the VisaPredict AI v5.1
    formulation, only rows with status 'F' are a prediction target; 'C'/'U'
    are kept as descriptive annotation.
    """
    if pd.isna(date_str):
        return 'UNK'
    s = str(date_str).strip().upper()
    if s == '':
        return 'UNK'
    if s == 'C':
        return 'C'
    if s == 'U':
        return 'U'
    try:
        datetime.strptime(str(date_str).strip(), '%d%b%y')
        return 'F'
    except ValueError:
        return 'UNK'


def _norm_label(s) -> str:
    """Collapse whitespace noise (\\n, \\xa0, runs of spaces) and lowercase."""
    if pd.isna(s):
        return ''
    return re.sub(r'\s+', ' ', str(s).replace('\xa0', ' ')).strip().lower()


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
        # Preserve the raw published cell and its C/F/U/NA regime BEFORE the
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

    countries = ['india', 'china', 'mexico', 'philippines', 'row']
    for country in tqdm(countries, desc=f"Extracting data for each country and computing backlogs"):
        country_df = extract_country_data(country, all_data)
        country_df = country_df.sort_values(by='visa_bulletin_date', ascending=False)
        country_df.to_csv(f'data/{country}_family_visa_backlog_timecourse.csv', index=False)


if __name__ == "__main__":
    main()
