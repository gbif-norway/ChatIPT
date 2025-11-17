import xml.etree.ElementTree as ET
import tempfile
from datetime import datetime
import os
from pathlib import Path
import traceback
from minio import Minio
from tenacity import retry, stop_after_attempt, wait_fixed
import requests
from requests.auth import HTTPBasicAuth
import uuid
import xmltodict
import re
import json
import zipfile
import pandas as pd
from dwcawriter import Archive
from dwcawriter.table import Table as DwcaWriterTable

from api.dwc_specs import (
    CORE_SCHEMAS,
    EXTENSION_SCHEMAS,
    DarwinCoreCoreType,
    DarwinCoreExtensionType,
)
from api.helpers import discord_bot

# Get the base directory for templates
_BASE_DIR = Path(__file__).resolve().parent.parent
_TEMPLATES_ROOT = _BASE_DIR / "templates"


class LocalSpecTable(DwcaWriterTable):
    """Table implementation that supports both vendored local spec files and GBIF URLs."""

    def update_spec(self):
        spec = self.spec
        if isinstance(spec, str) and spec.startswith(("http://", "https://")):
            raise ValueError(
                "Remote schema URLs are not supported. "
                "Download the specification and reference the local file instead."
            )

        if not spec:
            raise ValueError("Specification path is required for LocalSpecTable.")

        spec_path = Path(spec)
        if not spec_path.exists():
            raise FileNotFoundError(f"Specification file not found at {spec_path}")

        with spec_path.open("r", encoding="utf-8") as spec_file:
            spec_json = xmltodict.parse(spec_file.read())

        extension = spec_json.get("extension")
        if not extension:
            raise ValueError(f"Specification {spec_path} does not contain an 'extension' root element.")

        row_type = extension.get("@rowType")
        if not row_type:
            raise ValueError(f"Specification {spec_path} is missing a '@rowType' attribute.")
        self.row_type = row_type

        properties = extension.get("property")
        if properties is None:
            self.dwc_fields = {}
            return

        if isinstance(properties, dict):
            properties = [properties]

        field_map = {}
        for prop in properties:
            name = prop.get("@name")
            qual_name = prop.get("@qualName")
            if not name or not qual_name:
                # Skip malformed entries but keep processing others.
                continue
            field_map[name] = qual_name

        self.dwc_fields = field_map

def make_eml(title, description, user=None, eml_extra: dict | None = None):
    """Render an EML document populated with available metadata and prune empty elements.

    Args:
        title: Dataset title
        description: Dataset description (abstract)
        user: Primary user (creator/metadataProvider)
        eml_extra: Optional dict from `dataset.eml` with keys like
                   geographic_scope, temporal_scope, taxonomic_scope, methodology, users
    """
    eml_path = _TEMPLATES_ROOT / "eml.xml"
    if not eml_path.exists():
        raise FileNotFoundError(f"EML template not found at: {eml_path}")
    tree = ET.parse(str(eml_path))
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
        # Email if available
        email_value = person.get('email') or person.get('electronicMailAddress') or ''
        if email_value:
            set_text(get_or_create(parent_node, 'electronicMailAddress'), email_value)
        if include_role:
            set_text(get_or_create(parent_node, 'role'), role_value or 'metadataProvider')

    # Primary user as creator and metadataProvider
    primary_person = {
        'first_name': getattr(user, 'first_name', 'Unknown') if user else 'Test',
        'last_name': getattr(user, 'last_name', 'Unknown') if user else 'User',
        'orcid': getattr(user, 'orcid_id', '0000-0000-0000-0000') if user else '0000-0002-1825-0097',
        'email': getattr(user, 'email', '') if user else '',
    }

    creator_node = get_or_create(dataset_node, 'creator')
    set_person(creator_node, primary_person)

    metadata_provider_node = get_or_create(dataset_node, 'metadataProvider')
    set_person(metadata_provider_node, primary_person)

    # Contact (required by IPT): copy primary user
    contact_node = get_or_create(dataset_node, 'contact')
    set_person(contact_node, primary_person)

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

def ensure_identifier_column(df, target_name: str) -> int:
    """
    Ensure the given DataFrame has a column named ``target_name`` (case-insensitive) and
    return its positional index. If the column is absent, reuse a generic ``id`` column
    when available or mint fresh UUID4 values.
    """
    target_lower = target_name.lower()
    columns_lower = [str(col).lower() for col in df.columns]

    if target_lower in columns_lower:
        return columns_lower.index(target_lower)

    if "id" in columns_lower:
        return columns_lower.index("id")

    df[target_name] = [str(uuid.uuid4()) for _ in range(len(df))]
    return df.columns.get_loc(target_name)


def parse_newick_tip_labels(content: str) -> list[str]:
    """
    Extract tip labels from a Newick format tree string.
    
    Args:
        content: Content of a Newick file
        
    Returns:
        List of tip labels found in the tree
    """
    tip_labels = []
    # Newick format: labels can be inside parentheses or at the end
    # Pattern to match labels: word characters, underscores, hyphens, dots
    # Labels are typically separated by commas and enclosed in parentheses
    # Match labels that appear before colons (branch lengths) or at the end
    # Important: Don't match pure numbers (branch lengths) - they must contain at least one letter
    # Pattern: match sequences that contain at least one letter, before : or end of token
    pattern = r'([A-Za-z][A-Za-z0-9_\-\.]*|[A-Za-z0-9_\-\.]*[A-Za-z][A-Za-z0-9_\-\.]*|[A-Za-z])(?=:|\s*[,;)]|$)'
    matches = re.findall(pattern, content)
    # Filter out empty strings, pure numbers, and very short matches
    # Also remove duplicates while preserving order
    seen = set()
    tip_labels = []
    for m in matches:
        # Skip if it's a pure number (branch length) - check if it contains at least one letter
        if m and m not in seen:
            # Must contain at least one letter (handles single letters too)
            if any(c.isalpha() for c in m):
                # Additional check: if it's all digits and dots, skip it (branch length)
                if not m.replace('.', '').replace('-', '').isdigit():
                    seen.add(m)
                    tip_labels.append(m)
    return tip_labels


def parse_nexus_tip_labels(content: str) -> list[str]:
    """
    Extract tip labels from a NEXUS format tree file.
    Handles both TRANSLATE blocks and direct tree tip labels.
    
    Args:
        content: Content of a NEXUS file
        
    Returns:
        List of tip labels found in the tree(s)
    """
    tip_labels = []
    
    # Check if there's a TRANSLATE block
    translate_match = re.search(r'TRANSLATE\s+(.*?);', content, re.DOTALL | re.IGNORECASE)
    if translate_match:
        translate_block = translate_match.group(1)
        # Parse translate entries: key value, or key value,
        # Handle both comma-separated and space-separated entries
        # Format: key value, or key value;
        translate_entries = re.findall(r'(\S+)\s+([A-Za-z0-9_\-\.]+)[,\s]*', translate_block)
        # Use the translated names (second value) as tip labels
        tip_labels = [entry[1].strip() for entry in translate_entries if entry[1].strip()]
    else:
        # No TRANSLATE block, extract labels directly from tree
        # Look for TREE blocks
        tree_match = re.search(r'TREE\s+[^=]+=\s*(.*?);', content, re.DOTALL | re.IGNORECASE)
        if tree_match:
            tree_string = tree_match.group(1)
            # Extract tip labels using newick parsing
            tip_labels = parse_newick_tip_labels(tree_string)
    
    return tip_labels


def parse_newick_to_tree(newick_string: str) -> dict:
    """
    Parse a Newick format tree string into a hierarchical JSON structure.
    
    Args:
        newick_string: Newick format tree string (e.g., "(A:0.1,B:0.2,(C:0.3,D:0.4):0.5);")
        
    Returns:
        Dictionary representing the tree with structure:
        {
            "name": None (for root),
            "branch_length": float or 0,
            "children": [...]
        }
    """
    def parse_node(token_stream, pos):
        """Recursively parse a node from the token stream."""
        node = {"name": None, "branch_length": 0, "children": []}
        
        # Check for opening parenthesis (internal node)
        if pos < len(token_stream) and token_stream[pos] == '(':
            pos += 1  # Skip '('
            
            # Parse children until we hit ')'
            while pos < len(token_stream) and token_stream[pos] != ')':
                child, pos = parse_node(token_stream, pos)
                node["children"].append(child)
                
                # Skip comma separator
                if pos < len(token_stream) and token_stream[pos] == ',':
                    pos += 1
            
            # Skip closing ')'
            if pos < len(token_stream) and token_stream[pos] == ')':
                pos += 1
            
            # Parse label and/or branch length
            label_parts = []
            while pos < len(token_stream) and token_stream[pos] not in '(),;':
                label_parts.append(token_stream[pos])
                pos += 1
            
            label_str = ''.join(label_parts).strip()
            if ':' in label_str:
                parts = label_str.split(':', 1)
                label = parts[0].strip() if parts[0].strip() else None
                try:
                    node["branch_length"] = float(parts[1].strip())
                except (ValueError, IndexError):
                    node["branch_length"] = 0
                if label:
                    node["name"] = label
            elif label_str:
                node["name"] = label_str
        else:
            # Leaf node - parse label and branch length
            label_parts = []
            while pos < len(token_stream) and token_stream[pos] not in '(),;':
                label_parts.append(token_stream[pos])
                pos += 1
            
            label_str = ''.join(label_parts).strip()
            if ':' in label_str:
                parts = label_str.split(':', 1)
                label = parts[0].strip() if parts[0].strip() else None
                try:
                    node["branch_length"] = float(parts[1].strip())
                except (ValueError, IndexError):
                    node["branch_length"] = 0
                if label:
                    node["name"] = label
            elif label_str:
                node["name"] = label_str
        
        return node, pos
    
    # Remove whitespace except what's inside quoted labels
    cleaned = newick_string.strip()
    if cleaned.endswith(';'):
        cleaned = cleaned[:-1]
    
    # Remove whitespace for easier parsing (but preserve structure)
    # This is a simple approach - for more complex cases, a proper tokenizer would be better
    cleaned = re.sub(r'\s+', '', cleaned)
    
    # Simple tokenization: split into individual characters for parsing
    # This handles the tree structure character by character
    tokens = list(cleaned)
    
    root, _ = parse_node(tokens, 0)
    return root


def parse_nexus_to_tree(nexus_content: str) -> dict:
    """
    Parse a NEXUS format tree file into a hierarchical JSON structure.
    Extracts the first tree from the file.
    
    Args:
        nexus_content: Content of a NEXUS file
        
    Returns:
        Dictionary representing the tree (same structure as parse_newick_to_tree)
    """
    # Extract tree string from NEXUS file
    tree_match = re.search(r'TREE\s+[^=]+=\s*(.*?);', nexus_content, re.DOTALL | re.IGNORECASE)
    if tree_match:
        tree_string = tree_match.group(1).strip()
        # Remove any comments or metadata
        # NEXUS files might have comments like [&R] before the tree
        tree_string = re.sub(r'\[[^\]]*\]', '', tree_string)
        return parse_newick_to_tree(tree_string)
    
    return {"name": None, "branch_length": 0, "children": []}


def match_tip_label_to_scientific_name(tip_label: str, scientific_name: str) -> bool:
    """
    Check if a tip label matches a scientific name.
    Matches are made on the scientificName, if that string (might be separated by _) 
    appears in the nexus/nwk field.
    
    Args:
        tip_label: Tip label from the tree (e.g., "Berneuxia_thibetica_01")
        scientific_name: Scientific name from occurrence (e.g., "Berneuxia thibetica")
        
    Returns:
        True if there's a match, False otherwise
    """
    if not tip_label or not scientific_name:
        return False
    
    # Normalize: convert to lowercase and replace spaces with underscores
    tip_normalized = tip_label.lower().replace(' ', '_')
    sci_normalized = scientific_name.lower().replace(' ', '_')
    
    # Check if the scientific name (with underscores) appears in the tip label
    # Remove underscores from both for comparison
    tip_clean = tip_normalized.replace('_', '')
    sci_clean = sci_normalized.replace('_', '')
    
    # Check if the scientific name appears in the tip label
    if sci_clean in tip_clean:
        return True
    
    # Also check if individual words from scientific name appear in tip label
    sci_words = sci_normalized.split('_')
    if len(sci_words) >= 2:
        # Check if genus and species appear in tip label
        genus = sci_words[0]
        species = sci_words[1] if len(sci_words) > 1 else ''
        if genus in tip_normalized and species in tip_normalized:
            return True
    
    return False


def update_occurrence_dynamic_properties(
    df: pd.DataFrame,
    tree_files: list[tuple[str, list[str]]]
) -> pd.DataFrame:
    """
    Update the dynamicProperties column in the occurrence DataFrame with phylogeny information.
    
    Args:
        df: Occurrence DataFrame (must have 'scientificName' column)
        tree_files: List of tuples (filename, tip_labels) for each tree file
        
    Returns:
        Updated DataFrame with dynamicProperties populated
    """
    # Ensure dynamicProperties column exists
    if 'dynamicProperties' not in df.columns:
        df['dynamicProperties'] = ''
    
    # Ensure scientificName column exists
    if 'scientificName' not in df.columns:
        return df
    
    # Process each row
    for idx, row in df.iterrows():
        scientific_name = str(row.get('scientificName', '')).strip()
        if not scientific_name or scientific_name == 'nan':
            continue
        
        # Get existing dynamicProperties if any
        existing_dp = row.get('dynamicProperties', '')
        existing_json = {}
        
        if existing_dp and existing_dp.strip():
            try:
                existing_json = json.loads(existing_dp)
            except (json.JSONDecodeError, ValueError):
                # If parsing fails, start fresh
                existing_json = {}
        
        # Ensure phylogenies list exists
        if 'phylogenies' not in existing_json:
            existing_json['phylogenies'] = []
        
        # Find matches for this scientific name across all tree files
        for filename, tip_labels in tree_files:
            for tip_label in tip_labels:
                if match_tip_label_to_scientific_name(tip_label, scientific_name):
                    # Check if this phylogeny entry already exists
                    existing_entry = None
                    for entry in existing_json['phylogenies']:
                        if (entry.get('phyloTreeTipLabel') == tip_label and 
                            entry.get('phyloTreeFileName') == filename):
                            existing_entry = entry
                            break
                    
                    # Add new entry if it doesn't exist
                    if existing_entry is None:
                        existing_json['phylogenies'].append({
                            'phyloTreeTipLabel': tip_label,
                            'phyloTreeFileName': filename
                        })
        
        # Update the dynamicProperties column
        if existing_json.get('phylogenies'):
            df.at[idx, 'dynamicProperties'] = json.dumps(existing_json)
        else:
            # If no phylogenies found, preserve existing dynamicProperties if it had other data
            if existing_dp and existing_dp.strip() and existing_json:
                # Remove phylogenies key if it's empty but keep other properties
                existing_json.pop('phylogenies', None)
                if existing_json:
                    df.at[idx, 'dynamicProperties'] = json.dumps(existing_json)
                else:
                    df.at[idx, 'dynamicProperties'] = existing_dp
    
    return df


def upload_dwca(
    df_core,
    title,
    description,
    core_type: DarwinCoreCoreType,
    extensions: list[tuple[object, DarwinCoreExtensionType]] | None = None,
    user=None,
    eml_extra: dict | None = None,
    additional_files: list[tuple[str, bytes]] | None = None,
):
    try:
        archive = Archive()
    except Exception as e:
        error_msg = f"🚨 UploadDwCA Error - Failed to create Archive:\n{str(e)}\n\nTraceback:\n{traceback.format_exc()}"
        discord_bot.send_discord_message(error_msg)
        raise RuntimeError(f"Failed to create Archive: {e}") from e
    
    try:
        archive.eml_text = make_eml(title, description, user, eml_extra)
    except Exception as e:
        error_msg = f"🚨 UploadDwCA Error - Failed to generate EML:\n{str(e)}\n\nTraceback:\n{traceback.format_exc()}\n\nTemplates root: {_TEMPLATES_ROOT}\nCurrent working directory: {os.getcwd()}"
        discord_bot.send_discord_message(error_msg)
        raise RuntimeError(f"Failed to generate EML: {e}") from e

    core_schema = CORE_SCHEMAS[core_type]
    core_id_index = None
    if core_schema.id_column:
        core_id_index = ensure_identifier_column(df_core, core_schema.id_column)

    # Validate spec_path exists if it's a local file (not a URL)
    core_spec_path = core_schema.spec_path
    if not core_spec_path.startswith(('http://', 'https://')):
        spec_file = Path(core_spec_path)
        if not spec_file.exists():
            error_msg = (
                f"🚨 UploadDwCA Error - Core schema file not found:\n"
                f"Path: {core_spec_path}\n"
                f"Expected location: {spec_file}\n"
                f"Templates root: {_TEMPLATES_ROOT}\n"
                f"Current working directory: {os.getcwd()}\n"
                f"Core type: {core_type}"
            )
            discord_bot.send_discord_message(error_msg)
            raise FileNotFoundError(
                f"Core schema file not found: {core_spec_path}\n"
                f"Expected location: {spec_file}\n"
                f"Templates root: {_TEMPLATES_ROOT}\n"
                f"Current working directory: {os.getcwd()}"
            )

    core_kwargs = {
        "spec": core_spec_path,
        "data": df_core,
        "only_mapped_columns": True,
    }
    if core_id_index is not None:
        core_kwargs["id_index"] = core_id_index
    
    try:
        core_table = LocalSpecTable(**core_kwargs)
    except FileNotFoundError as e:
        error_msg = (
            f"🚨 UploadDwCA Error - Failed to create core Table:\n"
            f"Spec path: {core_spec_path}\n"
            f"Error: {str(e)}\n"
            f"Templates root: {_TEMPLATES_ROOT}\n"
            f"Current working directory: {os.getcwd()}\n\n"
            f"Traceback:\n{traceback.format_exc()}"
        )
        discord_bot.send_discord_message(error_msg)
        raise FileNotFoundError(
            f"Failed to create core Table with spec: {core_spec_path}\n"
            f"Error: {e}\n"
            f"Templates root: {_TEMPLATES_ROOT}\n"
            f"Current working directory: {os.getcwd()}"
        ) from e
    except Exception as e:
        error_msg = (
            f"🚨 UploadDwCA Error - Failed to create core Table (non-FileNotFound):\n"
            f"Spec path: {core_spec_path}\n"
            f"Error: {str(e)}\n"
            f"Error type: {type(e).__name__}\n\n"
            f"Traceback:\n{traceback.format_exc()}"
        )
        discord_bot.send_discord_message(error_msg)
        raise RuntimeError(f"Failed to create core Table: {e}") from e
    
    archive.core = core_table

    for ext_df, ext_type in extensions or []:
        schema = EXTENSION_SCHEMAS[ext_type]
        ext_spec_path = schema.spec_path
        
        # Validate spec_path exists if it's a local file (not a URL)
        if not ext_spec_path.startswith(('http://', 'https://')):
            spec_file = Path(ext_spec_path)
            if not spec_file.exists():
                error_msg = (
                    f"🚨 UploadDwCA Error - Extension schema file not found:\n"
                    f"Extension type: {ext_type}\n"
                    f"Path: {ext_spec_path}\n"
                    f"Expected location: {spec_file}\n"
                    f"Templates root: {_TEMPLATES_ROOT}"
                )
                discord_bot.send_discord_message(error_msg)
                raise FileNotFoundError(
                    f"Extension schema file not found: {ext_spec_path}\n"
                    f"Extension type: {ext_type}\n"
                    f"Expected location: {spec_file}\n"
                    f"Templates root: {_TEMPLATES_ROOT}"
                )
        
        ext_kwargs = {
            "spec": ext_spec_path,
            "data": ext_df,
            "only_mapped_columns": True,
        }
        if schema.id_column:
            ext_kwargs["id_index"] = ensure_identifier_column(ext_df, schema.id_column)
        try:
            archive.extensions.append(LocalSpecTable(**ext_kwargs))
        except Exception as e:
            error_msg = (
                f"🚨 UploadDwCA Error - Failed to create extension Table:\n"
                f"Extension type: {ext_type}\n"
                f"Spec path: {ext_spec_path}\n"
                f"Error: {str(e)}\n\n"
                f"Traceback:\n{traceback.format_exc()}"
            )
            discord_bot.send_discord_message(error_msg)
            raise

    file_name = datetime.now().strftime('output-%Y-%m-%d-%H%M%S') + '.zip'
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = os.path.join(temp_dir, file_name)
            archive.export(local_path)
            
            # Add additional files (e.g., tree files) to the archive
            if additional_files:
                with zipfile.ZipFile(local_path, 'a', zipfile.ZIP_DEFLATED) as zipf:
                    for filename, file_content in additional_files:
                        zipf.writestr(filename, file_content)
            
            client = Minio(os.getenv('MINIO_URI'), access_key=os.getenv('MINIO_ACCESS_KEY'), secret_key=os.getenv('MINIO_SECRET_KEY'))
            upload_file(client, os.getenv('MINIO_BUCKET'), f"{os.getenv('MINIO_BUCKET_FOLDER')}/{file_name}", local_path)
            return f"https://{os.getenv('MINIO_URI')}/{os.getenv('MINIO_BUCKET')}/{os.getenv('MINIO_BUCKET_FOLDER')}/{file_name}"
    except Exception as e:
        error_msg = (
            f"🚨 UploadDwCA Error - Failed during archive export/upload:\n"
            f"Error: {str(e)}\n"
            f"Error type: {type(e).__name__}\n\n"
            f"Traceback:\n{traceback.format_exc()}"
        )
        discord_bot.send_discord_message(error_msg)
        raise

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
