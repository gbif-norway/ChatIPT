import xml.etree.ElementTree as ET
from dwcawriter import Archive, Table
import tempfile
from datetime import datetime
import os
from minio import Minio
from tenacity import retry, stop_after_attempt, wait_fixed
import requests
from requests.auth import HTTPBasicAuth
import uuid

def make_eml(title, description):
    tree = ET.parse('api/templates/eml.xml')
    root = tree.getroot()
    placeholder_user = {'givenName': 'Test', 'surName': 'User', 'userId': '0000-0002-1825-0097'}
    values = {
        'title': title,
        'creator': placeholder_user, 
        'metadataProvider': placeholder_user,
        'abstract' : { 'para': description }
    }

    def fill_node(node, values):
        for key, value in values.items():
            child = node.find(f'.//{key}')
            if child is not None:
                if isinstance(value, dict):
                    fill_node(child, value)
                else:
                    child.text = value
    fill_node(root, values)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8')

@retry(stop=stop_after_attempt(10), wait=wait_fixed(2))
def upload_file(client, bucket_name, object_name, local_path):
    client.fput_object(bucket_name, object_name, local_path, content_type="application/zip")

def get_id_col_index(df, col_name='occurrenceID'):
    # Ensure all column names are treated as strings before lowercase comparison
    columns_lower = [str(col).lower() for col in df.columns]

    if col_name.lower() in columns_lower:
        ind = columns_lower.index(col_name.lower())
        return ind
    elif 'id' in columns_lower:
        ind = columns_lower.index('id')
        return ind
    else:
        # No ID col exists, create with random UUIDs
        df[col_name] = [str(uuid.uuid4()) for _ in range(len(df))]
        # Update columns_lower to include the new column
        return df.columns.get_loc(col_name)

def upload_dwca(df_core, title, description, df_extension=None):
    archive = Archive()
    archive.eml_text = make_eml(title, description)

    core_table = Table(spec='https://rs.gbif.org/core/dwc_occurrence_2022-02-02.xml', data=df_core, id_index=get_id_col_index(df_core, 'occurrenceID'), only_mapped_columns=True)
    archive.core = core_table
    if df_extension is not None:
        extension_table = Table(spec='https://rs.gbif.org/extension/dwc/measurements_or_facts_2022-02-02.xml', data=df_extension, id_index=get_id_col_index(df_extension, 'measurementID'))
        archive.extensions.append(extension_table)

    file_name = datetime.now().strftime('output-%Y-%m-%d-%H%M%S') + '.zip'
    with tempfile.TemporaryDirectory() as temp_dir:
        local_path = os.path.join(temp_dir, file_name)
        archive.export(local_path)
        client = Minio(os.getenv('MINIO_URI'), access_key=os.getenv('MINIO_ACCESS_KEY'), secret_key=os.getenv('MINIO_SECRET_KEY'))
        upload_file(client, os.getenv('MINIO_BUCKET'), f"{os.getenv('MINIO_BUCKET_FOLDER')}/{file_name}", local_path)
        return f"https://{os.getenv('MINIO_URI')}/{os.getenv('MINIO_BUCKET')}/{os.getenv('MINIO_BUCKET_FOLDER')}/{file_name}"

def register_dataset_and_endpoint(title, description, url):
    print('registering dataset')
    payload = {
        'title': title,
        'description': description,
        'publishingOrganizationKey': os.getenv('GBIF_PUBLISHING_ORGANIZATION_KEY'),
        'installationKey': os.getenv('GBIF_INSTALLATION_KEY'),
        'language': 'en',
        'type': 'OCCURRENCE'
    }
    response = requests.post(f"{os.getenv('GBIF_API_URL')}/dataset", json=payload, headers={'Content-Type': 'application/json'}, auth=HTTPBasicAuth(os.getenv('GBIF_USER'), os.getenv('GBIF_PASSWORD')))
    if response.status_code == 201:
        dataset_key = response.json()
    else:
        raise requests.exceptions.HTTPError(f'Failed to add dataset. Status code: {response.status_code}, Response JSON: {response.json()}')

    print(dataset_key)
    register_endpoint(dataset_key, url)
    return f'https://gbif-uat.org/dataset/{dataset_key}'


def register_endpoint(dataset_key, url):
    payload = { 'type': 'DWC_ARCHIVE', 'url': url, 'machineTags': [] }
    response = requests.post(f"{os.getenv('GBIF_API_URL')}/dataset/{dataset_key}/endpoint", json=payload, headers={'Content-Type': 'application/json'}, auth=HTTPBasicAuth(os.getenv('GBIF_USER'), os.getenv('GBIF_PASSWORD')))
    if response.status_code != 201:
        raise requests.exceptions.HTTPError(f'Failed to add endpoint. Status code: {response.status_code}, Response JSON: {response.json()}')
