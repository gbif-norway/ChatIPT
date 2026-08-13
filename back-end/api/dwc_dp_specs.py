from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping

import pandas as pd
from frictionless import Package, Schema

from api.helpers.publish import make_eml, upload_file
from api.publication_validation import (
    semantic_dwc_dp_warnings,
    utf8_serialization_errors,
    validate_publication_safety,
)


_BASE_DIR = Path(__file__).resolve().parent
_DWC_DP_ROOT = _BASE_DIR / "templates" / "dwc-dp"
_TABLE_SCHEMA_ROOT = _DWC_DP_ROOT / "table-schemas"

DWC_DP_SCHEMA_REVISION = "cbb6c887043876351eec1bed01c3dfc2e05c4eb4"
DWC_DP_SCHEMA_REPOSITORY = "https://github.com/gbif/dwc-dp"

# TDWG's versioned profile URI is not deployed yet. Pin the live upstream file
# to the same immutable revision as the vendored snapshot so exported package
# descriptors remain resolvable and reproducible.
DWC_DP_PROFILE_URL = (
    "https://raw.githubusercontent.com/gbif/dwc-dp/"
    f"{DWC_DP_SCHEMA_REVISION}/dwc-dp/dwc-dp-profile.json"
)


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


_DWC_DP_INDEX = _load_json(_DWC_DP_ROOT / "index.json")
_DWC_DP_VERSION = _load_json(_DWC_DP_ROOT / "version.json")
DWC_DP_SCHEMA_VERSION = str(_DWC_DP_INDEX.get("version") or _DWC_DP_VERSION.get("version") or "")
DWC_DP_SCHEMA_ISSUED = str(_DWC_DP_INDEX.get("issued") or "")


def _dwc_dp_schema_digest() -> str:
    digest = hashlib.sha256()
    paths = [
        _DWC_DP_ROOT / "dwc-dp-profile.json",
        _DWC_DP_ROOT / "index.json",
        _DWC_DP_ROOT / "version.json",
        *sorted(_TABLE_SCHEMA_ROOT.glob("*.json")),
    ]
    for path in paths:
        digest.update(path.relative_to(_DWC_DP_ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


DWC_DP_SCHEMA_SHA256 = _dwc_dp_schema_digest()


def dwc_dp_schema_snapshot() -> Dict[str, str]:
    return {
        "version": DWC_DP_SCHEMA_VERSION,
        "issued": DWC_DP_SCHEMA_ISSUED,
        "revision": DWC_DP_SCHEMA_REVISION,
        "sha256": DWC_DP_SCHEMA_SHA256,
        "source": f"{DWC_DP_SCHEMA_REPOSITORY}/tree/{DWC_DP_SCHEMA_REVISION}/dwc-dp",
    }


def _as_field_list(value: Any) -> list[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


@dataclass(frozen=True)
class DwcDpTableSpec:
    name: str
    title: str
    description: str
    schema: Dict[str, Any]

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(field["name"] for field in self.schema.get("fields", []))

    @property
    def field_descriptors(self) -> Dict[str, Dict[str, Any]]:
        return {field["name"]: field for field in self.schema.get("fields", [])}

    @property
    def primary_key(self) -> list[str]:
        return _as_field_list(self.schema.get("primaryKey"))

    @property
    def weak_primary_key(self) -> list[str]:
        return _as_field_list(self.schema.get("weakPrimaryKey"))

    @property
    def foreign_keys(self) -> list[Dict[str, Any]]:
        return list(self.schema.get("foreignKeys") or [])

    @property
    def weak_foreign_keys(self) -> list[Dict[str, Any]]:
        return list(self.schema.get("weakForeignKeys") or [])


def load_dwc_dp_table_specs() -> Dict[str, DwcDpTableSpec]:
    specs: Dict[str, DwcDpTableSpec] = {}
    for path in sorted(_TABLE_SCHEMA_ROOT.glob("*.json")):
        schema = _load_json(path)
        name = path.stem
        specs[name] = DwcDpTableSpec(
            name=name,
            title=schema.get("title") or name,
            description=schema.get("description") or "",
            schema=schema,
        )
    return specs


TABLE_SPECS = load_dwc_dp_table_specs()
RESERVED_TABLE_NAMES = frozenset(TABLE_SPECS.keys())


def validate_vendored_dwc_dp_schemas() -> list[str]:
    """Check the vendored snapshot before it is used to validate user data."""
    errors: list[str] = []
    declared_names = {
        str(item.get("name"))
        for item in _DWC_DP_INDEX.get("tableSchemas", [])
        if item.get("name")
    }
    if declared_names != set(TABLE_SPECS):
        missing = sorted(declared_names - set(TABLE_SPECS))
        extra = sorted(set(TABLE_SPECS) - declared_names)
        if missing:
            errors.append(f"Vendored DwC-DP schemas are missing: {', '.join(missing)}.")
        if extra:
            errors.append(f"Vendored DwC-DP schemas are not declared in index.json: {', '.join(extra)}.")

    for name, spec in TABLE_SPECS.items():
        try:
            Schema.from_descriptor(spec.schema)
        except Exception as exc:
            errors.append(f"DwC-DP table schema '{name}' is not a valid Frictionless schema: {exc}")
            continue

        fields = spec.field_descriptors
        for field_name, field in fields.items():
            missing_metadata = [
                key
                for key in ("name", "title", "description", "type", "dcterms:isVersionOf")
                if key not in field
            ]
            if missing_metadata:
                errors.append(
                    f"DwC-DP table schema '{name}' field '{field_name}' is missing metadata: "
                    f"{', '.join(missing_metadata)}."
                )

        for key_type, keys in (
            ("primary key", [spec.primary_key]),
            ("weak primary key", [spec.weak_primary_key]),
        ):
            for key_fields in keys:
                for field_name in key_fields:
                    if field_name not in fields:
                        errors.append(f"DwC-DP table schema '{name}' {key_type} field '{field_name}' is absent.")

        for relationship_type, foreign_keys in (
            ("foreign key", spec.foreign_keys),
            ("weak foreign key", spec.weak_foreign_keys),
        ):
            for foreign_key in foreign_keys:
                source_fields = _as_field_list(foreign_key.get("fields"))
                reference = foreign_key.get("reference") or {}
                target_name = str(reference.get("resource") or name)
                target_fields = _as_field_list(reference.get("fields"))
                if not source_fields or any(field not in fields for field in source_fields):
                    errors.append(f"DwC-DP table schema '{name}' has an invalid {relationship_type} source.")
                    continue
                target_spec = TABLE_SPECS.get(target_name)
                if not target_spec:
                    errors.append(
                        f"DwC-DP table schema '{name}' {relationship_type} references missing table '{target_name}'."
                    )
                    continue
                if not target_fields or any(field not in target_spec.fields for field in target_fields):
                    errors.append(
                        f"DwC-DP table schema '{name}' {relationship_type} references missing fields in "
                        f"'{target_name}'."
                    )

    return errors


VENDORED_SCHEMA_ERRORS = tuple(validate_vendored_dwc_dp_schemas())
if VENDORED_SCHEMA_ERRORS:
    raise RuntimeError("Invalid vendored DwC-DP schema snapshot: " + "; ".join(VENDORED_SCHEMA_ERRORS))


def get_table_spec(table_name: str) -> DwcDpTableSpec:
    normalized = str(table_name or "").strip()
    if normalized not in TABLE_SPECS:
        raise KeyError(
            f"Unknown DwC-DP table '{table_name}'. Reserved table names are: "
            f"{', '.join(sorted(RESERVED_TABLE_NAMES))}."
        )
    return TABLE_SPECS[normalized]


def _explorer_join_value(value: Any) -> str | None:
    if value is None:
        return None
    try:
        missing = pd.isna(value)
        if type(missing).__name__ in {"bool", "bool_"} and bool(missing):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        text = str(value)
    text = text.strip()
    return text or None


def _explorer_join_keys(df: pd.DataFrame, fields: list[str]) -> list[tuple[str, ...] | None]:
    if not fields or any(field not in df.columns for field in fields):
        return []
    keys: list[tuple[str, ...] | None] = []
    for values in df[fields].itertuples(index=False, name=None):
        normalized = tuple(_explorer_join_value(value) for value in values)
        keys.append(None if any(value is None for value in normalized) else normalized)
    return keys


def build_dwc_dp_explorer_model(dataset: Any) -> Dict[str, Any]:
    """Build a compact, schema-accurate model for the interactive package explorer."""
    resource_tables = {
        table.title: table
        for table in dataset.table_set.filter(title__in=RESERVED_TABLE_NAMES).order_by("title", "id")
    }
    nodes: list[Dict[str, Any]] = []
    edges: list[Dict[str, Any]] = []

    for name, table in resource_tables.items():
        spec = TABLE_SPECS[name]
        available_fields = {str(field) for field in table.df.columns}
        fields = []
        for field_name in table.df.columns:
            normalized_name = str(field_name)
            descriptor = spec.field_descriptors.get(normalized_name, {})
            fields.append(
                {
                    "name": normalized_name,
                    "title": descriptor.get("title") or normalized_name,
                    "description": descriptor.get("description") or "",
                    "type": descriptor.get("type") or "string",
                    "primary": normalized_name in spec.primary_key,
                    "weakPrimary": normalized_name in spec.weak_primary_key,
                }
            )
        nodes.append(
            {
                "id": name,
                "tableId": table.id,
                "title": spec.title,
                "description": spec.description,
                "comments": spec.schema.get("comments") or "",
                "examples": spec.schema.get("examples") or "",
                "rowCount": int(len(table.df)),
                "columnCount": len(available_fields),
                "primaryKey": spec.primary_key,
                "weakPrimaryKey": spec.weak_primary_key,
                "fields": fields,
            }
        )

    for source_name, source_table in resource_tables.items():
        spec = TABLE_SPECS[source_name]
        relationships = [
            ("foreign", relationship)
            for relationship in spec.foreign_keys
        ] + [
            ("weak", relationship)
            for relationship in spec.weak_foreign_keys
        ]
        for index, (kind, relationship) in enumerate(relationships):
            reference = relationship.get("reference") or {}
            target_name = str(reference.get("resource") or source_name)
            target_table = resource_tables.get(target_name)
            if target_table is None:
                continue

            source_fields = _as_field_list(relationship.get("fields"))
            target_fields = _as_field_list(reference.get("fields"))
            source_keys = _explorer_join_keys(source_table.df, source_fields)
            target_keys = _explorer_join_keys(target_table.df, target_fields)
            if not source_keys or not target_keys:
                continue

            populated_keys = [key for key in source_keys if key is not None]
            target_key_set = {key for key in target_keys if key is not None}
            linked_rows = sum(key in target_key_set for key in populated_keys)
            predicate = (
                relationship.get("predicate")
                or relationship.get("relationship")
                or relationship.get("predicateLabel")
                or "relates to"
            )
            edges.append(
                {
                    "id": f"{source_name}:{kind}:{index}:{target_name}",
                    "source": source_name,
                    "target": target_name,
                    "predicate": predicate,
                    "kind": kind,
                    "sourceFields": source_fields,
                    "targetFields": target_fields,
                    "sourceRows": int(len(source_table.df)),
                    "populatedRows": len(populated_keys),
                    "linkedRows": linked_rows,
                    "unmatchedRows": len(populated_keys) - linked_rows,
                    "blankRows": int(len(source_table.df)) - len(populated_keys),
                }
            )

    return {
        "schema": dwc_dp_schema_snapshot(),
        "nodes": nodes,
        "edges": edges,
    }


def normalize_resource_name(value: str) -> str:
    return str(value or "").strip().replace("_", "-").lower()


def _series_text(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.strip()


def _non_empty_mask(series: pd.Series) -> pd.Series:
    return _series_text(series) != ""


def _value_preview(series: pd.Series, mask: pd.Series) -> str:
    values = _series_text(series[mask]).drop_duplicates().head(5).tolist()
    return ", ".join(repr(value) for value in values)


def _validate_field_values(
    resource_name: str,
    field: Dict[str, Any],
    series: pd.Series,
    errors: list[str],
) -> None:
    field_name = field["name"]
    constraints = field.get("constraints") or {}
    populated = _non_empty_mask(series)

    if constraints.get("required") is True and not populated.all():
        errors.append(f"Resource '{resource_name}' has blank values in required field '{field_name}'.")

    if constraints.get("unique") is True:
        non_empty = _series_text(series)[populated]
        if not non_empty.is_unique:
            errors.append(f"Resource '{resource_name}' field '{field_name}' must be unique.")

    field_type = field.get("type")
    numeric = None
    if field_type in {"integer", "number"}:
        numeric = pd.to_numeric(_series_text(series).replace("", pd.NA), errors="coerce")
        invalid = populated & (numeric.isna() | ~numeric.map(lambda value: math.isfinite(value) if pd.notna(value) else True))
        if invalid.any():
            errors.append(
                f"Resource '{resource_name}' field '{field_name}' must contain {field_type} values; "
                f"invalid examples: {_value_preview(series, invalid)}."
            )
            numeric = None
        elif field_type == "integer":
            non_integer = populated & ((numeric % 1) != 0)
            if non_integer.any():
                errors.append(
                    f"Resource '{resource_name}' field '{field_name}' must contain integer values; "
                    f"invalid examples: {_value_preview(series, non_integer)}."
                )
                numeric = None
    elif field_type == "boolean":
        normalized = _series_text(series).str.lower()
        invalid = populated & ~normalized.isin({"true", "false"})
        if invalid.any():
            errors.append(
                f"Resource '{resource_name}' field '{field_name}' must contain boolean values "
                f"('true' or 'false'); invalid examples: {_value_preview(series, invalid)}."
            )

    if numeric is not None:
        if "minimum" in constraints and (numeric[populated] < constraints["minimum"]).any():
            errors.append(
                f"Resource '{resource_name}' field '{field_name}' has values below minimum "
                f"{constraints['minimum']}."
            )
        if "maximum" in constraints and (numeric[populated] > constraints["maximum"]).any():
            errors.append(
                f"Resource '{resource_name}' field '{field_name}' has values above maximum "
                f"{constraints['maximum']}."
            )


def _foreign_key_values(df: pd.DataFrame, fields: list[str]) -> tuple[set[tuple[str, ...]], bool]:
    values: set[tuple[str, ...]] = set()
    partially_blank = False
    for row in df[fields].itertuples(index=False, name=None):
        normalized = tuple(str(value).strip() if pd.notna(value) else "" for value in row)
        if not any(normalized):
            continue
        if not all(normalized):
            partially_blank = True
            continue
        values.add(normalized)
    return values, partially_blank


def validate_dwc_dp_resources(resources: Mapping[str, pd.DataFrame]) -> Dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    normalized_resources: Dict[str, pd.DataFrame] = {}

    if not resources:
        errors.append("A DwC-DP must contain at least one table resource.")

    for raw_name, df in resources.items():
        raw_name_text = str(raw_name)
        name = normalize_resource_name(raw_name_text)
        if name != raw_name_text:
            warnings.append(f"Resource '{raw_name_text}' will be normalized to '{name}'.")
        if name not in RESERVED_TABLE_NAMES:
            errors.append(f"Resource '{raw_name_text}' is not a reserved DwC-DP table name.")
            continue
        if name in normalized_resources:
            errors.append(f"Resource '{name}' is duplicated.")
            continue
        if not isinstance(df, pd.DataFrame):
            errors.append(f"Resource '{name}' is not a pandas DataFrame.")
            continue
        if df.empty:
            warnings.append(f"Resource '{name}' has no data rows.")
        normalized_resources[name] = df.copy()

    valid_columns_by_resource: Dict[str, set[str]] = {}
    for name, df in normalized_resources.items():
        spec = get_table_spec(name)
        raw_columns = list(df.columns)
        string_columns = [column for column in raw_columns if isinstance(column, str)]
        invalid_type_columns = [column for column in raw_columns if not isinstance(column, str)]
        if invalid_type_columns:
            errors.append(f"Resource '{name}' has non-string column names: {invalid_type_columns!r}.")

        blank_columns = [column for column in string_columns if not column.strip()]
        if blank_columns:
            errors.append(f"Resource '{name}' has blank column names.")

        whitespace_columns = [column for column in string_columns if column != column.strip()]
        if whitespace_columns:
            errors.append(
                f"Resource '{name}' has column names with surrounding whitespace: "
                f"{', '.join(repr(column) for column in whitespace_columns)}."
            )

        counts = Counter(string_columns)
        duplicates = sorted(column for column, count in counts.items() if count > 1)
        if duplicates:
            errors.append(f"Resource '{name}' has duplicate columns: {', '.join(duplicates)}.")

        valid_columns = {
            column
            for column in string_columns
            if column and column == column.strip() and counts[column] == 1
        }
        valid_columns_by_resource[name] = valid_columns
        unknown = sorted(valid_columns - set(spec.fields))
        if unknown:
            errors.append(
                f"Resource '{name}' contains fields not defined by its DwC-DP table schema: "
                f"{', '.join(unknown)}."
            )

        for field in spec.schema.get("fields", []):
            field_name = field["name"]
            constraints = field.get("constraints") or {}
            if field_name not in valid_columns:
                if constraints.get("required") is True:
                    errors.append(f"Resource '{name}' is missing required field '{field_name}'.")
                continue
            _validate_field_values(name, field, df[field_name], errors)

        weak_primary_key = spec.weak_primary_key
        if weak_primary_key and all(field in valid_columns for field in weak_primary_key):
            populated_keys: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
            for row in df[weak_primary_key].itertuples(index=False, name=None):
                display = tuple(str(value).strip() if pd.notna(value) else "" for value in row)
                if not all(display):
                    continue
                populated_keys.append(
                    (tuple(value.casefold() for value in display), display)
                )

            key_counts = Counter(key for key, _display in populated_keys)
            duplicate_keys = {key for key, count in key_counts.items() if count > 1}
            if duplicate_keys:
                duplicate_rows = sum(key_counts[key] for key in duplicate_keys)
                examples = []
                for key, display in populated_keys:
                    if key in duplicate_keys and display not in examples:
                        examples.append(display)
                    if len(examples) >= 5:
                        break
                preview = "; ".join(" | ".join(value) for value in examples)
                warnings.append(
                    f"Resource '{name}' weak primary key "
                    f"'{', '.join(weak_primary_key)}' contains {duplicate_rows} row(s) across "
                    f"{len(duplicate_keys)} case-insensitive duplicate value(s). Examples: {preview}. "
                    "DwC-DP permits weak-key duplication, but these values should be corrected before "
                    "using the resource as a DwC-A core."
                )

    for name, df in normalized_resources.items():
        spec = get_table_spec(name)
        columns = valid_columns_by_resource[name]
        for foreign_key in spec.foreign_keys:
            source_fields = _as_field_list(foreign_key.get("fields"))
            if not source_fields or not all(field in columns for field in source_fields):
                continue
            source_values, partially_blank = _foreign_key_values(df, source_fields)
            if partially_blank:
                errors.append(
                    f"Resource '{name}' has a partially blank composite foreign key "
                    f"'{', '.join(source_fields)}'."
                )
            if not source_values:
                continue

            reference = foreign_key.get("reference") or {}
            target_name = str(reference.get("resource") or name)
            target_fields = _as_field_list(reference.get("fields"))
            target_df = normalized_resources.get(target_name)
            if target_df is None:
                errors.append(
                    f"Resource '{name}' has populated foreign key '{', '.join(source_fields)}' "
                    f"but referenced resource '{target_name}' is absent."
                )
                continue
            target_columns = valid_columns_by_resource[target_name]
            if not target_fields or not all(field in target_columns for field in target_fields):
                errors.append(
                    f"Resource '{name}' references '{target_name}' fields '{', '.join(target_fields)}', "
                    "but the referenced fields are absent."
                )
                continue
            target_values, _ = _foreign_key_values(target_df, target_fields)
            missing = sorted(source_values - target_values)
            if missing:
                preview = ", ".join(" | ".join(value) for value in missing[:10])
                suffix = f" and {len(missing) - 10} more" if len(missing) > 10 else ""
                errors.append(
                    f"Resource '{name}' field(s) '{', '.join(source_fields)}' contain values not found in "
                    f"'{target_name}.{', '.join(target_fields)}': {preview}{suffix}."
                )

    errors.extend(utf8_serialization_errors(normalized_resources))
    warnings.extend(validate_publication_safety(normalized_resources))
    warnings.extend(semantic_dwc_dp_warnings(normalized_resources))

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "resources": sorted(normalized_resources.keys()),
        "schema": dwc_dp_schema_snapshot(),
    }


def _resource_schema_for_dataframe(
    table_name: str,
    df: pd.DataFrame,
    included_resources: Iterable[str],
) -> Dict[str, Any]:
    spec = get_table_spec(table_name)
    descriptors = spec.field_descriptors
    columns = [column for column in df.columns if isinstance(column, str) and column in descriptors]
    schema: Dict[str, Any] = {
        "fields": [deepcopy(descriptors[column]) for column in columns],
    }

    for property_name, key_fields in (
        ("primaryKey", spec.primary_key),
        ("weakPrimaryKey", spec.weak_primary_key),
    ):
        if key_fields and all(field in columns for field in key_fields):
            schema[property_name] = key_fields[0] if len(key_fields) == 1 else key_fields

    included = set(included_resources)
    for property_name, foreign_keys in (
        ("foreignKeys", spec.foreign_keys),
        ("weakForeignKeys", spec.weak_foreign_keys),
    ):
        included_keys = []
        for foreign_key in foreign_keys:
            source_fields = _as_field_list(foreign_key.get("fields"))
            target_name = str((foreign_key.get("reference") or {}).get("resource") or table_name)
            if all(field in columns for field in source_fields) and target_name in included:
                included_keys.append(deepcopy(foreign_key))
        if included_keys:
            schema[property_name] = included_keys

    return schema


def validate_datapackage_descriptor(descriptor: Mapping[str, Any]) -> list[str]:
    """Validate generated metadata without fetching the currently undeployed profile URI."""
    errors: list[str] = []
    if descriptor.get("profile") != DWC_DP_PROFILE_URL:
        errors.append(f"Descriptor profile must be '{DWC_DP_PROFILE_URL}'.")
    if descriptor.get("dwcDpSchema") != dwc_dp_schema_snapshot():
        errors.append("Descriptor must identify the exact vendored DwC-DP schema snapshot.")

    frictionless_descriptor = deepcopy(dict(descriptor))
    frictionless_descriptor["profile"] = "data-package"
    try:
        Package.from_descriptor(frictionless_descriptor)
    except Exception as exc:
        errors.append(f"Descriptor is not a valid Frictionless Data Package: {exc}")

    for resource in descriptor.get("resources") or []:
        name = resource.get("name")
        if name in RESERVED_TABLE_NAMES:
            if resource.get("profile") != "tabular-data-resource":
                errors.append(f"Reserved resource '{name}' must use the tabular-data-resource profile.")
            schema = resource.get("schema") or {}
            try:
                Schema.from_descriptor(schema)
            except Exception as exc:
                errors.append(f"Resource '{name}' has an invalid Frictionless table schema: {exc}")
            for field in schema.get("fields") or []:
                missing = [
                    key
                    for key in ("name", "title", "description", "type", "dcterms:isVersionOf")
                    if key not in field
                ]
                if missing:
                    errors.append(
                        f"Resource '{name}' field '{field.get('name', '<unnamed>')}' is missing metadata: "
                        f"{', '.join(missing)}."
                    )
    return errors


def build_datapackage_descriptor(
    resources: Mapping[str, pd.DataFrame],
    dataset_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    version: str | None = None,
) -> Dict[str, Any]:
    validation = validate_dwc_dp_resources(resources)
    if not validation["valid"]:
        raise ValueError("DwC-DP validation failed: " + "; ".join(validation["errors"]))

    names = [normalize_resource_name(name) for name in resources]
    descriptor: Dict[str, Any] = {
        "profile": DWC_DP_PROFILE_URL,
        "created": datetime.now(timezone.utc).isoformat(),
        "dwcDpSchema": dwc_dp_schema_snapshot(),
        "resources": [],
    }
    if dataset_id:
        descriptor["id"] = dataset_id
    if version:
        descriptor["version"] = version
    if title:
        descriptor["title"] = title
    if description:
        descriptor["description"] = description

    for raw_name, df in resources.items():
        name = normalize_resource_name(raw_name)
        descriptor["resources"].append(
            {
                "name": name,
                "path": f"{name}.csv",
                "profile": "tabular-data-resource",
                "format": "csv",
                "mediatype": "text/csv",
                "schema": _resource_schema_for_dataframe(name, df, names),
            }
        )

    descriptor_errors = validate_datapackage_descriptor(descriptor)
    if descriptor_errors:
        raise ValueError("Invalid DwC-DP descriptor: " + "; ".join(descriptor_errors))
    return descriptor


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        df.to_csv(handle, index=False, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)


def _safe_archive_name(filename: str) -> str:
    name = PurePosixPath(str(filename).replace("\\", "/"))
    if name.is_absolute() or ".." in name.parts or len(name.parts) != 1 or not name.name:
        raise ValueError(f"Unsafe ancillary archive filename: {filename!r}.")
    return name.name


def validate_dwc_dp_archive(
    archive_path: Path,
    expected_resources: Mapping[str, pd.DataFrame] | None = None,
    expected_additional_files: Iterable[str] | None = None,
) -> Dict[str, Any]:
    """Validate the exact serialized DwC-DP archive before it is uploaded."""
    errors: list[str] = []
    warnings: list[str] = []
    expected_resources = expected_resources or {}
    expected_additional = {
        _safe_archive_name(filename) for filename in (expected_additional_files or [])
    }

    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    errors.append(f"Archive contains an unsafe path: {name!r}.")

            for required_name in {"datapackage.json", "eml.xml", *expected_additional}:
                if required_name not in names:
                    errors.append(f"Archive is missing required file '{required_name}'.")

            if "datapackage.json" not in names or "eml.xml" not in names:
                return {"valid": False, "errors": errors, "warnings": warnings}

            try:
                descriptor = json.loads(
                    archive.extractfile("datapackage.json").read().decode("utf-8", "strict")
                )
            except Exception as exc:
                errors.append(f"datapackage.json cannot be parsed as strict UTF-8 JSON: {exc}.")
                descriptor = {}

            try:
                ET.fromstring(archive.extractfile("eml.xml").read().decode("utf-8", "strict"))
            except Exception as exc:
                errors.append(f"eml.xml cannot be parsed as strict UTF-8 XML: {exc}.")

            errors.extend(validate_datapackage_descriptor(descriptor))
            serialized_resources: Dict[str, pd.DataFrame] = {}
            for resource in descriptor.get("resources") or []:
                resource_name = str(resource.get("name") or "")
                resource_path = str(resource.get("path") or "")
                if not resource_name or not resource_path:
                    errors.append("Descriptor contains a resource without a name or path.")
                    continue
                if resource_path not in names:
                    errors.append(
                        f"Descriptor resource '{resource_name}' references missing file '{resource_path}'."
                    )
                    continue
                try:
                    text = archive.extractfile(resource_path).read().decode("utf-8", "strict")
                    rows = list(csv.reader(io.StringIO(text, newline="")))
                except Exception as exc:
                    errors.append(
                        f"Resource '{resource_name}' cannot be read as strict UTF-8 CSV: {exc}."
                    )
                    continue
                if not rows:
                    errors.append(f"Resource '{resource_name}' has no CSV header.")
                    continue
                header = rows[0]
                malformed_rows = [
                    index
                    for index, row in enumerate(rows[1:], start=2)
                    if len(row) != len(header)
                ]
                if malformed_rows:
                    preview = ", ".join(map(str, malformed_rows[:10]))
                    errors.append(
                        f"Resource '{resource_name}' has rows with a different field count "
                        f"from its header: {preview}."
                    )
                    continue
                serialized_df = pd.DataFrame(rows[1:], columns=header)
                serialized_resources[resource_name] = serialized_df

                expected_df = expected_resources.get(resource_name)
                if expected_df is not None:
                    if list(expected_df.columns) != header:
                        errors.append(
                            f"Resource '{resource_name}' serialized headers differ from the source table."
                        )
                    if len(expected_df) != len(serialized_df):
                        errors.append(
                            f"Resource '{resource_name}' serialized {len(serialized_df)} rows; "
                            f"expected {len(expected_df)}."
                        )

            serialized_validation = validate_dwc_dp_resources(serialized_resources)
            errors.extend(serialized_validation["errors"])
            warnings.extend(serialized_validation["warnings"])
    except Exception as exc:
        errors.append(f"DwC-DP archive cannot be opened: {exc}.")

    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
    }


def create_dwc_dp_archive(
    output_path: Path,
    resources: Mapping[str, pd.DataFrame],
    title: str,
    description: str,
    user=None,
    eml_extra: dict | None = None,
    dataset_id: str | None = None,
    version: str | None = None,
    additional_files: Iterable[tuple[str, bytes]] | None = None,
) -> Dict[str, Any]:
    descriptor = build_datapackage_descriptor(
        resources,
        dataset_id=dataset_id,
        title=title,
        description=description,
        version=version,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        package_root = Path(temp_dir) / "package"
        package_root.mkdir()
        (package_root / "datapackage.json").write_text(
            json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (package_root / "eml.xml").write_text(
            make_eml(title, description, user, eml_extra),
            encoding="utf-8",
        )
        for raw_name, df in resources.items():
            name = normalize_resource_name(raw_name)
            _write_csv(df, package_root / f"{name}.csv")

        additional_names = []
        for filename, content in additional_files or []:
            safe_name = _safe_archive_name(filename)
            target = package_root / safe_name
            if target.exists():
                raise ValueError(f"Ancillary file '{safe_name}' conflicts with a package file.")
            target.write_bytes(content)
            additional_names.append(safe_name)

        with tarfile.open(output_path, "w:gz") as archive:
            for child in sorted(package_root.iterdir()):
                archive.add(child, arcname=child.name)

    archive_validation = validate_dwc_dp_archive(
        output_path,
        expected_resources={
            normalize_resource_name(name): df for name, df in resources.items()
        },
        expected_additional_files=additional_names,
    )
    if not archive_validation["valid"]:
        raise ValueError(
            "Serialized DwC-DP archive validation failed: "
            + "; ".join(archive_validation["errors"])
        )
    return descriptor


def export_dwc_dp_package(
    resources: Mapping[str, pd.DataFrame],
    title: str,
    description: str,
    user=None,
    eml_extra: dict | None = None,
    dataset_id: str | None = None,
    version: str | None = None,
    additional_files: Iterable[tuple[str, bytes]] | None = None,
) -> str:
    from minio import Minio

    file_name = datetime.now().strftime("dwc-dp-%Y-%m-%d-%H%M%S") + ".tar.gz"
    with tempfile.TemporaryDirectory() as temp_dir:
        local_path = Path(temp_dir) / file_name
        create_dwc_dp_archive(
            local_path,
            resources,
            title,
            description,
            user=user,
            eml_extra=eml_extra,
            dataset_id=dataset_id,
            version=version,
            additional_files=additional_files,
        )

        client = Minio(
            os.getenv("MINIO_URI"),
            access_key=os.getenv("MINIO_ACCESS_KEY"),
            secret_key=os.getenv("MINIO_SECRET_KEY"),
        )
        object_name = f"{os.getenv('MINIO_BUCKET_FOLDER')}/{file_name}"
        upload_file(
            client,
            os.getenv("MINIO_BUCKET"),
            object_name,
            str(local_path),
            content_type="application/gzip",
        )
        return f"https://{os.getenv('MINIO_URI')}/{os.getenv('MINIO_BUCKET')}/{object_name}"
