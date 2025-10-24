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

def make_eml(title, description, user=None, eml_extra: dict | None = None):
    """Render an EML document populated with available metadata and prune empty elements.

    Args:
        title: Dataset title
        description: Dataset description (abstract)
        user: Primary user (creator/metadataProvider)
        eml_extra: Optional dict from `dataset.eml` with keys like
                   geographic_scope, temporal_scope, taxonomic_scope, methodology, users
    """
    tree = ET.parse('api/templates/eml.xml')
    root = tree.getroot()

    # EML 2.2.0: the root is namespaced (eml:eml) but children are unqualified.
    # Work with unqualified child elements throughout.

    def find(elem: ET.Element, path: str):
        return elem.find(path)

    def findall(elem: ET.Element, path: str):
        return elem.findall(path)

    def get_or_create(parent: ET.Element, tag: str) -> ET.Element:
        child = parent.find(tag)
        if child is None:
            child = ET.SubElement(parent, tag)
        return child

    def set_text(elem, text: str | None):
        if elem is not None and text not in (None, ''):
            elem.text = str(text)

    dataset_node = find(root, 'dataset')
    if dataset_node is None:
        dataset_node = ET.SubElement(root, 'dataset')

    # Title
    set_text(get_or_create(dataset_node, 'title'), title)

    # Language (ISO 639-3 per IPT EML 2.2.0 examples)
    set_text(get_or_create(dataset_node, 'language'), 'eng')

    # Publication date
    set_text(get_or_create(dataset_node, 'pubDate'), datetime.now().date().isoformat())

    # Abstract
    abstract = get_or_create(dataset_node, 'abstract')
    set_text(get_or_create(abstract, 'para'), description)

    # Helper to set a person into creator/metadataProvider/personnel
    def set_person(parent_node, person: dict, include_role: bool = False, role_value: str | None = None):
        individual = get_or_create(parent_node, 'individualName')
        set_text(get_or_create(individual, 'givenName'), person.get('first_name') or person.get('givenName') or '')
        set_text(get_or_create(individual, 'surName'), person.get('last_name') or person.get('surName') or '')
        user_id = get_or_create(parent_node, 'userId')
        user_id.set('directory', 'https://orcid.org/')
        set_text(user_id, person.get('orcid') or person.get('userId') or '')
        if include_role:
            set_text(get_or_create(parent_node, 'role'), role_value or 'metadataProvider')

    # Primary user as creator and metadataProvider
    primary_person = {
        'first_name': getattr(user, 'first_name', 'Unknown') if user else 'Test',
        'last_name': getattr(user, 'last_name', 'Unknown') if user else 'User',
        'orcid': getattr(user, 'orcid_id', '0000-0000-0000-0000') if user else '0000-0002-1825-0097',
    }

    creator_node = get_or_create(dataset_node, 'creator')
    set_person(creator_node, primary_person)

    metadata_provider_node = get_or_create(dataset_node, 'metadataProvider')
    set_person(metadata_provider_node, primary_person)

    # Optional additional metadata
    eml_extra = eml_extra or {}

    # Users array: add additional creators and project personnel
    users_list = eml_extra.get('users') or []
    # Append additional creators for any extra users beyond the primary
    for idx, person in enumerate(users_list):
        # If this user is effectively the same as primary, skip duplicating as extra
        is_primary_like = (
            (person.get('first_name') or '') == primary_person.get('first_name') and
            (person.get('last_name') or '') == primary_person.get('last_name')
        )
        # Always include in project personnel; add as extra creator only for non-primary users
        if not is_primary_like:
            extra_creator = ET.SubElement(dataset_node, 'creator')
            set_person(extra_creator, person)

    # Project personnel
    project_node = find(dataset_node, 'project')
    if project_node is None:
        project_node = ET.SubElement(dataset_node, 'project')
    for person in users_list:
        personnel = ET.SubElement(project_node, 'personnel')
        set_person(personnel, person, include_role=True, role_value='metadataProvider')

    # Coverage
    coverage = get_or_create(dataset_node, 'coverage')
    # Geographic
    geo = get_or_create(coverage, 'geographicCoverage')
    set_text(get_or_create(geo, 'geographicDescription'), eml_extra.get('geographic_scope'))
    # Temporal
    temporal_value = eml_extra.get('temporal_scope')
    if temporal_value:
        # Try to detect range vs single date
        temporal_value_str = str(temporal_value).strip()
        if any(sep in temporal_value_str for sep in ['/', '-', '–', ' to ']):
            # Split on first common separators
            for sep in ['/', ' to ', '-', '–']:
                if sep in temporal_value_str:
                    start, end = [s.strip() for s in temporal_value_str.split(sep, 1)]
                    break
            range_node = get_or_create(coverage, 'temporalCoverage')
            rnd = get_or_create(range_node, 'rangeOfDates')
            set_text(get_or_create(get_or_create(rnd, 'beginDate'), 'calendarDate'), start)
            set_text(get_or_create(get_or_create(rnd, 'endDate'), 'calendarDate'), end)
        else:
            single_node = get_or_create(coverage, 'temporalCoverage')
            sdt = get_or_create(single_node, 'singleDateTime')
            set_text(get_or_create(sdt, 'calendarDate'), temporal_value_str)

    # Taxonomic
    tax = get_or_create(coverage, 'taxonomicCoverage')
    set_text(get_or_create(tax, 'generalTaxonomicCoverage'), eml_extra.get('taxonomic_scope'))

    # Methods / methodology
    methods = get_or_create(dataset_node, 'methods')
    method_step = get_or_create(methods, 'methodStep')
    description_node = get_or_create(method_step, 'description')
    set_text(get_or_create(description_node, 'para'), eml_extra.get('methodology'))

    # Prune empty elements except root and dataset and intellectualRights
    def is_empty(element: ET.Element) -> bool:
        has_text = (element.text or '').strip() != ''
        has_children = len(element) > 0
        return not has_text and not has_children

    def prune(element: ET.Element):
        # Copy list to avoid modification during iteration
        for child in list(element):
            prune(child)
            # Preserve intellectualRights if present, even if empty (it has license text in template anyway)
            # Preserve intellectualRights if present
            if is_empty(child) and child.tag not in {'intellectualRights'}:
                element.remove(child)

    prune(root)

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

def upload_dwca(df_core, title, description, df_extension=None, user=None, eml_extra: dict | None = None):
    archive = Archive()
    archive.eml_text = make_eml(title, description, user, eml_extra)

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
