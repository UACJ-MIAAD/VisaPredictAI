from typing import List, Union
import requests
import re
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

def get_soup(url: str) -> BeautifulSoup:
    response = requests.get(url)
    return BeautifulSoup(response.text, 'html.parser')

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
        if any("family-sponsored" in row.get_text(strip=True).lower() for row in rows):
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


def extract_country_data(country: str, all_data: List[pd.DataFrame]) -> pd.DataFrame:
        country_data = []
        for df in all_data:
            df.columns = [column.replace(u'\xa0', u' ') for column in df.columns]

            search_country = country
            if country == 'row':
                search_country = 'all chargeability  areas except those listed'

            if any([search_country in col for col in df.columns]):
                col_idx = [i for i, col in enumerate(df.columns) if search_country in col][0]
                country_col = df.columns[col_idx]
                try:
                    df_subset = df[['family-sponsored', country_col, 'visa_bulletin_date', 'table_type']]
                    df_subset = df_subset.copy()
                    df_subset.columns = df_subset.columns.str.replace(country_col, 'final_action_dates')
                    df_subset.columns = df_subset.columns.str.replace('family-sponsored', 'F_level')
                    country_data.append(df_subset)
                except:
                    pass

        if not country_data:
            return pd.DataFrame(columns=['F_level', 'final_action_dates', 'visa_bulletin_date', 'visa_wait_time', 'table_type'])

        country_df = pd.concat(country_data, axis=0, ignore_index=True)

        country_df = country_df[country_df['visa_bulletin_date'].notna()]
        country_df['final_action_dates'] = country_df.apply(lambda row: string_to_datetime(row['final_action_dates'], row['visa_bulletin_date']), axis=1)
        country_df['visa_wait_time'] = country_df.apply(
            lambda row: (row['visa_bulletin_date'] - row['final_action_dates']).days / 365.25
            if pd.notna(row['final_action_dates']) and pd.notna(row['visa_bulletin_date']) else None, axis=1)

        # Clean F_level values: "1st" -> "1", "2nd-A" -> "2A", "2nd-B" -> "2B", "3rd" -> "3", "4th" -> "4"
        level_map = {}
        for val in country_df['F_level'].unique():
            cleaned = val.strip().lower()
            if cleaned in ('1st', 'f1'):
                level_map[val] = '1'
            elif cleaned in ('2a', '2nd-a', 'f2a', '2nda'):
                level_map[val] = '2A'
            elif cleaned in ('2b', '2nd-b', 'f2b', '2ndb'):
                level_map[val] = '2B'
            elif cleaned in ('3rd', 'f3'):
                level_map[val] = '3'
            elif cleaned in ('4th', 'f4'):
                level_map[val] = '4'

        country_df['F_level'] = country_df['F_level'].map(level_map)

        valid_levels = ['1', '2A', '2B', '3', '4']
        country_df = country_df[country_df['F_level'].isin(valid_levels)]

        return country_df

def main():
    month_links = extract_month_links()

    all_data = []
    for i, link in tqdm(enumerate(month_links), total=len(month_links),
                        desc="Extracting all family-sponsored visa bulletin tables"):
        try:
            table_data = extract_tables(link)
            all_data.extend(table_data)
        except Exception:
            pass

    countries = ['india', 'china', 'mexico', 'philippines', 'row']
    for country in tqdm(countries, desc=f"Extracting data for each country and computing backlogs"):
        country_df = extract_country_data(country, all_data)
        country_df = country_df.sort_values(by='visa_bulletin_date', ascending=False)
        country_df.to_csv(f'data/{country}_family_visa_backlog_timecourse.csv', index=False)


if __name__ == "__main__":
    main()
