import calendar
import csv
import io
import xml.etree.ElementTree as ET
import tempfile
from datetime import date, datetime, timezone
import os
from pathlib import Path, PurePosixPath
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
import dendropy
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
from api.publication_validation import utf8_serialization_errors

# Get the base directory for templates
_BASE_DIR = Path(__file__).resolve().parent.parent
_TEMPLATES_ROOT = _BASE_DIR / "templates"

GBIF_LICENSES = {
    "CC0 1.0": {
        "url": "http://creativecommons.org/publicdomain/zero/1.0/legalcode",
        "title": "Creative Commons CC0 1.0 Universal Public Domain Dedication",
    },
    "CC BY 4.0": {
        "url": "http://creativecommons.org/licenses/by/4.0/legalcode",
        "title": "Creative Commons Attribution (CC BY) 4.0 License",
    },
    "CC BY-NC 4.0": {
        "url": "http://creativecommons.org/licenses/by-nc/4.0/legalcode",
        "title": "Creative Commons Attribution-NonCommercial (CC BY-NC) 4.0 License",
    },
}
DEFAULT_GBIF_LICENSE = "CC BY 4.0"

GBIF_DATASET_TYPES = {
    DarwinCoreCoreType.OCCURRENCE: "OCCURRENCE",
    DarwinCoreCoreType.EVENT: "SAMPLING_EVENT",
    DarwinCoreCoreType.TAXON: "CHECKLIST",
}


def clean_text(value) -> str | None:
    """Return meaningful scalar text, excluding common serialized missing values."""
    if value is None:
        return None
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    text = str(value).strip()
    if text.lower() in {'nan', 'nat', '<na>'}:
        return None
    return text or None


class DwcaExtensionLinkError(ValueError):
    """Expected projection error raised when extension rows cannot link to the core."""

    def __init__(self, core_id_column: str | None, issues: list[dict]):
        self.core_id_column = core_id_column
        self.issues = issues
        summaries = []
        for issue in issues:
            details = []
            if issue.get("missing_column"):
                details.append(f"missing core ID column '{issue['requested_column']}'")
            if issue.get("blank_count"):
                details.append(f"{issue['blank_count']} blank value(s)")
            if issue.get("unresolved_count"):
                details.append(f"{issue['unresolved_count']} unresolved distinct value(s)")
            summaries.append(f"{issue['extension_type']}: {', '.join(details)}")
        super().__init__(
            "DwC-A extension link validation failed for "
            f"{len(issues)} extension table(s) against core '{core_id_column}': "
            + "; ".join(summaries)
        )


def normalize_gbif_license(value: str | None) -> tuple[str, dict]:
    """Return a supported GBIF license name and its canonical metadata."""
    name = (value or DEFAULT_GBIF_LICENSE).strip()
    if name not in GBIF_LICENSES:
        supported = ", ".join(GBIF_LICENSES)
        raise ValueError(f"Unsupported GBIF license '{name}'. Choose one of: {supported}.")
    return name, GBIF_LICENSES[name]


def gbif_dataset_type_for_core(core_type: DarwinCoreCoreType | str) -> str:
    """Return the GBIF Registry dataset class for a Darwin Core archive core."""
    try:
        normalized_core_type = DarwinCoreCoreType(core_type)
    except ValueError as exc:
        supported = ", ".join(core.value for core in GBIF_DATASET_TYPES)
        raise ValueError(
            f"Unsupported Darwin Core type '{core_type}'. Choose one of: {supported}."
        ) from exc
    return GBIF_DATASET_TYPES[normalized_core_type]


class LocalSpecTable(DwcaWriterTable):
    """Table implementation that supports both vendored local spec files and GBIF URLs."""

    def __init__(self, *args, output_filename: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.output_filename = output_filename

    def get_filename(self) -> str:
        if self.output_filename:
            return self.output_filename
        return super().get_filename()

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


def _replace_surrogateescape_chars(value: str) -> str:
    """Convert surrogateescape bytes into real Unicode so UTF-8 export cannot fail."""
    if not any(0xDC80 <= ord(char) <= 0xDCFF for char in value):
        return value

    result = []
    for char in value:
        codepoint = ord(char)
        if 0xDC80 <= codepoint <= 0xDCFF:
            byte = bytes([codepoint - 0xDC00])
            result.append(byte.decode("cp1252", errors="replace"))
        else:
            result.append(char)
    return "".join(result)


def _sanitize_dataframe_for_utf8_export(df: pd.DataFrame) -> pd.DataFrame:
    sanitized = df.copy()
    sanitized.columns = [
        _replace_surrogateescape_chars(column) if isinstance(column, str) else column
        for column in sanitized.columns
    ]

    for column in sanitized.select_dtypes(include=["object"]).columns:
        sanitized[column] = sanitized[column].map(
            lambda value: _replace_surrogateescape_chars(value)
            if isinstance(value, str)
            else value
        )
    return sanitized

def make_eml(title, description, user=None, eml_extra: dict | None = None):
    """Render an EML document populated with available metadata and prune empty elements.

    Args:
        title: Dataset title
        description: Dataset description (abstract)
        user: Primary user (creator/metadataProvider)
        eml_extra: Optional dict from `dataset.eml` with keys like
                   geographic_scope, temporal_scope, taxonomic_scope, methodology, users, project_title
    """
    eml_path = _TEMPLATES_ROOT / "eml.xml"
    if not eml_path.exists():
        raise FileNotFoundError(f"EML template not found at: {eml_path}")
    tree = ET.parse(str(eml_path))
    root = tree.getroot()
    eml_extra = eml_extra or {}

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

    def remove_children(parent: ET.Element, tag: str):
        for child in list(findall(parent, tag)):
            parent.remove(child)

    def local_tag(tag_name: str) -> str:
        if "}" in tag_name:
            return tag_name.rsplit("}", 1)[-1]
        return tag_name

    def ensure_child_order(parent: ET.Element, ordered_tags: list[str]):
        order_map = {tag: i for i, tag in enumerate(ordered_tags)}
        indexed_children = list(enumerate(list(parent)))
        indexed_children.sort(
            key=lambda item: (
                order_map.get(local_tag(item[1].tag), len(order_map)),
                item[0],
            )
        )
        parent[:] = [child for _, child in indexed_children]

    def set_text(elem, text: str | None):
        if elem is not None and text not in (None, ''):
            elem.text = str(text)

    package_id = clean_text(eml_extra.get('package_id'))
    if package_id:
        root.set('packageId', package_id)

    def normalize_orcid_identifier(value: str | None) -> str | None:
        """Normalize ORCID input to the identifier token expected in EML userId."""
        text = clean_text(value)
        if text is None:
            return None

        match = re.match(
            r"^(?:https?://)?(?:www\.)?orcid\.org/(?P<identifier>[^?#]+)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            text = (match.group("identifier") or "").strip().strip("/")

        if re.fullmatch(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}", text):
            return text.upper()
        return text

    def normalize_doi(value: str | None) -> str | None:
        if value in (None, ''):
            return None
        doi = str(value).strip()
        if not doi:
            return None
        doi = doi.replace('https://doi.org/', '').replace('http://doi.org/', '')
        doi = doi.replace('doi:', '').strip()
        return doi or None

    def _to_iso_date(value: str | None, is_end: bool = False, context: date | None = None) -> str | None:
        if value in (None, ''):
            return None

        token = str(value).strip().strip(",.;:")
        if not token:
            return None

        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(token, fmt).date().isoformat()
            except ValueError:
                pass

        for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(token, fmt).date().isoformat()
            except ValueError:
                pass

        year_month = re.fullmatch(r"(\d{4})-(\d{2})", token)
        if year_month:
            year = int(year_month.group(1))
            month = int(year_month.group(2))
            if 1 <= month <= 12:
                day = calendar.monthrange(year, month)[1] if is_end else 1
                return datetime(year, month, day).date().isoformat()

        year_only = re.fullmatch(r"\d{4}", token)
        if year_only:
            year = int(token)
            return f"{year}-12-31" if is_end else f"{year}-01-01"

        day_only = re.fullmatch(r"\d{1,2}", token)
        if day_only and context is not None:
            day = int(token)
            max_day = calendar.monthrange(context.year, context.month)[1]
            if 1 <= day <= max_day:
                return datetime(context.year, context.month, day).date().isoformat()

        return None

    def _normalize_temporal_scope(raw_value: str) -> tuple[str, str] | tuple[str, str, str] | None:
        temporal_value_str = str(raw_value or "").strip()
        if not temporal_value_str:
            return None

        iso_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", temporal_value_str)
        if len(iso_dates) >= 2:
            return ("range", iso_dates[0], iso_dates[-1])
        if len(iso_dates) == 1:
            return ("single", iso_dates[0])

        range_parts = None
        for sep in ("/", " to ", " - ", " – "):
            if sep in temporal_value_str:
                parts = [s.strip() for s in temporal_value_str.split(sep, 1)]
                if len(parts) == 2 and parts[0] and parts[1]:
                    range_parts = parts
                    break

        if range_parts is not None:
            left, right = range_parts
            right_iso = _to_iso_date(right, is_end=True)
            right_context = datetime.strptime(right_iso, "%Y-%m-%d").date() if right_iso else None
            left_iso = _to_iso_date(left, is_end=False, context=right_context)
            if left_iso and right_iso:
                return ("range", left_iso, right_iso)

        single_iso = _to_iso_date(temporal_value_str)
        if single_iso:
            return ("single", single_iso)

        return None

    def _coerce_coordinate(value) -> float | None:
        if value in (None, ''):
            return None
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _coordinate_from_match(number: str, direction: str | None = None) -> float | None:
        coordinate = _coerce_coordinate(number)
        if coordinate is None:
            return None
        if direction:
            direction = direction.upper()
            if direction in {'S', 'W'}:
                coordinate = -abs(coordinate)
            elif direction in {'N', 'E'}:
                coordinate = abs(coordinate)
        return coordinate

    def _normalize_geographic_bounds(bounds) -> dict[str, float] | None:
        if not bounds:
            return None

        if isinstance(bounds, dict):
            west = _coerce_coordinate(bounds.get('west') if 'west' in bounds else bounds.get('westBoundingCoordinate'))
            east = _coerce_coordinate(bounds.get('east') if 'east' in bounds else bounds.get('eastBoundingCoordinate'))
            north = _coerce_coordinate(bounds.get('north') if 'north' in bounds else bounds.get('northBoundingCoordinate'))
            south = _coerce_coordinate(bounds.get('south') if 'south' in bounds else bounds.get('southBoundingCoordinate'))
        elif isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            west = _coerce_coordinate(bounds[0])
            east = _coerce_coordinate(bounds[1])
            north = _coerce_coordinate(bounds[2])
            south = _coerce_coordinate(bounds[3])
        else:
            return None

        if None in {west, east, north, south}:
            return None
        if not all(-180 <= value <= 180 for value in (west, east)):
            return None
        if not all(-90 <= value <= 90 for value in (north, south)):
            return None

        return {
            'west': min(west, east),
            'east': max(west, east),
            'north': max(north, south),
            'south': min(north, south),
        }

    def _extract_axis_bounds(text: str, axis_terms: tuple[str, ...], directions: str) -> tuple[float, float] | None:
        number = r"([+-]?\d+(?:\.\d+)?)"
        direction = rf"\s*([{directions}])?"
        separator = r"\s*(?:to|-|–)\s*"
        axis_pattern = "|".join(axis_terms)
        patterns = [
            rf"(?:{axis_pattern})[^.;,\n]*?{number}{direction}{separator}{number}{direction}",
            rf"{number}{direction}{separator}{number}{direction}[^.;,\n]*?(?:{axis_pattern})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            first = _coordinate_from_match(match.group(1), match.group(2))
            second = _coordinate_from_match(match.group(3), match.group(4))
            if first is not None and second is not None:
                return first, second
        return None

    def _extract_geographic_bounds_from_text(text: str | None) -> dict[str, float] | None:
        if not text:
            return None
        lat_bounds = _extract_axis_bounds(text, ('lat', 'latitude'), 'NS')
        lon_bounds = _extract_axis_bounds(text, ('lon', 'long', 'longitude'), 'EW')
        if not lat_bounds or not lon_bounds:
            return None

        south = min(lat_bounds)
        north = max(lat_bounds)
        west = min(lon_bounds)
        east = max(lon_bounds)
        return _normalize_geographic_bounds({
            'west': west,
            'east': east,
            'north': north,
            'south': south,
        })

    def _format_coordinate(value: float) -> str:
        text = f"{value:.6f}".rstrip('0').rstrip('.')
        return text if text != '-0' else '0'

    def _infer_taxon_rank(name: str, index: int, total: int) -> str:
        lower = name.lower()
        if lower in {'animalia', 'plantae', 'fungi', 'bacteria', 'archaea', 'protista', 'chromista'}:
            return 'kingdom'
        if lower.endswith('aceae') or lower.endswith('idae'):
            return 'family'
        if lower.endswith('ales'):
            return 'order'
        if lower.endswith('mycota') or lower.endswith('phyta'):
            return 'phylum'
        if lower.endswith('opsida') or lower.endswith('phyceae'):
            return 'class'
        if total == 1:
            return 'taxon'
        return 'higherTaxon' if index == 0 else 'taxon'

    def _normalize_taxonomic_keywords(raw_keywords) -> list[dict[str, str]]:
        if not isinstance(raw_keywords, list):
            return []

        normalized = []
        seen = set()
        for keyword in raw_keywords:
            if not isinstance(keyword, dict):
                continue
            scientific_name = clean_text(
                keyword.get('scientificName')
                or keyword.get('scientific_name')
                or keyword.get('taxonRankValue')
                or keyword.get('name')
            )
            if not scientific_name:
                continue
            rank = clean_text(keyword.get('rank') or keyword.get('taxonRankName'))
            common_name = clean_text(keyword.get('commonName') or keyword.get('common_name'))
            key = ((rank or '').lower(), scientific_name.lower(), (common_name or '').lower())
            if key in seen:
                continue
            seen.add(key)
            normalized.append({
                'rank': rank or 'taxon',
                'scientificName': scientific_name,
                'commonName': common_name,
            })
        return normalized

    def _fallback_taxonomic_keywords_from_scope(scope: str | None) -> list[dict[str, str]]:
        """Conservative fallback for simple scopes like "Plantae, Fabaceae".

        IPT writes taxonomicClassification from structured taxon keywords, not
        from prose descriptions. Stop before sentence-like tokens to avoid
        turning general text into bogus classifications.
        """
        if not scope:
            return []

        tokens = []
        for raw_token in str(scope).split(','):
            token = raw_token.strip()
            if not token:
                continue
            if any(char in token for char in '.;:()'):
                break
            if len(token.split()) > 3 or len(token) > 80:
                break
            tokens.append(token)
            if len(tokens) >= 12:
                break

        return [
            {
                'rank': _infer_taxon_rank(token, index, len(tokens)),
                'scientificName': token,
                'commonName': None,
            }
            for index, token in enumerate(tokens)
        ]

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
    def set_person(
        parent_node,
        person: dict,
        include_role: bool = False,
        role_value: str | None = None,
        include_email: bool = True,
    ):
        individual = get_or_create(parent_node, 'individualName')
        set_text(get_or_create(individual, 'givenName'), person.get('first_name') or person.get('givenName') or '')
        set_text(get_or_create(individual, 'surName'), person.get('last_name') or person.get('surName') or '')

        orcid_value = normalize_orcid_identifier(person.get('orcid') or person.get('userId'))

        if include_email:
            email_value = person.get('email') or person.get('electronicMailAddress') or ''
            if email_value:
                set_text(get_or_create(parent_node, 'electronicMailAddress'), email_value)
        if orcid_value:
            user_id = get_or_create(parent_node, 'userId')
            user_id.set('directory', 'https://orcid.org/')
            user_id.text = orcid_value
        if include_role:
            set_text(get_or_create(parent_node, 'role'), role_value or 'metadataProvider')

        ensure_child_order(
            parent_node,
            [
                'individualName',
                'organizationName',
                'positionName',
                'address',
                'phone',
                'electronicMailAddress',
                'onlineUrl',
                'userId',
                'role',
            ],
        )

    # Primary user as creator and metadataProvider
    primary_email = (getattr(user, 'email', None) or '') if user else ''
    # ORCID-only accounts use a synthetic address as their login identifier.
    # It is not a deliverable contact address and must not be published in EML.
    if primary_email.lower().endswith('@orcid.org'):
        primary_email = ''

    primary_person = {
        'first_name': (getattr(user, 'first_name', None) or 'Test') if user else 'Test',
        'last_name': (getattr(user, 'last_name', None) or 'User') if user else 'User',
        'orcid': (getattr(user, 'orcid_id', None) or '0000-0000-0000-0000') if user else '0000-0002-1825-0097',
        'email': primary_email,
    }

    creator_node = get_or_create(dataset_node, 'creator')
    set_person(creator_node, primary_person)

    metadata_provider_node = get_or_create(dataset_node, 'metadataProvider')
    set_person(metadata_provider_node, primary_person)

    # Optional additional metadata
    # GBIF accepts one of three dataset licenses. Keep the license in EML and
    # registration metadata instead of requiring a DwC-DP usage-policy table.
    _, license_metadata = normalize_gbif_license(eml_extra.get("license"))
    rights_link = get_or_create(
        get_or_create(get_or_create(dataset_node, "intellectualRights"), "para"),
        "ulink",
    )
    rights_link.set("url", license_metadata["url"])
    set_text(get_or_create(rights_link, "citetitle"), license_metadata["title"])

    # Contact (required by IPT): copy primary user by default, allow explicit override
    contact_person = dict(primary_person)
    contact_email = eml_extra.get('contact_email')
    if isinstance(contact_email, str) and contact_email.strip():
        contact_person['email'] = contact_email.strip()
    contact_node = get_or_create(dataset_node, 'contact')
    set_person(contact_node, contact_person)

    # Users array: add additional creators and (optionally) project personnel
    users_list = eml_extra.get('users') or []
    if users_list:
        for existing_creator in list(findall(dataset_node, 'creator')):
            dataset_node.remove(existing_creator)
        metadata_provider = find(dataset_node, 'metadataProvider')
        insert_index = 0
        if metadata_provider is not None:
            insert_index = list(dataset_node).index(metadata_provider)

        for person in users_list:
            creator = ET.Element('creator')
            set_person(creator, person)
            dataset_node.insert(insert_index, creator)
            insert_index += 1

    project_title = eml_extra.get('project_title')
    if isinstance(project_title, str):
        project_title = project_title.strip()
    if not project_title:
        project_title = None

    # The GBIF EML profile requires both a title and at least one personnel
    # entry whenever a project is present. Project metadata is optional, so
    # omit it rather than emitting an invalid title-only block.
    if project_title is not None and users_list:
        project_node = find(dataset_node, 'project')
        if project_node is None:
            project_node = ET.SubElement(dataset_node, 'project')
        set_text(get_or_create(project_node, 'title'), project_title)

        for person in users_list:
            personnel = ET.SubElement(project_node, 'personnel')
            set_person(
                personnel,
                person,
                include_role=True,
                role_value='metadataProvider',
                include_email=False,
            )

    # Coverage: rebuild from populated values only. GBIF's profile requires
    # geographicCoverage/boundingCoordinates and taxonomicCoverage/classification.
    remove_children(dataset_node, 'coverage')
    coverage = ET.Element('coverage')
    has_coverage = False

    # Geographic
    geographic_scope = clean_text(eml_extra.get('geographic_scope'))
    geographic_bounds = _normalize_geographic_bounds(eml_extra.get('geographic_bounds'))
    if geographic_bounds is None:
        geographic_bounds = _extract_geographic_bounds_from_text(geographic_scope)
    if geographic_scope and geographic_bounds:
        geo = ET.SubElement(coverage, 'geographicCoverage')
        set_text(ET.SubElement(geo, 'geographicDescription'), geographic_scope)
        bounds = ET.SubElement(geo, 'boundingCoordinates')
        set_text(ET.SubElement(bounds, 'westBoundingCoordinate'), _format_coordinate(geographic_bounds['west']))
        set_text(ET.SubElement(bounds, 'eastBoundingCoordinate'), _format_coordinate(geographic_bounds['east']))
        set_text(ET.SubElement(bounds, 'northBoundingCoordinate'), _format_coordinate(geographic_bounds['north']))
        set_text(ET.SubElement(bounds, 'southBoundingCoordinate'), _format_coordinate(geographic_bounds['south']))
        has_coverage = True

    # Temporal
    temporal_value = eml_extra.get('temporal_scope')
    if temporal_value:
        normalized_temporal = _normalize_temporal_scope(str(temporal_value))
        if normalized_temporal and normalized_temporal[0] == "range":
            _, start, end = normalized_temporal
            range_node = ET.SubElement(coverage, 'temporalCoverage')
            rnd = ET.SubElement(range_node, 'rangeOfDates')
            set_text(ET.SubElement(ET.SubElement(rnd, 'beginDate'), 'calendarDate'), start)
            set_text(ET.SubElement(ET.SubElement(rnd, 'endDate'), 'calendarDate'), end)
            has_coverage = True
        elif normalized_temporal and normalized_temporal[0] == "single":
            _, single_date = normalized_temporal
            single_node = ET.SubElement(coverage, 'temporalCoverage')
            sdt = ET.SubElement(single_node, 'singleDateTime')
            set_text(ET.SubElement(sdt, 'calendarDate'), single_date)
            has_coverage = True

    # Taxonomic
    taxonomic_scope = clean_text(eml_extra.get('taxonomic_scope'))

    taxonomic_keywords = _normalize_taxonomic_keywords(eml_extra.get('taxonomic_keywords'))
    if not taxonomic_keywords:
        taxonomic_keywords = _fallback_taxonomic_keywords_from_scope(taxonomic_scope)

    if taxonomic_keywords:
        tax = ET.SubElement(coverage, 'taxonomicCoverage')
        if taxonomic_scope:
            set_text(ET.SubElement(tax, 'generalTaxonomicCoverage'), taxonomic_scope)
        for keyword in taxonomic_keywords:
            classification = ET.SubElement(tax, 'taxonomicClassification')
            if keyword.get('rank'):
                set_text(ET.SubElement(classification, 'taxonRankName'), keyword['rank'])
            set_text(ET.SubElement(classification, 'taxonRankValue'), keyword['scientificName'])
            if keyword.get('commonName'):
                set_text(ET.SubElement(classification, 'commonName'), keyword['commonName'])
        has_coverage = True

    if has_coverage:
        methods_node = find(dataset_node, 'methods')
        if methods_node is not None:
            dataset_node.insert(list(dataset_node).index(methods_node), coverage)
        else:
            dataset_node.append(coverage)

    # Methods / methodology
    methods = get_or_create(dataset_node, 'methods')
    method_step = get_or_create(methods, 'methodStep')
    description_node = get_or_create(method_step, 'description')
    set_text(get_or_create(description_node, 'para'), eml_extra.get('methodology'))

    manuscript_doi = normalize_doi(eml_extra.get('manuscript_doi'))
    if manuscript_doi:
        alternate_identifier = ET.Element('alternateIdentifier')
        set_text(alternate_identifier, f"https://doi.org/{manuscript_doi}")
        title_node = find(dataset_node, 'title')
        if title_node is not None:
            dataset_node.insert(list(dataset_node).index(title_node), alternate_identifier)
        else:
            dataset_node.insert(0, alternate_identifier)

    dataset_citation = eml_extra.get('dataset_citation')
    if dataset_citation:
        additional_metadata = get_or_create(root, 'additionalMetadata')
        metadata_node = get_or_create(additional_metadata, 'metadata')
        gbif_node = get_or_create(metadata_node, 'gbif')
        set_text(
            get_or_create(gbif_node, 'dateStamp'),
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
        set_text(get_or_create(gbif_node, 'hierarchyLevel'), 'dataset')
        set_text(get_or_create(gbif_node, 'citation'), dataset_citation)
        ensure_child_order(gbif_node, ['dateStamp', 'hierarchyLevel', 'citation'])

    ensure_child_order(
        dataset_node,
        [
            'alternateIdentifier',
            'title',
            'creator',
            'metadataProvider',
            'associatedParty',
            'pubDate',
            'language',
            'abstract',
            'keywordSet',
            'intellectualRights',
            'distribution',
            'coverage',
            'purpose',
            'maintenance',
            'contact',
            'methods',
            'project',
        ],
    )

    # Prune empty elements except root and dataset and intellectualRights
    def is_empty(element: ET.Element) -> bool:
        has_text = (element.text or '').strip() != ''
        has_children = len(element) > 0
        has_attributes = len(element.attrib) > 0
        return not has_text and not has_children and not has_attributes

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
def upload_file(client, bucket_name, object_name, local_path, content_type="application/zip"):
    client.fput_object(bucket_name, object_name, local_path, content_type=content_type)

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


def assert_case_insensitive_unique_identifier(df, column_name: str):
    """
    Validate that non-empty identifier values are unique in a case-insensitive way.
    Raises ValueError with sample collisions when duplicates are found.
    """
    identifiers = df[column_name].astype('string').fillna('').str.strip()
    non_empty_identifiers = identifiers[identifiers != '']
    folded = non_empty_identifiers.str.casefold()
    duplicate_mask = folded.duplicated(keep=False)

    if duplicate_mask.any():
        duplicate_values = non_empty_identifiers[duplicate_mask]
        folded_values = duplicate_values.str.casefold()
        collision_examples = []
        for key in folded_values.drop_duplicates().head(5):
            variants = duplicate_values[folded_values == key].drop_duplicates().tolist()
            if variants:
                collision_examples.append(" / ".join(map(str, variants[:3])))

        example_text = "; ".join(collision_examples) if collision_examples else "No examples available"
        raise ValueError(
            f"Identifier column '{column_name}' contains case-insensitive duplicates. "
            f"Examples: {example_text}"
        )


def _unique_labels(labels) -> list[str]:
    seen = set()
    result = []
    for label in labels:
        normalized = str(label or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _tolerant_newick_tip_labels(content: str) -> list[str]:
    """Extract leaf labels from common non-standard Newick with unquoted spaces."""
    labels = []
    index = 0
    expect_child = True
    length = len(content)
    while index < length:
        char = content[index]
        if char == "[":
            end = content.find("]", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char in "(,":
            expect_child = True
            index += 1
            continue
        if char == ")":
            expect_child = False
            index += 1
            continue
        if not expect_child or char.isspace():
            index += 1
            continue
        if char == "(":
            index += 1
            continue

        if char in {"'", '"'}:
            quote = char
            index += 1
            value = []
            while index < length:
                if content[index] == quote:
                    if quote == "'" and index + 1 < length and content[index + 1] == quote:
                        value.append(quote)
                        index += 2
                        continue
                    index += 1
                    break
                value.append(content[index])
                index += 1
            label = "".join(value).strip()
        else:
            start = index
            while index < length and content[index] not in ",():;[]":
                index += 1
            label = content[start:index].strip()

        if label:
            labels.append(label)
        expect_child = False
    return _unique_labels(labels)


def parse_newick_tip_labels(content: str) -> list[str]:
    """Parse Newick tips strictly first, with a tolerant real-world fallback."""
    try:
        trees = dendropy.TreeList.get(
            data=content,
            schema="newick",
            preserve_underscores=True,
        )
        labels = [
            node.taxon.label
            for tree in trees
            for node in tree.leaf_node_iter()
            if node.taxon and node.taxon.label
        ]
        if labels:
            return _unique_labels(labels)
    except Exception:
        pass
    return _tolerant_newick_tip_labels(content)


def parse_nexus_tip_labels(content: str) -> list[str]:
    """
    Extract tip labels from a NEXUS format tree file.
    Handles both TRANSLATE blocks and direct tree tip labels.
    
    Args:
        content: Content of a NEXUS file
        
    Returns:
        List of tip labels found in the tree(s)
    """
    try:
        trees = dendropy.TreeList.get(
            data=content,
            schema="nexus",
            preserve_underscores=True,
        )
        labels = [
            node.taxon.label
            for tree in trees
            for node in tree.leaf_node_iter()
            if node.taxon and node.taxon.label
        ]
        if labels:
            return _unique_labels(labels)
    except Exception:
        pass

    translate_match = re.search(r"TRANSLATE\s+(.*?);", content, re.DOTALL | re.IGNORECASE)
    if translate_match:
        entries = re.findall(
            r"(?:^|,)\s*(\S+)\s+('(?:''|[^'])*'|\"[^\"]*\"|[^,]+)",
            translate_match.group(1),
            re.DOTALL,
        )
        return _unique_labels(
            value.strip().strip("'\"").replace("''", "'")
            for _, value in entries
        )

    tree_matches = re.findall(
        r"TREE\s+[^=]+=\s*(.*?);",
        content,
        re.DOTALL | re.IGNORECASE,
    )
    return _unique_labels(
        label
        for tree_string in tree_matches
        for label in parse_newick_tip_labels(re.sub(r"^\s*\[[^\]]*\]\s*", "", tree_string))
    )


def _dendropy_node_to_tree(node) -> dict:
    label = node.taxon.label if node.taxon is not None else node.label
    return {
        "name": label,
        "branch_length": node.edge_length if node.edge_length is not None else 0,
        "children": [_dendropy_node_to_tree(child) for child in node.child_node_iter()],
    }


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
    try:
        tree = dendropy.Tree.get(
            data=newick_string,
            schema="newick",
            preserve_underscores=True,
        )
        return _dendropy_node_to_tree(tree.seed_node)
    except Exception:
        # Some production files contain unquoted spaces in labels. DendroPy
        # correctly rejects those, so retain a tolerant fallback for display.
        pass

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
    
    cleaned = newick_string.strip()
    if cleaned.endswith(';'):
        cleaned = cleaned[:-1]

    # Simple tokenization: split into individual characters for parsing
    # This handles the tree structure character by character
    tokens = list(cleaned)
    
    root, _ = parse_node(tokens, 0)
    return root


def parse_nexus_to_tree(nexus_content: str) -> dict:
    """
    Parse a NEXUS format tree file into a hierarchical JSON structure.
    Extracts the first tree from the file.
    Applies TRANSLATE block mappings if present.
    
    Args:
        nexus_content: Content of a NEXUS file
        
    Returns:
        Dictionary representing the tree (same structure as parse_newick_to_tree)
    """
    try:
        trees = dendropy.TreeList.get(
            data=nexus_content,
            schema="nexus",
            preserve_underscores=True,
        )
        if trees:
            return _dendropy_node_to_tree(trees[0].seed_node)
    except Exception:
        # Fall back for non-standard but historically accepted NEXUS files.
        pass

    # First, check for a TRANSLATE block and build a mapping
    translate_map = {}
    translate_match = re.search(r'TRANSLATE\s+(.*?);', nexus_content, re.DOTALL | re.IGNORECASE)
    if translate_match:
        translate_block = translate_match.group(1)
        # Parse translate entries: key value, or key value,
        # Format: "1 Ephedrales_Ephedraceae_Ephedra_sinica_VDAO," or "1 Ephedrales_Ephedraceae_Ephedra_sinica_VDAO;"
        translate_entries = re.findall(r'(\d+)\s+([A-Za-z0-9_\-\.]+)[,\s]*', translate_block)
        for key, value in translate_entries:
            translate_map[key] = value.strip()
    
    # Extract tree string from NEXUS file
    tree_match = re.search(r'TREE\s+[^=]+=\s*(.*?);', nexus_content, re.DOTALL | re.IGNORECASE)
    if tree_match:
        tree_string = tree_match.group(1).strip()
        # Remove any comments or metadata
        # NEXUS files might have comments like [&R] before the tree
        tree_string = re.sub(r'\[[^\]]*\]', '', tree_string)
        
        # If we have a translate map, replace numeric IDs with their translated names
        # OPTIMIZED: Use a single regex pass with a callback function
        if translate_map:
            # Sort keys in descending order by length first, then numerically
            # This ensures "10" is checked before "1" to avoid partial matches
            sorted_keys = sorted(translate_map.keys(), key=lambda x: (-len(x), int(x)))
            
            # For very large translate maps, use a more efficient approach
            # Build a pattern that matches numbers at word boundaries
            if len(translate_map) > 1000:
                # For large maps, use a simpler pattern and check against the map
                # Pattern matches numbers surrounded by delimiters
                def replacer(match):
                    prefix = match.group(1)
                    number = match.group(2)
                    suffix = match.group(3)
                    # Look up the translation
                    translated = translate_map.get(number, number)
                    return prefix + translated + suffix
                
                # Use a pattern that matches any sequence of digits
                # The replacer will check if it's in our translate map
                pattern = r'(^|[\(\,:\s])(\d+)([\)\,:\s]|$)'
                tree_string = re.sub(pattern, replacer, tree_string)
            else:
                # For smaller maps, build an optimized alternation pattern
                escaped_keys = [re.escape(key) for key in sorted_keys]
                pattern = r'(^|[\(\,:\s])(' + '|'.join(escaped_keys) + r')([\)\,:\s]|$)'
                
                def replacer(match):
                    prefix = match.group(1)
                    number = match.group(2)
                    suffix = match.group(3)
                    translated = translate_map.get(number, number)
                    return prefix + translated + suffix
                
                tree_string = re.sub(pattern, replacer, tree_string)
        
        return parse_newick_to_tree(tree_string)
    
    return {"name": None, "branch_length": 0, "children": []}


def _local_xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _decoded_dialect_character(value: str | None, default: str) -> str:
    if value is None or value == "":
        return default
    return {
        r"\t": "\t",
        r"\n": "\n",
        r"\r": "\r",
    }.get(value, value)


def _safe_zip_filename(filename: str) -> str:
    path = PurePosixPath(str(filename).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1 or not path.name:
        raise ValueError(f"Unsafe archive filename: {filename!r}.")
    return path.name


def validate_dwca_archive(archive_path: str | Path) -> dict:
    """Validate the exact DwC-A ZIP that will be uploaded."""
    errors = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    errors.append(f"Archive contains an unsafe path: {name!r}.")
            for required in ("meta.xml", "eml.xml"):
                if required not in names:
                    errors.append(f"Archive is missing required file '{required}'.")
            if errors:
                return {"valid": False, "errors": errors}

            try:
                meta_root = ET.fromstring(archive.read("meta.xml").decode("utf-8", "strict"))
            except Exception as exc:
                return {"valid": False, "errors": [f"meta.xml is invalid strict UTF-8 XML: {exc}."]}
            try:
                eml_root = ET.fromstring(archive.read("eml.xml").decode("utf-8", "strict"))
            except Exception as exc:
                errors.append(f"eml.xml is invalid strict UTF-8 XML: {exc}.")
            else:
                for project in (
                    element
                    for element in eml_root.iter()
                    if _local_xml_name(element.tag) == "project"
                ):
                    child_names = [_local_xml_name(child.tag) for child in project]
                    if "title" not in child_names or "personnel" not in child_names:
                        errors.append(
                            "eml.xml project must contain a title and at least one personnel entry."
                        )

            table_elements = [
                element
                for element in meta_root
                if _local_xml_name(element.tag) in {"core", "extension"}
            ]
            if not table_elements:
                errors.append("meta.xml does not declare a core or extension table.")
            core_elements = [
                element
                for element in table_elements
                if _local_xml_name(element.tag) == "core"
            ]
            if len(core_elements) != 1:
                errors.append("meta.xml must declare exactly one core table.")

            core_identifiers = None
            extension_links = []

            for table in table_elements:
                table_kind = _local_xml_name(table.tag)
                files = next(
                    (child for child in table if _local_xml_name(child.tag) == "files"),
                    None,
                )
                locations = [] if files is None else [
                    (child.text or "").strip()
                    for child in files
                    if _local_xml_name(child.tag) == "location"
                ]
                if not locations:
                    errors.append(f"meta.xml {table_kind} does not declare a data file.")
                    continue

                indexes = []
                id_index = None
                coreid_index = None
                for child in table:
                    child_name = _local_xml_name(child.tag)
                    if child_name not in {"id", "coreid", "field"}:
                        continue
                    try:
                        index = int(child.attrib["index"])
                    except (KeyError, TypeError, ValueError):
                        errors.append(
                            f"meta.xml {table_kind} contains a {child_name} without a valid index."
                        )
                        continue
                    indexes.append(index)
                    if child_name == "id":
                        id_index = index
                    elif child_name == "coreid":
                        coreid_index = index

                if table_kind == "extension" and coreid_index is None:
                    errors.append("meta.xml extension does not declare a coreid field.")

                expected_columns = max(indexes, default=-1) + 1
                delimiter = _decoded_dialect_character(
                    table.attrib.get("fieldsTerminatedBy"),
                    "\t",
                )
                enclosed_by = table.attrib.get("fieldsEnclosedBy")
                quotechar = (
                    _decoded_dialect_character(enclosed_by, '"')
                    if enclosed_by
                    else None
                )
                ignored_headers = int(table.attrib.get("ignoreHeaderLines") or 0)

                for location in locations:
                    if location not in names:
                        errors.append(
                            f"meta.xml {table_kind} references missing file '{location}'."
                        )
                        continue
                    try:
                        text = archive.read(location).decode("utf-8", "strict")
                        reader_kwargs = {"delimiter": delimiter}
                        if quotechar is None:
                            reader_kwargs["quoting"] = csv.QUOTE_NONE
                        else:
                            reader_kwargs["quotechar"] = quotechar
                        reader = csv.reader(io.StringIO(text, newline=""), **reader_kwargs)
                        rows = list(reader)
                    except Exception as exc:
                        errors.append(
                            f"DwC-A table '{location}' cannot be parsed as strict UTF-8 CSV: {exc}."
                        )
                        continue

                    data_rows = rows[ignored_headers:]
                    malformed = [
                        row_number
                        for row_number, row in enumerate(
                            data_rows,
                            start=ignored_headers + 1,
                        )
                        if len(row) != expected_columns
                    ]
                    if malformed:
                        preview = ", ".join(map(str, malformed[:10]))
                        errors.append(
                            f"DwC-A table '{location}' has rows with a field count different "
                            f"from meta.xml: {preview}."
                        )

                    if table_kind == "core" and id_index is not None and not malformed:
                        identifiers = [row[id_index].strip() for row in data_rows]
                        if any(not value for value in identifiers):
                            errors.append(f"DwC-A core '{location}' contains blank identifiers.")
                        if len(identifiers) != len(set(value.casefold() for value in identifiers)):
                            errors.append(
                                f"DwC-A core '{location}' contains duplicate identifiers."
                            )
                        if core_identifiers is None:
                            core_identifiers = set()
                        core_identifiers.update(identifiers)

                    if (
                        table_kind == "extension"
                        and coreid_index is not None
                        and not malformed
                    ):
                        identifiers = [row[coreid_index].strip() for row in data_rows]
                        if any(not value for value in identifiers):
                            errors.append(
                                f"DwC-A extension '{location}' contains blank core identifiers."
                            )
                        extension_links.append((location, identifiers))

            if extension_links and core_identifiers is None:
                errors.append("DwC-A extensions require a core table with an explicit id field.")
            elif core_identifiers is not None:
                for location, identifiers in extension_links:
                    unresolved = sorted(set(identifiers) - core_identifiers)
                    if unresolved:
                        preview = ", ".join(unresolved[:5])
                        errors.append(
                            f"DwC-A extension '{location}' contains {len(unresolved)} core "
                            f"identifier(s) that do not resolve to the core. Examples: {preview}."
                        )
    except Exception as exc:
        errors.append(f"DwC-A archive cannot be opened: {exc}.")

    return {"valid": not errors, "errors": list(dict.fromkeys(errors))}


def upload_dwca(
    df_core,
    title,
    description,
    core_type: DarwinCoreCoreType,
    extensions: list[tuple[object, DarwinCoreExtensionType, str]] | None = None,
    user=None,
    eml_extra: dict | None = None,
    additional_files: list[tuple[str, bytes]] | None = None,
):
    df_core = _sanitize_dataframe_for_utf8_export(df_core)
    extensions = [
        (_sanitize_dataframe_for_utf8_export(ext_df), ext_type, core_id_column)
        for ext_df, ext_type, core_id_column in extensions or []
    ]
    serialization_errors = utf8_serialization_errors({
        "core": df_core,
        **{
            f"extension-{index}": ext_df
            for index, (ext_df, _, _) in enumerate(extensions, start=1)
        },
    })
    if serialization_errors:
        raise ValueError(
            "DwC-A UTF-8 preflight failed: " + "; ".join(serialization_errors)
        )

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
    core_id_column = None
    if core_schema.core_id_column:
        core_id_index = ensure_identifier_column(df_core, core_schema.core_id_column)
        core_id_column = df_core.columns[core_id_index]
        if core_id_index != 0:
            identifier_values = df_core.pop(core_id_column)
            df_core.insert(0, core_id_column, identifier_values)
            core_id_index = 0
        df_core[core_id_column] = (
            df_core[core_id_column].astype("string").fillna("").str.strip()
        )
        if df_core[core_id_column].eq("").any():
            raise ValueError(f"DwC-A core identifier column '{core_id_column}' contains blank values.")
        assert_case_insensitive_unique_identifier(df_core, core_id_column)

    core_identifiers = (
        set(df_core[core_id_column].tolist())
        if core_id_index is not None
        else set()
    )

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
        "fields_enclosed_by": '"',
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

    prepared_extensions = []
    extension_link_issues = []
    for extension_index, (ext_df, ext_type, extension_core_id_column) in enumerate(extensions or []):
        schema = EXTENSION_SCHEMAS[ext_type]
        if schema.compatible_cores and core_type.value not in schema.compatible_cores:
            compatible = ", ".join(schema.compatible_cores)
            raise ValueError(
                f"DwC-A extension '{ext_type}' is not compatible with the '{core_type.value}' "
                f"core. Compatible cores: {compatible}."
            )
        ext_spec_path = schema.spec_path

        matching_columns = [
            column
            for column in ext_df.columns
            if str(column).casefold() == str(extension_core_id_column).casefold()
        ]
        if not matching_columns:
            extension_link_issues.append({
                "extension_index": extension_index,
                "extension_type": ext_type.value,
                "requested_column": str(extension_core_id_column),
                "missing_column": True,
                "blank_count": 0,
                "unresolved_count": 0,
                "examples": [],
            })
            continue
        actual_core_id_column = matching_columns[0]
        if ext_df.columns.get_loc(actual_core_id_column) != 0:
            identifier_values = ext_df.pop(actual_core_id_column)
            ext_df.insert(0, actual_core_id_column, identifier_values)
        ext_df[actual_core_id_column] = (
            ext_df[actual_core_id_column].astype("string").fillna("").str.strip()
        )
        blank_links = int(ext_df[actual_core_id_column].eq("").sum())
        if core_id_index is None:
            raise ValueError("DwC-A extensions require a core table with an explicit identifier.")
        nonblank_links = set(ext_df.loc[ext_df[actual_core_id_column].ne(""), actual_core_id_column])
        unresolved = sorted(nonblank_links - core_identifiers)
        if blank_links or unresolved:
            extension_link_issues.append({
                "extension_index": extension_index,
                "extension_type": ext_type.value,
                "requested_column": str(extension_core_id_column),
                "actual_column": str(actual_core_id_column),
                "missing_column": False,
                "blank_count": blank_links,
                "unresolved_count": len(unresolved),
                "examples": list(map(str, unresolved[:5])),
            })
            continue

        prepared_extensions.append((ext_df, ext_type, actual_core_id_column, schema))

    if extension_link_issues:
        raise DwcaExtensionLinkError(core_id_column, extension_link_issues)

    for ext_df, ext_type, actual_core_id_column, schema in prepared_extensions:
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
            "id_index": 0,
            "fields_enclosed_by": '"',
            "output_filename": re.sub(r"[^a-z0-9._-]+", "-", schema.key.casefold()) + ".txt",
        }
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

    file_name = datetime.now().strftime('output-%Y-%m-%d-%H%M%S-%f') + '.zip'
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = os.path.join(temp_dir, file_name)
            archive.export(local_path)
            
            # Add additional files (e.g., tree files) to the archive
            if additional_files:
                with zipfile.ZipFile(local_path, 'a', zipfile.ZIP_DEFLATED) as zipf:
                    for filename, file_content in additional_files:
                        safe_name = _safe_zip_filename(filename)
                        if safe_name in zipf.namelist():
                            raise ValueError(
                                f"Ancillary file '{safe_name}' conflicts with a DwC-A package file."
                            )
                        zipf.writestr(safe_name, file_content)

            archive_validation = validate_dwca_archive(local_path)
            if not archive_validation["valid"]:
                raise ValueError(
                    "Serialized DwC-A archive validation failed: "
                    + "; ".join(archive_validation["errors"])
                )
            
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

def register_dataset_and_endpoint(
    title,
    description,
    url,
    license_name=DEFAULT_GBIF_LICENSE,
    core_type: DarwinCoreCoreType | str = DarwinCoreCoreType.OCCURRENCE,
):
    print('registering dataset')
    _, license_metadata = normalize_gbif_license(license_name)
    payload = {
        'title': title,
        'description': description,
        'publishingOrganizationKey': os.getenv('GBIF_PUBLISHING_ORGANIZATION_KEY'),
        'installationKey': os.getenv('GBIF_INSTALLATION_KEY'),
        'language': 'en',
        'type': gbif_dataset_type_for_core(core_type),
        'license': license_metadata['url'],
    }
    response = requests.post(
        f"{os.getenv('GBIF_API_URL')}/dataset",
        json=payload,
        headers={'Content-Type': 'application/json'},
        auth=HTTPBasicAuth(os.getenv('GBIF_USER'), os.getenv('GBIF_PASSWORD')),
        timeout=30,
    )
    if response.status_code == 201:
        dataset_key = response.json()
    else:
        raise requests.exceptions.HTTPError(f'Failed to add dataset. Status code: {response.status_code}, Response JSON: {response.json()}')

    print(dataset_key)
    register_endpoint(dataset_key, url)
    dataset_url = os.getenv('GBIF_DATASET_URL', 'https://www.gbif-test.org/dataset').rstrip('/')
    return f'{dataset_url}/{dataset_key}'


def register_endpoint(dataset_key, url):
    payload = { 'type': 'DWC_ARCHIVE', 'url': url, 'machineTags': [] }
    response = requests.post(
        f"{os.getenv('GBIF_API_URL')}/dataset/{dataset_key}/endpoint",
        json=payload,
        headers={'Content-Type': 'application/json'},
        auth=HTTPBasicAuth(os.getenv('GBIF_USER'), os.getenv('GBIF_PASSWORD')),
        timeout=30,
    )
    if response.status_code != 201:
        raise requests.exceptions.HTTPError(f'Failed to add endpoint. Status code: {response.status_code}, Response JSON: {response.json()}')
