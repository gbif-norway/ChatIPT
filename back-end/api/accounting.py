from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
from typing import Any

import pandas as pd
from django.conf import settings
from django.utils import timezone

from api.dwc_dp_specs import DWC_DP_SCHEMA_REVISION, RESERVED_TABLE_NAMES


ACCOUNTING_VERSION = 2


def _populated_count(series: pd.Series) -> int:
    non_null = series.notna()
    if not non_null.any():
        return 0
    non_blank = series.astype(str).str.strip().ne("")
    return int((non_null & non_blank).sum())


def build_source_accounting_snapshot(dataset) -> dict[str, Any]:
    """Capture counts that survive replacement or deletion of source tables."""
    tables = []
    for table in dataset.table_set.order_by("created_at", "id"):
        df = table.df
        columns = []
        for index, column_name in enumerate(df.columns):
            populated_values = _populated_count(df.iloc[:, index])
            if populated_values:
                columns.append(
                    {
                        "column_index": index,
                        "name": str(column_name),
                        "populated_values": populated_values,
                    }
                )
        tables.append(
            {
                "source_table_id": table.id,
                "title": table.title,
                "row_count": int(len(df)),
                "columns": columns,
            }
        )

    return {
        "version": ACCOUNTING_VERSION,
        "created_at": timezone.now().isoformat(),
        "tables": tables,
    }


def _snapshot_columns(source: dict[str, Any]) -> dict[tuple[int, str], dict[str, Any]]:
    return {
        (column["column_index"], column["name"]): column
        for column in source.get("columns", [])
        if column.get("populated_values", 0) > 0
    }


def expand_compact_accounting(
    dataset,
    compact_sources: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """
    Expand the model's compact semantic routing into the complete declaration
    stored in the signed receipt.

    Source names/counts and target row totals are server-owned facts. Column
    routes use immutable source column indexes. Resource mappings are grouped
    by target table, while metadata and omission mappings are grouped by
    destination. An omitted source_values entry covers every populated value
    in that source column.
    """
    snapshot = dataset.source_accounting_snapshot or {}
    snapshot_by_id = {
        source["source_table_id"]: source
        for source in snapshot.get("tables", [])
    }
    target_tables = {
        title: list(dataset.table_set.filter(title=title).order_by("id"))
        for title in RESERVED_TABLE_NAMES
    }
    errors: list[str] = []
    expanded_sources: list[dict[str, Any]] = []

    for compact_source in compact_sources:
        source_id = compact_source.get("source_table_id")
        source_snapshot = snapshot_by_id.get(source_id)
        if source_snapshot is None:
            errors.append(
                f"Source table {source_id} is not present in the source accounting snapshot."
            )
            continue

        populated_columns = {
            column["column_index"]: column
            for column in source_snapshot.get("columns", [])
            if column.get("populated_values", 0) > 0
        }
        expanded_columns: dict[int, dict[str, Any]] = {}
        destination_keys: dict[int, set[tuple[Any, ...]]] = {}

        def route_source_values(route: dict[str, Any], column_index: int) -> int:
            values = route.get("source_values", {})
            column_snapshot = populated_columns.get(column_index)
            default = column_snapshot["populated_values"] if column_snapshot else 0
            return values.get(
                column_index,
                values.get(str(column_index), default),
            )

        def add_destination(
            column_index: int,
            destination: dict[str, Any],
            destination_key: tuple[Any, ...],
        ) -> None:
            column_snapshot = populated_columns.get(column_index)
            if column_snapshot is None:
                errors.append(
                    f"Source table {source_id}: column index {column_index} is unknown "
                    "or has no populated source values."
                )
                return

            column = expanded_columns.setdefault(
                column_index,
                {
                    "source_column": column_snapshot["name"],
                    "source_column_index": column_index,
                    "source_populated_values": column_snapshot["populated_values"],
                    "destinations": [],
                },
            )
            keys = destination_keys.setdefault(column_index, set())
            if destination_key in keys:
                errors.append(
                    f"Source table {source_id}: populated column index {column_index} "
                    "repeats the same destination."
                )
                return
            keys.add(destination_key)
            column["destinations"].append(destination)

        for route in compact_source.get("resource_routes", []):
            target_table = route.get("target_table")
            for raw_index, target_field in route.get("field_mappings", {}).items():
                column_index = int(raw_index)
                add_destination(
                    column_index,
                    {
                        "kind": "resource",
                        "source_values": route_source_values(route, column_index),
                        "target_table": target_table,
                        "target_field": target_field,
                    },
                    ("resource", target_table, target_field),
                )

        for route in compact_source.get("metadata_routes", []):
            metadata_field = route.get("metadata_field")
            for column_index in route.get("source_column_indexes", []):
                add_destination(
                    column_index,
                    {
                        "kind": "metadata",
                        "source_values": route_source_values(route, column_index),
                        "metadata_field": metadata_field,
                    },
                    ("metadata", metadata_field),
                )

        for route in compact_source.get("omitted_column_routes", []):
            reason = route.get("reason")
            for column_index in route.get("source_column_indexes", []):
                add_destination(
                    column_index,
                    {
                        "kind": "omitted",
                        "source_values": route_source_values(route, column_index),
                        "reason": reason,
                    },
                    ("omitted", reason),
                )

        expanded_dispositions = []
        for disposition in compact_source.get("dispositions", []):
            expanded_disposition = copy.deepcopy(disposition)
            matches = target_tables.get(disposition.get("target_table"), [])
            expanded_disposition["target_table_rows"] = (
                int(len(matches[0].df)) if len(matches) == 1 else 0
            )
            expanded_dispositions.append(expanded_disposition)

        omissions = copy.deepcopy(compact_source.get("omissions", []))
        expanded_sources.append(
            {
                "source_table_id": source_id,
                "source_table_title": source_snapshot["title"],
                "source_rows": source_snapshot["row_count"],
                "rows_accounted": compact_source.get("rows_accounted", 0),
                "omitted_rows": sum(item.get("rows", 0) for item in omissions),
                "omissions": omissions,
                "coverage_notes": compact_source.get("coverage_notes", ""),
                "dispositions": expanded_dispositions,
                "columns": [
                    expanded_columns[index]
                    for index in sorted(expanded_columns)
                ],
            }
        )

    return {"sources": expanded_sources}, errors


def _dataset_metadata_value(dataset, field_path: str | None) -> Any:
    """Resolve the small, explicit metadata namespace accepted by accounting."""
    if not field_path:
        return None
    parts = field_path.split(".")
    if parts[0] in {"title", "description", "orcid"}:
        value: Any = getattr(dataset, parts[0], None)
        parts = parts[1:]
    elif parts[0] == "eml":
        value = dataset.eml or {}
        parts = parts[1:]
    else:
        return None
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _has_populated_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _dataframe_field_has_populated_value(df: pd.DataFrame, field_name: str) -> bool:
    positions = [index for index, name in enumerate(df.columns) if str(name) == field_name]
    return any(_populated_count(df.iloc[:, index]) > 0 for index in positions)


def remove_empty_dwc_dp_resources(dataset) -> list[str]:
    """Remove reserved package tables with no records."""
    removed = []
    for table in dataset.table_set.filter(title__in=RESERVED_TABLE_NAMES).order_by("id"):
        if table.df.empty:
            removed.append(table.title)
            table.delete()
    return removed


def remove_final_staging_tables(dataset) -> list[str]:
    """Remove source and working tables once package validation is complete."""
    removed = list(
        dataset.table_set.exclude(title__in=RESERVED_TABLE_NAMES).values_list("title", flat=True)
    )
    dataset.table_set.exclude(title__in=RESERVED_TABLE_NAMES).delete()
    return removed


def _canonical_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return str(value)
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dataframe_digest(df: pd.DataFrame) -> str:
    payload = {
        "columns": [str(column) for column in df.columns],
        "rows": [
            [_canonical_value(value) for value in row]
            for row in df.itertuples(index=False, name=None)
        ],
    }
    return _json_digest(payload)


def _resource_state(dataset) -> list[dict[str, Any]]:
    return [
        {
            "table_id": table.id,
            "title": table.title,
            "row_count": int(len(table.df)),
            "sha256": _dataframe_digest(table.df),
        }
        for table in dataset.table_set.filter(title__in=RESERVED_TABLE_NAMES).order_by("title", "id")
    ]


def _receipt_signature(receipt: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in receipt.items() if key != "signature"}
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    secret = str(settings.SECRET_KEY).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify_dataset_accounting(dataset, declaration: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = dataset.source_accounting_snapshot or {}
    declaration = declaration if declaration is not None else (dataset.dwc_dp_accounting or {}).get("declaration")
    errors: list[str] = []
    warnings: list[str] = []

    if not snapshot.get("tables"):
        errors.append("No source accounting snapshot exists. Start the Data transformation task again before changing tables.")
    if not declaration:
        errors.append("No DwC-DP accounting declaration has been submitted.")

    if errors:
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "summary": {},
        }

    snapshot_by_id = {source["source_table_id"]: source for source in snapshot["tables"]}
    declared_sources = declaration.get("sources", [])
    declared_by_id: dict[int, dict[str, Any]] = {}
    for source in declared_sources:
        source_id = source.get("source_table_id")
        if source_id in declared_by_id:
            errors.append(f"Source table {source_id} is declared more than once.")
        else:
            declared_by_id[source_id] = source

    missing_ids = sorted(set(snapshot_by_id) - set(declared_by_id))
    extra_ids = sorted(set(declared_by_id) - set(snapshot_by_id))
    if missing_ids:
        errors.append(f"Source tables missing from accounting: {missing_ids}.")
    if extra_ids:
        errors.append(f"Accounting refers to tables not present in the source snapshot: {extra_ids}.")

    target_tables: dict[str, Any] = {}
    for title in RESERVED_TABLE_NAMES:
        matches = list(dataset.table_set.filter(title=title).order_by("id"))
        if len(matches) > 1:
            errors.append(f"Target resource '{title}' exists more than once; accounting is ambiguous.")
        elif matches:
            target_tables[title] = matches[0]

    omitted_rows_total = 0
    omitted_columns_total = 0
    omitted_values_total = 0
    disposition_count = 0

    for source_id, source_snapshot in snapshot_by_id.items():
        source = declared_by_id.get(source_id)
        if not source:
            continue
        label = f"source table {source_id} ({source_snapshot['title']})"

        if source.get("source_table_title") != source_snapshot["title"]:
            errors.append(
                f"{label}: declared title {source.get('source_table_title')!r} does not match the snapshot."
            )
        if source.get("source_rows") != source_snapshot["row_count"]:
            errors.append(
                f"{label}: declared source_rows={source.get('source_rows')} but snapshot has {source_snapshot['row_count']}."
            )

        rows_accounted = source.get("rows_accounted", 0)
        omitted_rows = source.get("omitted_rows", 0)
        if rows_accounted + omitted_rows != source_snapshot["row_count"]:
            errors.append(
                f"{label}: rows_accounted ({rows_accounted}) + omitted_rows ({omitted_rows}) "
                f"must equal {source_snapshot['row_count']}."
            )
        omissions = source.get("omissions", [])
        omission_rows_by_reason = sum(item.get("rows", 0) for item in omissions)
        if omitted_rows != omission_rows_by_reason:
            errors.append(
                f"{label}: omitted_rows ({omitted_rows}) must equal the sum of omission reason rows "
                f"({omission_rows_by_reason})."
            )
        if omitted_rows:
            omitted_rows_total += omitted_rows
            warnings.append(f"{label}: {omitted_rows} source rows were explicitly omitted.")

        dispositions = source.get("dispositions", [])
        disposition_count += len(dispositions)
        if rows_accounted and not dispositions:
            errors.append(f"{label}: accounted rows require at least one target disposition.")
        disposition_row_coverage = sum(item.get("source_rows_used", 0) for item in dispositions)
        if disposition_row_coverage < rows_accounted:
            errors.append(
                f"{label}: disposition paths cover at most {disposition_row_coverage} source-row uses, "
                f"which cannot account for {rows_accounted} unique rows."
            )
        for disposition in dispositions:
            target_name = disposition.get("target_table")
            target = target_tables.get(target_name)
            if target is None:
                errors.append(f"{label}: target resource '{target_name}' does not exist exactly once.")
                continue
            actual_target_rows = int(len(target.df))
            if disposition.get("target_table_rows") != actual_target_rows:
                errors.append(
                    f"{label} -> {target_name}: declared target_table_rows="
                    f"{disposition.get('target_table_rows')} but the table has {actual_target_rows}."
                )
            used = disposition.get("source_rows_used", 0)
            contributed = disposition.get("target_rows_contributed", 0)
            if used > source_snapshot["row_count"]:
                errors.append(f"{label} -> {target_name}: source_rows_used exceeds the source row count.")
            if used > rows_accounted:
                errors.append(f"{label} -> {target_name}: source_rows_used exceeds unique rows_accounted.")
            if contributed > actual_target_rows:
                errors.append(f"{label} -> {target_name}: target_rows_contributed exceeds target table rows.")
            if used and not contributed:
                errors.append(
                    f"{label} -> {target_name}: source_rows_used is non-zero but target_rows_contributed is zero."
                )
            if used and not actual_target_rows:
                errors.append(f"{label} -> {target_name}: an empty target cannot account for source rows.")

        expected_columns = _snapshot_columns(source_snapshot)
        declared_columns: dict[tuple[int, str], dict[str, Any]] = {}
        for column in source.get("columns", []):
            key = (column.get("source_column_index"), column.get("source_column"))
            if key in declared_columns:
                errors.append(f"{label}: source column {key!r} is declared more than once.")
            else:
                declared_columns[key] = column

        missing_columns = sorted(set(expected_columns) - set(declared_columns))
        extra_columns = sorted(set(declared_columns) - set(expected_columns))
        if missing_columns:
            errors.append(f"{label}: populated source columns missing from accounting: {missing_columns}.")
        if extra_columns:
            errors.append(f"{label}: accounting contains unknown or unpopulated source columns: {extra_columns}.")

        for key, column_snapshot in expected_columns.items():
            column = declared_columns.get(key)
            if not column:
                continue
            if column.get("source_populated_values") != column_snapshot["populated_values"]:
                errors.append(
                    f"{label}, column {key!r}: declared source_populated_values="
                    f"{column.get('source_populated_values')} but snapshot has {column_snapshot['populated_values']}."
                )
            destinations = column.get("destinations", [])
            if not destinations:
                errors.append(f"{label}, column {key!r}: at least one destination or omission is required.")
            destination_coverage = sum(item.get("source_values", 0) for item in destinations)
            if destination_coverage < column_snapshot["populated_values"]:
                errors.append(
                    f"{label}, column {key!r}: destination paths cover at most {destination_coverage} "
                    f"source values but the snapshot has {column_snapshot['populated_values']} populated values."
                )
            for destination in destinations:
                kind = destination.get("kind")
                destination_values = destination.get("source_values", 0)
                if destination_values > column_snapshot["populated_values"]:
                    errors.append(
                        f"{label}, column {key!r}: destination source_values ({destination_values}) "
                        f"exceeds populated source values ({column_snapshot['populated_values']})."
                    )
                if kind == "resource":
                    target_name = destination.get("target_table")
                    target_field = destination.get("target_field")
                    target = target_tables.get(target_name)
                    if target is None:
                        errors.append(
                            f"{label}, column {key!r}: target resource '{target_name}' does not exist exactly once."
                        )
                    elif target_field not in [str(value) for value in target.df.columns]:
                        errors.append(
                            f"{label}, column {key!r}: target field '{target_name}.{target_field}' does not exist."
                        )
                    elif destination_values and not _dataframe_field_has_populated_value(
                        target.df, target_field
                    ):
                        errors.append(
                            f"{label}, column {key!r}: target field '{target_name}.{target_field}' has no populated values."
                        )
                elif kind == "metadata":
                    metadata_field = destination.get("metadata_field")
                    if not metadata_field:
                        errors.append(f"{label}, column {key!r}: metadata destination requires metadata_field.")
                    elif destination_values and not _has_populated_value(
                        _dataset_metadata_value(dataset, metadata_field)
                    ):
                        errors.append(
                            f"{label}, column {key!r}: metadata field '{metadata_field}' does not exist or is empty."
                        )
                elif kind == "omitted":
                    omitted_columns_total += 1
                    omitted_values_total += destination_values
                    if not destination.get("reason"):
                        errors.append(f"{label}, column {key!r}: omitted destination requires a reason.")
                    else:
                        warnings.append(
                            f"{label}, column {column_snapshot['name']!r}: {destination_values} populated values "
                            "were explicitly omitted."
                        )
                else:
                    errors.append(f"{label}, column {key!r}: unknown destination kind {kind!r}.")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "source_tables": len(snapshot_by_id),
            "source_rows": sum(source["row_count"] for source in snapshot_by_id.values()),
            "populated_source_columns": sum(len(_snapshot_columns(source)) for source in snapshot_by_id.values()),
            "target_resources": len(target_tables),
            "dispositions": disposition_count,
            "explicitly_omitted_rows": omitted_rows_total,
            "explicitly_omitted_columns": omitted_columns_total,
            "explicitly_omitted_values": omitted_values_total,
        },
    }


def save_and_verify_accounting(dataset, declaration: dict[str, Any]) -> dict[str, Any]:
    remove_empty_dwc_dp_resources(dataset)
    verification = verify_dataset_accounting(dataset, declaration)
    if not verification["valid"]:
        return verification

    receipt = {
        "version": ACCOUNTING_VERSION,
        "submitted_at": timezone.now().isoformat(),
        "dataset_id": dataset.id,
        "schema_revision": DWC_DP_SCHEMA_REVISION,
        "source_snapshot_sha256": _json_digest(dataset.source_accounting_snapshot or {}),
        "resources": _resource_state(dataset),
        "declaration": declaration,
    }
    receipt["signature"] = _receipt_signature(receipt)
    dataset.dwc_dp_accounting = receipt
    dataset.save(update_fields=["dwc_dp_accounting"])
    return verification


def current_accounting_status(dataset) -> dict[str, Any]:
    """Verify the signed receipt and reconcile it against the current package."""
    receipt = dataset.dwc_dp_accounting or {}
    errors: list[str] = []

    if not receipt:
        return {
            "valid": False,
            "errors": ["No current DwC-DP accounting receipt exists. Call SubmitDwcDpAccounting."],
            "warnings": [],
            "summary": {},
        }

    signature = receipt.get("signature")
    if not signature or not hmac.compare_digest(str(signature), _receipt_signature(receipt)):
        errors.append(
            "The DwC-DP accounting receipt is not authentic or has been edited. "
            "Call SubmitDwcDpAccounting again."
        )
    if receipt.get("dataset_id") != dataset.id:
        errors.append("The DwC-DP accounting receipt belongs to a different dataset.")
    if receipt.get("schema_revision") != DWC_DP_SCHEMA_REVISION:
        errors.append(
            "The DwC-DP schema revision changed after accounting was submitted. "
            "Call SubmitDwcDpAccounting again."
        )
    if receipt.get("source_snapshot_sha256") != _json_digest(dataset.source_accounting_snapshot or {}):
        errors.append(
            "The source accounting snapshot changed after accounting was submitted. "
            "Call SubmitDwcDpAccounting again."
        )
    if receipt.get("resources") != _resource_state(dataset):
        errors.append(
            "The DwC-DP resource tables changed after accounting was submitted. "
            "Call SubmitDwcDpAccounting again."
        )

    verification = verify_dataset_accounting(dataset, receipt.get("declaration"))
    combined_errors = list(dict.fromkeys([*errors, *verification["errors"]]))
    return {
        **verification,
        "valid": not combined_errors,
        "errors": combined_errors,
    }
