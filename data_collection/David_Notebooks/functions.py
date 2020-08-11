from sentinelsat import SentinelAPI, read_geojson, geojson_to_wkt
from datetime import date
from env_vars import sentinel_username,sentinel_password
import glob
import pandas as pd
import subprocess


def get_api():
    
    return SentinelAPI(sentinel_username, sentinel_password, "https://scihub.copernicus.eu/apihub/")


def get_products_df(api, footprint, date_start, date_end,
                 area='IsWithin', raw='1C',
                 platform='Sentinel-2', cloudcover=(1,5)):
    
    products = api.query(footprint,
                         date=(date_start, date_end),
                         area_relation=area,
                         raw=raw,
                         platformname=platform,
                         cloudcoverpercentage=cloudcover)
    
    return api.to_dataframe(products)


def get_products_df_for_year(api, footprint, year, cloudcover):
    months_dict = {
        1: 31,
        2: 28,
        3: 31,
        4: 30,
        5: 31,
        6: 30,
        7: 31,
        8: 31,
        9: 30,
        10: 31,
        11: 30,
        12: 31
    }
    
    month_start = 1
    month_end = 13
    if year < 2015 or year > 2020:
        return None
    elif year == 2015:
        month_start = 7
    elif year == 2020:
        months_dict[2] = 29
        month_end = 8

    products_df = pd.DataFrame()
    for month in range(month_start, month_end):
        print('getting month', month)
        date_start = date(year, month, 1)
        date_end = date(year, month, months_dict[month])
        products_df_2 = get_products_df(api, footprint, date_start, date_end, cloudcover=cloudcover)
        products_df = pd.concat([products_df, products_df_2])
        products_df_3 = get_products_df(api, footprint, date_start, date_end, area='Intersects', cloudcover=cloudcover)
        products_df = pd.concat([products_df, products_df_2])
        print('Products so far:', len(products_df))
        
    return products_df


def find_ee_index_matches(api, products_df, ee_index):
    
    if len(products_df) == 0:
        print('No search results')
        return []
   
    products_df = products_df.reset_index()
    products_df = products_df.rename(columns={'index': 'sentinel_id'})
    products_df = products_df.drop_duplicates(subset=['tileid'])
    
    ee_index_2 = ee_index.reset_index()
    merged = products_df.merge(ee_index_2, left_on='title', right_on='PRODUCT_ID')
    rows = merged['index'].tolist()
    
    print(len(rows), 'total rows')
            
    return rows


def generate_tci_uri(ee_index, row):
    uri = ee_index.loc[row, 'BASE_URL']
    uri += '/GRANULE/'
    granule_id = ee_index.loc[row, 'GRANULE_ID']
    uri += granule_id
    uri += '/IMG_DATA/'
    tile_id = granule_id.split('_')[1]
    date = ee_index.loc[row, 'DATATAKE_IDENTIFIER'].split('_')[1]
    uri += f'{tile_id}_{date}_TCI.jp2'
    
    return uri


def download_tcis(ee_index, rows, dest_folder):
    
    cloud_env = r"C:\Users\David\AppData\Local\Google\Cloud SDK\cloud_env.bat"
    
    for row in rows:
        uri = generate_tci_uri(ee_index, row)
        subprocess.run([cloud_env, '&&', 'gsutil', 'cp', uri, dest_folder])
        
        
def get_tcis_for_year(year, footprint, ee_index, dest_folder, cloudcover=(1,5)):
    
    api = get_api()
    
    print('getting products')
    if year < 2015:
        print('ERROR: Year must be 2014 or above')
        return None
    else:
        products_df = get_products_df_for_year(api, footprint, year, cloudcover)
    print('finding rows')
    rows = find_ee_index_matches(api, products_df, ee_index)
    print('downloading tcis')
    download_tcis(ee_index, rows, dest_folder)
