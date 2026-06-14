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
    # Regular expression to match "visa-bulletin-for-month-year.html"
    pattern = r'visa-bulletin-for-(\w+)-(\d{4})\.html$'
    
    match = re.search(pattern, link)

    if not match:
        return None

    # Extract month and year from the matched groups
    month_str, year = match.groups()

    # Map month string to its corresponding number
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

    # Create a datetime object
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

    # Step 1: Find each main accordion container
    accordion_sections = soup.find_all('div', class_='accordion parbase section')

    for section in accordion_sections:
        # Step 2: Locate the div containing the links
        link_container = section.find('div', class_='tsg-rwd-accordion-copy')

        if link_container:
            # Step 3: Extract all the <a> tags
            links = link_container.find_all('a', href=True)
            
            for link in links:
                month_links.append(link['href'])

    return month_links


def extract_tables(link: str) -> List[pd.DataFrame]:
    year_month = extract_datetime_from_link(link)
    soup = get_soup('https://travel.state.gov/' + link)

    # Find all table elements
    tables = soup.find_all('table')

    dfs = []  # List to hold DataFrames
    employment_table_count = 0  # 1st employment table = FAD, 2nd = DFF

    for table in tables:
        rows = table.find_all('tr')
        # Detect the employment section tolerating spacing drift in the header
        # ('employment-based', 'employment- based', 'employment based').
        if any(re.search(r'employment[\s-]*based', row.get_text(strip=True).lower())
               for row in rows):
            # On a bulletin page the employment section lists Final Action Dates
            # first and Dates for Filing second (DFF tables exist only from
            # Oct 2015 on; earlier months have a single FAD table).
            employment_table_count += 1
            table_type = "final_action" if employment_table_count == 1 else "dates_for_filing"

            table_data = []
            for row in rows:
                th_cols = row.find_all('th')
                td_cols = row.find_all('td')

                # Combine the th and td columns, th first
                all_cols = th_cols + td_cols

                # Extract text from each column
                cols = [ele.text.strip() for ele in all_cols]
                table_data.append(cols)

            # If the first row only has one column, it is a spanning header, remove it
            if len(table_data[0]) == 1:
                columns = table_data[1]
                table_body = table_data[2:]
            else:
                columns = table_data[0]
                table_body = table_data[1:]

            # Convert the table_data into a DataFrame, treating the first row as headers
            df = pd.DataFrame(table_body, columns=columns)
            df['visa_bulletin_date'] = year_month  # Add a column for the year_month
            df['table_type'] = table_type
            df.columns = df.columns.str.replace('\n', '').str.replace('- ', '-')
            df.columns = df.columns.str.lower()
            dfs.append(df)  # Append the DataFrame to the list

            if employment_table_count >= 2:
                break  # FAD + DFF captured

    return dfs


def string_to_datetime(date_str: str, bulletin_date: datetime) -> Union[None, datetime]:
    # Handle special cases
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
        'F'  a specific final-action date is published (a parseable date)
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
        # Preserve the raw published cell and its C/F/U/NA regime BEFORE the
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

    countries = ['india', 'china', 'mexico', 'philippines', 'row']
    for country in tqdm(countries, desc=f"Extracting data for each country and computing backlogs"):
        country_df = extract_country_data(country, all_data)
        country_df = country_df.sort_values(by='visa_bulletin_date', ascending=False)
        country_df.to_csv(f'data/{country}_visa_backlog_timecourse.csv', index=False)


if __name__ == "__main__":
    main()
