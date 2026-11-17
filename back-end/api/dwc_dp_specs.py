from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import tarfile
import tempfile
import threading
from collections import Counter, OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping

import numpy as np
import pandas as pd
from frictionless import Package, Schema
from jsonschema.exceptions import best_match
from jsonschema.validators import validator_for
from lxml import etree
from referencing import Registry, Resource

from api.helpers.publish import make_eml, upload_file
from api.publication_validation import (
    semantic_dwc_dp_warnings,
    utf8_serialization_errors,
    validate_publication_safety,
)


_BASE_DIR = Path(__file__).resolve().parent
_DWC_DP_ROOT = _BASE_DIR / "templates" / "dwc-dp"
_TABLE_SCHEMA_ROOT = _DWC_DP_ROOT / "table-schemas"
# The DwC-DP profile extends Frictionless Data Package v1 by URL; resolve it locally.
_FRICTIONLESS_DATA_PACKAGE_SCHEMA = _BASE_DIR / "templates" / "frictionless" / "data-package-v1.json"
FRICTIONLESS_DATA_PACKAGE_SCHEMA_URL = "https://specs.frictionlessdata.io/schemas/data-package.json"
# EML 2.2.0 XSDs, vendored from gbif/dwc-dp-analyser (the same schemas GBIF's
# DwC-DP analyser validates eml.xml against).
_EML_XSD = _BASE_DIR / "templates" / "xsd" / "eml-2.2.0" / "eml.xsd"
# Cap on reported errors per resource or file, so one systematic problem stays readable.
_REPORTED_ERRORS_PER_SOURCE = 10

# rs.tdwg.org master commit that published DwC-DP 1.0 (Darwin Core 2026-10-13 release).
DWC_DP_SCHEMA_REVISION = "88a4b91f191849cd6dfb1aad78ed0510f1a47823"
DWC_DP_SCHEMA_REPOSITORY = "https://github.com/tdwg/rs.tdwg.org"

# TDWG's deployed DwC-DP 1.0 profile. Descriptors also record the immutable
# source revision and vendored content hash for reproducibility.
DWC_DP_PROFILE_URL = "https://rs.tdwg.org/dwc-dp/1.0/dwc-dp-profile.json"


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
        "source": f"{DWC_DP_SCHEMA_REPOSITORY}/tree/{DWC_DP_SCHEMA_REVISION}/dwc-dp/1.0",
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


# Fields the package explorer joins on: relationship fields of every DwC-DP table and the
# columns the taxonomy conversion links on.
TAXONOMY_JOIN_FIELDS = ("archive_taxon_id", "archive_join_id", "occurrence_fk", "occurrence_pk")
_join_field_names: set[str] | None = None


def explorer_join_field_names() -> set[str]:
    global _join_field_names
    if _join_field_names is None:
        names = set(TAXONOMY_JOIN_FIELDS)
        for spec in TABLE_SPECS.values():
            for relationship in [*spec.foreign_keys, *spec.weak_foreign_keys]:
                names.update(_as_field_list(relationship.get("fields")))
                names.update(_as_field_list((relationship.get("reference") or {}).get("fields")))
        _join_field_names = names
    return _join_field_names


JOIN_HASH_CACHE_SECONDS = 24 * 60 * 60
# Memory bounds for relationship coverage. The in-process cache of per-table join hashes holds
# at most JOIN_HASH_CACHE_BYTES (entries over JOIN_HASH_ENTRY_BYTES are not cached), and one
# build holds at most JOIN_HASH_BUILD_BYTES of hashes at a time; a relationship that would
# need more is reported with coverage "skipped" instead of counts.
JOIN_HASH_CACHE_BYTES = 128 * 2 ** 20
JOIN_HASH_ENTRY_BYTES = 32 * 2 ** 20
JOIN_HASH_BUILD_BYTES = 256 * 2 ** 20


def _entry_bytes(entry: Dict[str, Any]) -> int:
    return sum(hashes.nbytes + filled.nbytes for hashes, filled in entry["columns"].values())


class JoinHashCache:
    """In-process LRU of per-table join hashes, one entry per table, bounded by bytes and age.

    Entries are kept as read-only arrays (no serialised copy, unlike the Django cache)."""

    def __init__(self, max_bytes=JOIN_HASH_CACHE_BYTES, max_entry_bytes=JOIN_HASH_ENTRY_BYTES, ttl=JOIN_HASH_CACHE_SECONDS):
        self.max_bytes, self.max_entry_bytes, self.ttl = max_bytes, max_entry_bytes, ttl
        self._entries: "OrderedDict[int, tuple]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, table_id: int, version: str):
        import time

        with self._lock:
            item = self._entries.get(table_id)
            if item is None:
                return None
            stored_at, stored_version, entry, size = item
            if stored_version != version or time.monotonic() - stored_at > self.ttl:
                self._drop(table_id)  # a superseded version is released, not kept beside the new one
                return None
            self._entries.move_to_end(table_id)
            return entry

    def set(self, table_id: int, version: str, entry: Dict[str, Any]) -> None:
        import time

        size = _entry_bytes(entry)
        with self._lock:
            if table_id in self._entries:
                self._drop(table_id)
            if size > self.max_entry_bytes:
                return
            for hashes, filled in entry["columns"].values():
                hashes.setflags(write=False)
                filled.setflags(write=False)
            self._entries[table_id] = (time.monotonic(), version, entry, size)
            self._bytes += size
            while self._bytes > self.max_bytes:
                self._drop(next(iter(self._entries)))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def _drop(self, table_id: int) -> None:
        self._bytes -= self._entries.pop(table_id)[3]


join_hash_cache = JoinHashCache()


def _join_column_hashes(df: pd.DataFrame) -> Dict[str, Any]:
    """{'rows': n, 'columns': {name: (uint64 hash per row, bool filled per row)}} for the join
    columns of one table. Cell text is compared as ``_explorer_join_value`` reads it."""
    columns = {}
    for name in df.columns:
        if name not in explorer_join_field_names() or name in columns:
            continue
        series = df[name]
        if isinstance(series, pd.DataFrame):  # duplicate labels: the first, as df[fields] would
            series = series.iloc[:, 0]
        texts = [_explorer_join_value(value) for value in series.tolist()]
        filled = np.fromiter((text is not None for text in texts), dtype=bool, count=len(texts))
        hashes = pd.util.hash_array(np.asarray([text or "" for text in texts], dtype=object)) if texts else np.array([], dtype=np.uint64)
        columns[str(name)] = (hashes, filled)
    return {"rows": int(len(df.index)), "columns": columns}


def _join_estimate(row_count: Any, columns: Iterable[Any]) -> int:
    """Bytes of join hashes a table needs, from stored metadata: 9 per row per join column."""
    join_columns = sum(1 for column in (columns or []) if column in explorer_join_field_names())
    return 9 * int(row_count or 0) * join_columns


def explorer_join_hashes(table_id: int, updated_at: Any, load_snapshot, fits=None) -> Dict[str, Any] | None:
    """The join-column hashes of one table, cached with its version so saving one table does
    not re-read the others.

    On a miss ``load_snapshot()`` returns a table with ``df``, ``updated_at``, ``row_count`` and
    ``columns`` read in one query, so the hashes are filed under the version they were computed
    from even if the table was saved meanwhile. ``fits(row_count, columns)`` can refuse that
    snapshot (None is returned) before any hashing; the DataFrame is released straight away.
    """
    version = updated_at.isoformat() if updated_at else ""
    entry = join_hash_cache.get(table_id, version)
    if entry is not None:
        return entry
    snapshot = load_snapshot()
    if fits is not None and not fits(snapshot.row_count, snapshot.columns):
        del snapshot
        return None
    version = snapshot.updated_at.isoformat() if snapshot.updated_at else ""
    df = snapshot.df
    del snapshot
    entry = {"version": version, **_join_column_hashes(df)}
    del df
    join_hash_cache.set(table_id, version, entry)
    return entry


_HASH_MIX = np.uint64(0x9E3779B97F4A7C15)


def _explorer_join_hashes(entry: Dict[str, Any], fields: list[str]) -> "np.ndarray | None":
    """64-bit hashes of the populated join keys (rows with every field filled); None when a
    field is missing or the table has no rows."""
    if not fields or entry["rows"] == 0 or any(field not in entry["columns"] for field in fields):
        return None
    combined = None
    filled = np.ones(entry["rows"], dtype=bool)
    with np.errstate(over="ignore"):
        for field in fields:
            hashes, column_filled = entry["columns"][field]
            filled &= column_filled
            combined = hashes.copy() if combined is None else (combined * _HASH_MIX) ^ hashes
    return combined[filled]


def _edges_with_coverage(dataset: Any, pending: list) -> list[Dict[str, Any]]:
    """Fill in linked/unmatched/blank counts for each collected relationship, in order.

    A relationship whose fields are missing (or whose tables have no rows) is left out, as
    before. Each table's hashes are held only while a later relationship still needs them, and
    never more than JOIN_HASH_BUILD_BYTES at once: past that, coverage is "skipped" (counts are
    null), which the UI shows as not checked rather than as zero matches.
    """
    remaining = Counter()
    for _edge, source_table, _sf, target_table, _tf in pending:
        remaining[source_table["id"]] += 1
        remaining[target_table["id"]] += 1
    held: Dict[int, Dict[str, Any]] = {}
    held_bytes = 0
    edges = []

    def entry_for(table: Dict[str, Any]):
        nonlocal held_bytes
        table_id = table["id"]
        if table_id in held:
            return held[table_id]
        # Checked from stored metadata before anything is read, and again on the snapshot that
        # is actually read (the table may have grown since the metadata was fetched).
        if held_bytes + _join_estimate(table["row_count"], table["columns"]) > JOIN_HASH_BUILD_BYTES:
            return None
        entry = explorer_join_hashes(
            table_id, table.get("updated_at"),
            lambda: dataset.table_set.only("df", "updated_at", "row_count", "columns").get(id=table_id),
            fits=lambda rows, columns: held_bytes + _join_estimate(rows, columns) <= JOIN_HASH_BUILD_BYTES,
        )
        if entry is None:
            return None
        size = _entry_bytes(entry)
        if held_bytes + size > JOIN_HASH_BUILD_BYTES:
            return None
        held[table_id] = entry
        held_bytes += size
        return entry

    def coverage_of(edge, source_table, source_fields, target_table, target_fields):
        """The edge with its coverage, or None to leave it out. Its arrays are local to this call,
        so nothing outlives the relationship once its tables are released."""
        source_entry = entry_for(source_table)
        target_entry = entry_for(target_table) if source_entry is not None else None
        if source_entry is None or target_entry is None:
            # Columns are known without hashes: leave out relationships that cannot apply.
            available = set(source_table["columns"]), set(target_table["columns"])
            if source_table["row_count"] and target_table["row_count"] and \
                    set(source_fields) <= available[0] and set(target_fields) <= available[1]:
                return {**edge, "coverage": "skipped", "populatedRows": None, "linkedRows": None,
                        "unmatchedRows": None, "blankRows": None}
            return None
        source = _explorer_join_hashes(source_entry, source_fields)
        target = _explorer_join_hashes(target_entry, target_fields)
        if source is None or target is None:
            return None
        populated_rows = len(source)
        linked_rows = int(np.isin(source, target).sum())
        # Every count comes from the same snapshot of the source table (it may have been saved
        # since the metadata was read), so they always add up.
        source_rows = int(source_entry["rows"])
        return {**edge, "coverage": "checked", "sourceRows": source_rows, "populatedRows": populated_rows,
                "linkedRows": linked_rows, "unmatchedRows": populated_rows - linked_rows,
                "blankRows": source_rows - populated_rows}

    def release(table: Dict[str, Any]) -> None:
        nonlocal held_bytes
        table_id = table["id"]
        remaining[table_id] -= 1
        if remaining[table_id] <= 0 and table_id in held:
            held_bytes -= _entry_bytes(held.pop(table_id))

    for edge, source_table, source_fields, target_table, target_fields in pending:
        result = coverage_of(edge, source_table, source_fields, target_table, target_fields)
        if result is not None:
            edges.append(result)
        release(source_table)
        release(target_table)
    return edges


def build_dwc_dp_explorer_model(dataset: Any) -> Dict[str, Any]:
    """Build the explorer model for standard and converted taxonomy resources."""
    resource_tables = {
        table["title"]: table
        for table in dataset.table_set.filter(title__in=RESERVED_TABLE_NAMES)
        .order_by("title", "id")
        .values("id", "title", "row_count", "columns", "updated_at")
    }
    nodes: list[Dict[str, Any]] = []
    edges: list[Dict[str, Any]] = []

    # Relationships are collected first and their coverage computed afterwards, so each table's
    # join hashes (read at most once per version, the DataFrame released straight away) are
    # dropped as soon as no remaining relationship needs them.
    pending: list[tuple[Dict[str, Any], Dict[str, Any], list[str], Dict[str, Any], list[str]]] = []

    def add_edge(edge: Dict[str, Any], source_table, source_fields, target_table, target_fields) -> None:
        pending.append((edge, source_table, source_fields, target_table, target_fields))

    for name, table in resource_tables.items():
        spec = TABLE_SPECS[name]
        available_fields = set(table["columns"])
        fields = []
        for field_name in table["columns"]:
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
                "tableId": table["id"],
                "title": spec.title,
                "description": spec.description,
                "comments": spec.schema.get("notes") or "",
                "examples": spec.schema.get("examples") or "",
                "rowCount": table["row_count"],
                "updatedAt": table["updated_at"].isoformat().replace("+00:00", "Z") if table["updated_at"] else "",
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
            predicate = (
                relationship.get("predicate")
                or relationship.get("relationship")
                or relationship.get("predicateLabel")
                or "relates to"
            )
            add_edge(
                {
                    "id": f"{source_name}:{kind}:{index}:{target_name}",
                    "source": source_name,
                    "target": target_name,
                    "predicate": predicate,
                    "kind": kind,
                    "sourceFields": source_fields,
                    "targetFields": target_fields,
                    "sourceRows": source_table["row_count"],
                },
                source_table, source_fields, target_table, target_fields,
            )

    conversion = getattr(dataset, "conversion", None) if dataset.workflow_type == dataset.WorkflowType.DWCA_CONVERSION else None
    if conversion and conversion.status == "complete" and "taxonomy" in conversion.plan:
        taxonomy_tables = {
            table["title"]: table
            for table in dataset.table_set.filter(title__startswith="taxonomy-")
            .order_by("title", "id")
            .values("id", "title", "row_count", "columns", "updated_at")
        }
        source_tables = conversion.plan.get("tables", [])
        for name, table in taxonomy_tables.items():
            if name == "taxonomy-taxon":
                source = next((item for item in source_tables if item.get("core")), {})
                primary_key = "archive_join_id"
            elif name.startswith("taxonomy-extension-") and name.removeprefix("taxonomy-extension-").isdigit():
                index = int(name.removeprefix("taxonomy-extension-"))
                source = source_tables[index] if index < len(source_tables) else {}
                primary_key = "source_row_id"
            else:
                source = {}
                primary_key = ""
            source_name = source.get("name")
            fields = [{"name": field, "title": str(field).rsplit("/", 1)[-1],
                       "description": "Source value copied into the package." if "://" in str(field) else "",
                       "type": "string", "primary": field == primary_key, "weakPrimary": False}
                      for field in table["columns"]]
            nodes.append({"id": name, "tableId": table["id"],
                          "title": source_name or name.replace("-", " ").title(),
                          "description": (f"Original {source_name} table retained in the package."
                                          if source_name else "Explicit links between checklist and occurrence records."),
                          "comments": "", "examples": "", "rowCount": table["row_count"],
                          "updatedAt": table["updated_at"].isoformat().replace("+00:00", "Z") if table["updated_at"] else "",
                          "columnCount": len(table["columns"]), "primaryKey": [primary_key] if primary_key else [],
                          "weakPrimaryKey": [], "fields": fields})

        def add_taxonomy_link(source_name, target_name, source_field, target_field, predicate):
            source_table = taxonomy_tables.get(source_name)
            target_table = taxonomy_tables.get(target_name) or resource_tables.get(target_name)
            if not source_table or not target_table:
                return
            add_edge({"id": f"{source_name}:foreign:{target_name}",
                      "source": source_name, "target": target_name, "predicate": predicate,
                      "kind": "foreign", "sourceFields": [source_field], "targetFields": [target_field],
                      "sourceRows": source_table["row_count"]},
                     source_table, [source_field], target_table, [target_field])

        for name, table in taxonomy_tables.items():
            if name != "taxonomy-taxon" and "archive_taxon_id" in table["columns"]:
                add_taxonomy_link(name, "taxonomy-taxon", "archive_taxon_id", "archive_join_id", "belongs to taxon")
        if "taxonomy-occurrence-links" in taxonomy_tables:
            add_taxonomy_link("taxonomy-occurrence-links", "occurrence", "occurrence_fk", "occurrence_pk", "identifies occurrence")

    edges.extend(_edges_with_coverage(dataset, pending))
    return {
        "schema": dwc_dp_schema_snapshot(),
        "packageType": "taxonomy-data-package" if conversion and conversion.report.get("output_format") == "taxonomy-data-package" else "dwc-dp",
        "nodes": nodes,
        "edges": edges,
    }


EXPLORER_MODEL_CACHE_SECONDS = 60 * 60


def explorer_model_cache_key(dataset: Any) -> str:
    """Changes whenever a package or taxonomy table, or the dataset's conversion, changes."""
    from django.db.models import Q

    digest = hashlib.sha256(f"{dataset.pk}|{dataset.workflow_type}\n".encode())
    tables = (
        dataset.table_set.filter(Q(title__in=RESERVED_TABLE_NAMES) | Q(title__startswith="taxonomy-"))
        .order_by("id")
        .values_list("id", "title", "updated_at")
    )
    for table_id, title, updated_at in tables:
        digest.update(f"t{table_id}|{title}|{updated_at.isoformat() if updated_at else ''}\n".encode())
    if dataset.workflow_type == dataset.WorkflowType.DWCA_CONVERSION:
        from api.models import DwcConversion

        conversion = DwcConversion.objects.filter(dataset=dataset).only("id", "status", "updated_at").first()
        if conversion:
            digest.update(f"c{conversion.pk}|{conversion.status}|{conversion.updated_at.isoformat()}\n".encode())
    return f"dwc-dp-explorer-model:v1:{dataset.pk}:{digest.hexdigest()[:32]}"


def cached_dwc_dp_explorer_model(dataset: Any) -> Dict[str, Any]:
    """The explorer model, cached per version of the tables it reads.

    The dataset page asks for it on every load and after table saves; relationship coverage
    reads whole tables, so it is computed once per version (and once per process when
    requests arrive together).
    """
    from django.core.cache import cache

    from api import column_stats

    key = explorer_model_cache_key(dataset)
    model = cache.get(key)
    if model is not None:
        return model
    with column_stats.single_flight(key):
        model = cache.get(key)
        if model is None:
            model = build_dwc_dp_explorer_model(dataset)
            cache.set(key, model, EXPLORER_MODEL_CACHE_SECONDS)
    return model


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
            if pd.api.types.is_float_dtype(series):
                # pandas stores integer columns with blanks as floats; _serializable_resource
                # writes whole-number floats as integers.
                non_integer = populated & ((numeric % 1) != 0)
            else:
                # Frictionless needs plain digits: text such as '12.0' or '1e3' is not an integer.
                non_integer = populated & ~_series_text(series).str.fullmatch(r"[+-]?\d+")
            if non_integer.any():
                errors.append(
                    f"Resource '{resource_name}' field '{field_name}' must contain integer values "
                    f"written as whole numbers without a decimal point or exponent; "
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


@lru_cache(maxsize=None)
def _incoming_links(target_name: str) -> tuple[tuple[str, Dict[str, Any], bool], ...]:
    """Keys from other resources that point at a shared resource, excluding its own satellites."""
    links = []
    for source_name in sorted(RESERVED_TABLE_NAMES):
        # agent-identifier, protocol-reference and similar describe the entity itself.
        if source_name == target_name or source_name.startswith(f"{target_name}-"):
            continue
        spec = get_table_spec(source_name)
        for foreign_key, weak in [(key, False) for key in spec.foreign_keys] + [
            (key, True) for key in spec.weak_foreign_keys
        ]:
            if (foreign_key.get("reference") or {}).get("resource") == target_name:
                links.append((source_name, foreign_key, weak))
    return tuple(links)


def _unlinked_shared_rows(
    resources: Mapping[str, pd.DataFrame],
    valid_columns_by_resource: Mapping[str, set[str]],
) -> list[str]:
    """Warn about rows of shared resources (agent, protocol, ...) that no data resource uses."""
    warnings = []
    relationship = resources.get("resource-relationship")
    relationship_fields = [
        field for field in ("subjectResourceID", "relatedResourceID")
        if field in valid_columns_by_resource.get("resource-relationship", set())
    ]
    relationship_ids: set[str] = set()
    for field in relationship_fields:
        relationship_ids.update(_series_text(relationship[field]))
    relationship_ids.discard("")

    for target_name, target_df in resources.items():
        spec = get_table_spec(target_name)
        if any(
            (key.get("reference") or {}).get("resource") not in ("", target_name)
            for key in spec.foreign_keys
        ):
            continue  # it links itself to the data
        links = _incoming_links(target_name)
        primary_key = spec.primary_key
        target_columns = valid_columns_by_resource[target_name]
        if not links or len(primary_key) != 1 or primary_key[0] not in target_columns:
            continue

        referenced: Dict[str, set[str]] = {}
        for source_name, foreign_key, weak in links:
            source_df = resources.get(source_name)
            source_fields = _as_field_list(foreign_key.get("fields"))
            target_fields = _as_field_list((foreign_key.get("reference") or {}).get("fields"))
            if (
                source_df is None
                or len(source_fields) != 1
                or len(target_fields) != 1
                or source_fields[0] not in valid_columns_by_resource[source_name]
                or target_fields[0] not in target_columns
            ):
                continue
            values = referenced.setdefault(target_fields[0], set())
            for value in _series_text(source_df[source_fields[0]]):
                # Weak ID fields such as identifiedByID may list several agents.
                parts = value.split("|") if weak else [value]
                values.update(part.strip() for part in parts if part.strip())

        # Generic relationship endpoints are polymorphic, so canonical schemas cannot
        # declare their target table. They can name either its local key or canonical ID.
        for identifier in (primary_key, spec.weak_primary_key):
            if len(identifier) == 1 and identifier[0] in target_columns:
                referenced.setdefault(identifier[0], set()).update(relationship_ids)

        keys = _series_text(target_df[primary_key[0]])
        linked = pd.Series(False, index=target_df.index)
        for field_name, values in referenced.items():
            linked |= _series_text(target_df[field_name]).isin(values)
        unlinked = keys[(keys != "") & ~linked]
        if unlinked.empty:
            continue
        examples = ", ".join(unlinked.head(5))
        suffix = f" and {len(unlinked) - 5} more" if len(unlinked) > 5 else ""
        # Suggest fields on resources already in the package, then their enforced link tables.
        routes = sorted({
            f"{source_name}.{_as_field_list(key.get('fields'))[0]}"
            if source_name in resources else source_name
            for source_name, key, weak in links
            if source_name in resources
            or (not weak and any(source_name.startswith(f"{name}-") for name in resources))
        })[:8]
        warnings.append(
            f"Semantic review: resource '{target_name}' has {len(unlinked)} row(s) that no other "
            f"resource references ({primary_key[0]}: {examples}{suffix}). A shared entity should be "
            "linked to the records it describes"
            + (f", for example through {', '.join(routes)}" if routes else "")
            + ". Link each row, or remove it if the source gives it no role."
        )
    return warnings


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
    warnings.extend(_unlinked_shared_rows(normalized_resources, valid_columns_by_resource))
    warnings.extend(validate_publication_safety(normalized_resources))
    warnings.extend(semantic_dwc_dp_warnings(normalized_resources))

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "resources": sorted(normalized_resources.keys()),
        "schema": dwc_dp_schema_snapshot(),
    }


def _declared_columns(table_name: str, df: pd.DataFrame) -> list[str]:
    descriptors = get_table_spec(table_name).field_descriptors
    return [column for column in df.columns if isinstance(column, str) and column in descriptors]


def _resource_schema_for_dataframe(
    table_name: str,
    df: pd.DataFrame,
    included_resources: Mapping[str, Iterable[str]],
) -> Dict[str, Any]:
    """``included_resources`` maps each packaged resource to the fields it declares."""
    spec = get_table_spec(table_name)
    descriptors = spec.field_descriptors
    columns = _declared_columns(table_name, df)
    schema: Dict[str, Any] = {
        "fields": [deepcopy(descriptors[column]) for column in columns],
    }

    for property_name, key_fields in (
        ("primaryKey", spec.primary_key),
        ("weakPrimaryKey", spec.weak_primary_key),
    ):
        if key_fields and all(field in columns for field in key_fields):
            schema[property_name] = key_fields[0] if len(key_fields) == 1 else key_fields

    included = {name: set(fields) for name, fields in included_resources.items()}
    included[table_name] = set(columns)
    for property_name, foreign_keys in (
        ("foreignKeys", spec.foreign_keys),
        ("weakForeignKeys", spec.weak_foreign_keys),
    ):
        included_keys = []
        for foreign_key in foreign_keys:
            source_fields = _as_field_list(foreign_key.get("fields"))
            reference = foreign_key.get("reference") or {}
            target_name = str(reference.get("resource") or table_name)
            # A key is declared only when both ends exist: an empty *ByID column
            # can sit beside an agent table whose name-only rows have no agentID.
            target_fields = _as_field_list(reference.get("fields"))
            if (all(field in columns for field in source_fields) and target_name in included
                    and all(field in included[target_name] for field in target_fields)):
                included_keys.append(deepcopy(foreign_key))
        if included_keys:
            schema[property_name] = included_keys

    return schema


@lru_cache(maxsize=None)
def _dwc_dp_profile_validator():
    """The vendored DwC-DP profile, with its base Data Package $ref resolved offline."""
    profile = _load_json(_DWC_DP_ROOT / "dwc-dp-profile.json")
    registry = Registry().with_resource(
        FRICTIONLESS_DATA_PACKAGE_SCHEMA_URL,
        Resource.from_contents(_load_json(_FRICTIONLESS_DATA_PACKAGE_SCHEMA)),
    )
    return validator_for(profile)(profile, registry=registry)


def _dwc_dp_profile_errors(descriptor: Mapping[str, Any]) -> list[str]:
    errors = []
    for error in _dwc_dp_profile_validator().iter_errors(deepcopy(dict(descriptor))):
        # A oneOf failure's own message repeats the whole resource; report the branch error.
        error = best_match([error])
        location = "/" + "/".join(str(part) for part in error.absolute_path)
        message = error.message if len(error.message) <= 300 else error.message[:300] + "..."
        errors.append(f"Descriptor does not match the DwC-DP profile at '{location}': {message}")
    return errors


# Field properties that must match the canonical DwC-DP table schema. The first
# group must be present; the second is only compared when declared.
_CANONICAL_FIELD_PROPERTIES = ("title", "description", "type", "dcterms:isVersionOf")
_OPTIONAL_CANONICAL_FIELD_PROPERTIES = ("format", "namespace", "dcterms:references")


def _foreign_key_signature(foreign_key: Mapping[str, Any]) -> tuple:
    reference = foreign_key.get("reference") or {}
    return (
        tuple(_as_field_list(foreign_key.get("fields"))),
        str(foreign_key.get("predicate") or ""),
        str(reference.get("resource") or ""),
        tuple(_as_field_list(reference.get("fields"))),
    )


def _canonical_table_schema_errors(
    name: str,
    schema: Mapping[str, Any],
    fields_by_resource: Mapping[str, set[str]],
) -> list[str]:
    """Compare a reserved resource's schema with its canonical DwC-DP table schema."""
    errors: list[str] = []
    spec = get_table_spec(name)
    canonical_fields = spec.field_descriptors
    fields = [field for field in schema.get("fields") or [] if isinstance(field, Mapping)]
    field_names = {str(field.get("name") or "") for field in fields}

    counts = Counter(str(field.get("name") or "") for field in fields)
    for field_name in sorted(field_name for field_name, count in counts.items() if count > 1):
        errors.append(f"Resource '{name}' declares field '{field_name}' more than once.")

    for field_name, canonical in canonical_fields.items():
        if (canonical.get("constraints") or {}).get("required") is True and field_name not in field_names:
            errors.append(f"Resource '{name}' schema is missing required field '{field_name}'.")

    for field in fields:
        field_name = str(field.get("name") or "<unnamed>")
        missing = [key for key in ("name", *_CANONICAL_FIELD_PROPERTIES) if key not in field]
        if missing:
            errors.append(
                f"Resource '{name}' field '{field_name}' is missing metadata: {', '.join(missing)}."
            )
        canonical = canonical_fields.get(field_name)
        if canonical is None:
            errors.append(
                f"Resource '{name}' field '{field_name}' is not defined by its DwC-DP table schema."
            )
            continue
        for key in _CANONICAL_FIELD_PROPERTIES + _OPTIONAL_CANONICAL_FIELD_PROPERTIES:
            if key in field and field[key] != canonical.get(key):
                errors.append(
                    f"Resource '{name}' field '{field_name}' declares {key} {field[key]!r}, but the "
                    f"DwC-DP table schema defines {canonical.get(key)!r}."
                )

    canonical_keys = {
        _foreign_key_signature(foreign_key)
        for foreign_key in spec.foreign_keys + spec.weak_foreign_keys
    }
    for property_name in ("foreignKeys", "weakForeignKeys"):
        for foreign_key in schema.get(property_name) or []:
            if not isinstance(foreign_key, Mapping):
                continue
            source_fields, _predicate, target_name, target_fields = _foreign_key_signature(foreign_key)
            label = f"{property_name} '{', '.join(source_fields)}'"
            for field_name in source_fields:
                if field_name not in field_names:
                    errors.append(f"Resource '{name}' {label} uses undeclared field '{field_name}'.")
            target_name = target_name or name
            target_field_names = fields_by_resource.get(target_name)
            if target_field_names is None:
                errors.append(
                    f"Resource '{name}' {label} references resource '{target_name}', which is not in "
                    "the package."
                )
            else:
                for field_name in target_fields:
                    if field_name not in target_field_names:
                        errors.append(
                            f"Resource '{name}' {label} references field '{field_name}', which "
                            f"resource '{target_name}' does not declare."
                        )
            if _foreign_key_signature(foreign_key) not in canonical_keys:
                errors.append(
                    f"Resource '{name}' {label} does not match a relationship in its DwC-DP table schema."
                )
    return errors


def validate_datapackage_descriptor(descriptor: Mapping[str, Any], *, allow_generic: bool = False) -> list[str]:
    """Validate generated metadata without fetching the currently undeployed profile URI."""
    errors: list[str] = []
    generic = allow_generic and descriptor.get('profile') == 'data-package'
    if descriptor.get("profile") != DWC_DP_PROFILE_URL and not generic:
        errors.append(f"Descriptor profile must be '{DWC_DP_PROFILE_URL}'.")
    if descriptor.get("dwcDpSchema") != dwc_dp_schema_snapshot():
        errors.append("Descriptor must identify the exact vendored DwC-DP schema snapshot.")
    if generic:
        base_schema = _load_json(_FRICTIONLESS_DATA_PACKAGE_SCHEMA)
        errors.extend(f'Data Package descriptor: {error.message}'
                      for error in validator_for(base_schema)(base_schema).iter_errors(dict(descriptor)))
    else:
        errors.extend(_dwc_dp_profile_errors(descriptor))

    frictionless_descriptor = deepcopy(dict(descriptor))
    frictionless_descriptor["profile"] = "data-package"
    try:
        Package.from_descriptor(frictionless_descriptor)
    except Exception as exc:
        errors.append(f"Descriptor is not a valid Frictionless Data Package: {exc}")

    resources = [
        resource for resource in descriptor.get("resources") or [] if isinstance(resource, Mapping)
    ]
    fields_by_resource = {
        str(resource.get("name")): {
            str(field.get("name") or "")
            for field in (resource.get("schema") or {}).get("fields") or []
            if isinstance(field, Mapping)
        }
        for resource in resources
    }
    for resource in resources:
        name = resource.get("name")
        if name in RESERVED_TABLE_NAMES:
            if resource.get("profile") != "tabular-data-resource":
                errors.append(f"Reserved resource '{name}' must use the tabular-data-resource profile.")
            schema = resource.get("schema") or {}
            try:
                Schema.from_descriptor(schema)
            except Exception as exc:
                errors.append(f"Resource '{name}' has an invalid Frictionless table schema: {exc}")
            errors.extend(_canonical_table_schema_errors(name, schema, fields_by_resource))
    return errors


def build_datapackage_descriptor(
    resources: Mapping[str, pd.DataFrame],
    dataset_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    version: str | None = None,
    additional_tables: Mapping[str, Mapping[str, Any]] | None = None,
    descriptor_metadata: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    additional_tables = additional_tables or {}
    validation = validate_dwc_dp_resources(resources) if resources or not additional_tables else {'valid': True}
    if not validation["valid"]:
        raise ValueError("DwC-DP validation failed: " + "; ".join(validation["errors"]))

    names = {normalize_resource_name(name): _declared_columns(normalize_resource_name(name), df)
             for name, df in resources.items()}
    descriptor: Dict[str, Any] = {
        "profile": DWC_DP_PROFILE_URL if resources else 'data-package',
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
    for key, value in (descriptor_metadata or {}).items():
        if key not in {'licenses', 'contributors', 'keywords'}:
            raise ValueError(f'Unsupported descriptor metadata field: {key}')
        descriptor[key] = deepcopy(value)

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

    for name, table in additional_tables.items():
        if name in RESERVED_TABLE_NAMES or name != normalize_resource_name(name):
            raise ValueError(f'Additional table {name!r} must have a normalized, non-reserved name.')
        df = table['dataframe']
        schema = deepcopy(table['schema'])
        if list(df.columns) != [field['name'] for field in schema.get('fields', [])]:
            raise ValueError(f'Additional table {name!r} fields do not match its dataframe.')
        descriptor['resources'].append({
            **{key: deepcopy(value) for key, value in table.items() if key not in {'dataframe', 'schema'}},
            'name': name, 'path': f'{name}.csv', 'profile': 'tabular-data-resource',
            'format': 'csv', 'mediatype': 'text/csv', 'encoding': 'utf-8', 'schema': schema,
        })

    descriptor_errors = validate_datapackage_descriptor(descriptor, allow_generic=bool(additional_tables))
    if descriptor_errors:
        raise ValueError("Invalid DwC-DP descriptor: " + "; ".join(descriptor_errors))
    return descriptor


def _serializable_resource(name: str, df: pd.DataFrame) -> pd.DataFrame:
    """Write whole-number float columns of integer fields as integers ('2', not '2.0')."""
    fields = get_table_spec(name).field_descriptors
    columns = [
        column
        for column in df.columns
        if isinstance(column, str)
        and (fields.get(column) or {}).get("type") == "integer"
        and pd.api.types.is_float_dtype(df[column])
        and ((df[column].dropna() % 1) == 0).all()
        and (df[column].dropna().abs() < 2**53).all()
    ]
    if not columns:
        return df
    df = df.copy()
    df[columns] = df[columns].astype("Int64")
    return df


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        df.to_csv(handle, index=False, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)


def _safe_archive_name(filename: str) -> str:
    name = PurePosixPath(str(filename).replace("\\", "/"))
    if name.is_absolute() or ".." in name.parts or len(name.parts) != 1 or not name.name:
        raise ValueError(f"Unsafe ancillary archive filename: {filename!r}.")
    return name.name


@lru_cache(maxsize=None)
def _eml_xml_schema() -> etree.XMLSchema:
    return etree.XMLSchema(etree.parse(str(_EML_XSD)))


# XMLSchema keeps its error log on the instance, so validations must not overlap.
_EML_SCHEMA_LOCK = threading.Lock()


def validate_eml(content: bytes) -> list[str]:
    """Check eml.xml as GBIF's DwC-DP analyser does: well-formed, titled, credited, EML 2.2.0-valid."""
    try:
        content.decode("utf-8", "strict")
        root = etree.fromstring(
            content, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        )
    except (UnicodeDecodeError, etree.XMLSyntaxError) as exc:
        return [f"eml.xml cannot be parsed as strict UTF-8 XML: {exc}."]

    errors = []
    for element in ("title", "creator"):
        node = root.find(f"dataset/{element}")
        if node is None or not "".join(node.itertext()).strip():
            errors.append(f"eml.xml has no non-empty dataset <{element}>.")

    with _EML_SCHEMA_LOCK:
        schema = _eml_xml_schema()
        schema.validate(root)
        schema_errors = list(schema.error_log)
    for entry in schema_errors[:_REPORTED_ERRORS_PER_SOURCE]:
        errors.append(f"eml.xml does not match the EML 2.2.0 schema (line {entry.line}): {entry.message}")
    if len(schema_errors) > _REPORTED_ERRORS_PER_SOURCE:
        errors.append(
            f"eml.xml has {len(schema_errors) - _REPORTED_ERRORS_PER_SOURCE} more EML 2.2.0 schema errors."
        )
    return errors


def _frictionless_data_errors(descriptor: Mapping[str, Any], files: Mapping[str, bytes]) -> list[str]:
    """Run Frictionless validate() over the serialized package, checking every row of every resource."""
    with tempfile.TemporaryDirectory() as temp_dir:
        for path, content in files.items():
            (Path(temp_dir) / path).write_bytes(content)
        package_descriptor = deepcopy(dict(descriptor))
        package_descriptor["profile"] = "data-package"
        # Opaque provenance resources have byte/hash checks in archive validation.
        # Frictionless otherwise attempts to interpret the original ZIP as a table
        # and a JSON conversion report as its own validation-report format.
        package_descriptor["resources"] = [resource for resource in package_descriptor.get("resources", [])
            if resource.get("profile") != "data-resource" or resource.get("name") in RESERVED_TABLE_NAMES]
        report = Package.from_descriptor(package_descriptor, basepath=temp_dir).validate()

    errors = [f"Frictionless validation failed: {error.message}" for error in report.errors]
    for task in report.tasks:
        for error in task.errors[:_REPORTED_ERRORS_PER_SOURCE]:
            errors.append(f"Resource '{task.name}' failed Frictionless validation: {error.message}")
        if len(task.errors) > _REPORTED_ERRORS_PER_SOURCE:
            errors.append(
                f"Resource '{task.name}' has {len(task.errors) - _REPORTED_ERRORS_PER_SOURCE} more "
                "Frictionless validation errors."
            )
    return errors


def validate_dwc_dp_archive(
    archive_path: Path,
    expected_resources: Mapping[str, pd.DataFrame] | None = None,
    expected_additional_files: Iterable[str] | None = None,
    require_eml: bool = True,
    allow_generic: bool = False,
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

            required_files = {"datapackage.json", *expected_additional}
            if require_eml:
                required_files.add("eml.xml")
            for required_name in required_files:
                if required_name not in names:
                    errors.append(f"Archive is missing required file '{required_name}'.")

            if "datapackage.json" not in names or (require_eml and "eml.xml" not in names):
                return {"valid": False, "errors": errors, "warnings": warnings}

            try:
                descriptor = json.loads(
                    archive.extractfile("datapackage.json").read().decode("utf-8", "strict")
                )
            except Exception as exc:
                errors.append(f"datapackage.json cannot be parsed as strict UTF-8 JSON: {exc}.")
                descriptor = {}

            if "eml.xml" in names:
                errors.extend(validate_eml(archive.extractfile("eml.xml").read()))
            errors.extend(validate_datapackage_descriptor(descriptor, allow_generic=allow_generic))
            serialized_resources: Dict[str, pd.DataFrame] = {}
            resource_files: Dict[str, bytes] = {}
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
                if PurePosixPath(resource_path).name != resource_path:
                    errors.append(
                        f"Descriptor resource '{resource_name}' path '{resource_path}' must be a plain "
                        "file name at the package root."
                    )
                    continue
                if resource.get("profile") == "data-resource" and resource_name not in RESERVED_TABLE_NAMES:
                    content = archive.extractfile(resource_path).read()
                    if resource.get("bytes") is not None and resource["bytes"] != len(content):
                        errors.append(f"Ancillary resource '{resource_name}' has an incorrect byte count.")
                    if resource.get("hash") and resource["hash"] != "sha256:" + hashlib.sha256(content).hexdigest():
                        errors.append(f"Ancillary resource '{resource_name}' has an incorrect checksum.")
                    resource_files[resource_path] = content
                    continue
                try:
                    content = archive.extractfile(resource_path).read()
                    text = content.decode("utf-8", "strict")
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
                resource_files[resource_path] = content

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

            canonical = {name: df for name, df in serialized_resources.items() if name in RESERVED_TABLE_NAMES}
            if not canonical and descriptor.get('profile') == DWC_DP_PROFILE_URL:
                errors.append('A package with only additional tables must use the generic Data Package profile, not the DwC-DP profile.')
            serialized_validation = validate_dwc_dp_resources(canonical) if canonical or not serialized_resources else {'errors': [], 'warnings': []}
            errors.extend(serialized_validation["errors"])
            warnings.extend(serialized_validation["warnings"])
            # Row-level Frictionless errors would mostly repeat the checks above, so
            # only run it over a package that already passes them.
            if not errors:
                errors.extend(_frictionless_data_errors(descriptor, resource_files))
    except Exception as exc:
        errors.append(f"DwC-DP archive cannot be opened: {exc}.")

    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
    }


class DwcDpArchiveValidationError(ValueError):
    """The serialized package failed validation, so it must not be uploaded."""


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
    include_eml: bool = True,
    eml_content: bytes | None = None,
    declare_additional_resources: bool = False,
    additional_tables: Mapping[str, Mapping[str, Any]] | None = None,
    descriptor_metadata: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    descriptor = build_datapackage_descriptor(
        resources,
        dataset_id=dataset_id,
        title=title,
        description=description,
        version=version,
        additional_tables=additional_tables,
        descriptor_metadata=descriptor_metadata,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        package_root = Path(temp_dir) / "package"
        package_root.mkdir()
        if include_eml:
            content = eml_content if eml_content is not None else make_eml(title, description, user, eml_extra, dataset_id=dataset_id).encode("utf-8")
            (package_root / "eml.xml").write_bytes(content)
        for raw_name, df in resources.items():
            name = normalize_resource_name(raw_name)
            _write_csv(_serializable_resource(name, df), package_root / f"{name}.csv")
        for name, table in (additional_tables or {}).items():
            _write_csv(table['dataframe'], package_root / f'{name}.csv')

        additional_names = []
        for filename, content in additional_files or []:
            safe_name = _safe_archive_name(filename)
            target = package_root / safe_name
            if target.exists():
                raise ValueError(f"Ancillary file '{safe_name}' conflicts with a package file.")
            target.write_bytes(content)
            additional_names.append(safe_name)
            if declare_additional_resources:
                descriptor["resources"].append({"name": normalize_resource_name(Path(safe_name).stem),
                    "path": safe_name, "profile": "data-resource", "format": Path(safe_name).suffix.lstrip("."),
                    "mediatype": "application/zip" if safe_name.endswith(".zip") else "application/json",
                    "bytes": len(content), "hash": "sha256:" + hashlib.sha256(content).hexdigest()})

        descriptor_errors = validate_datapackage_descriptor(descriptor, allow_generic=bool(additional_tables))
        if descriptor_errors:
            raise DwcDpArchiveValidationError("; ".join(descriptor_errors))
        (package_root / "datapackage.json").write_text(json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        with tarfile.open(output_path, "w:gz") as archive:
            for child in sorted(package_root.iterdir()):
                archive.add(child, arcname=child.name)

    archive_validation = validate_dwc_dp_archive(
        output_path,
        expected_resources={
            **{normalize_resource_name(name): df for name, df in resources.items()},
            **{name: table['dataframe'] for name, table in (additional_tables or {}).items()},
        },
        expected_additional_files=additional_names,
        require_eml=include_eml,
        allow_generic=bool(additional_tables),
    )
    if not archive_validation["valid"]:
        raise DwcDpArchiveValidationError(
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
