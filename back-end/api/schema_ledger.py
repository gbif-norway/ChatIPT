"""Compact DwC-DP schema rendering and the per-agent schema/decision ledger.

History compaction in ``Agent.messages_for_model`` trims older tool output to a
few thousand characters and later drops it entirely. Verbose schema lookups
therefore disappeared from the model's context one or two turns after they were
fetched, and agents re-fetched the same schemas dozens of times. This module
keeps schema lookups small and derives a ledger from the stored tool-call log so
the looked-up schemas, the agent's working plan and its progress survive
compaction via the per-turn workflow state.

Everything here is pure (no Django or pandas imports) so it can be unit tested
in isolation. Callers pass spec objects exposing ``name``, ``title``,
``description`` and ``schema`` (see ``api.dwc_dp_specs.DwcDpTableSpec``).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

LOOKUP_TOOL = "GetDwcDpTableInfo"
PLAN_TOOL = "SetWorkingPlan"

# Ledger coverage levels for a looked-up table.
LEVEL_KEYS = "keys"
LEVEL_FIELDS = "fields"

MAX_FIELD_DETAILS = 20
MAX_PLAN_CHARS = 3000

# Tools whose calls never advance the package by themselves. A Python call is
# read-only unless it changes a table, which callers detect separately through
# table revision timestamps.
READ_ONLY_TOOLS = frozenset({
    LOOKUP_TOOL,
    PLAN_TOOL,
    "Python",
    # Validation checks the package without changing it. Counting it as progress
    # let a verification loop reset the no-progress limit every few turns.
    "ValidateDwcDp",
    "ReconcileSourceCoverage",
    "GetDarwinCoreInfo",
    "GetDwCExtensionInfo",
    "PreviewDwcDpDescriptor",
    "BasicValidationForSomeDwCTerms",
})

_FAILED_RESULT_PREFIXES = (
    "Unknown DwC-DP table",
    "ERROR CALLING FUNCTION",
)


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    return [str(item) for item in value] if isinstance(value, list) else [str(value)]


def normalize_table_name(value: Any) -> str:
    return str(value or "").strip().replace("_", "-").lower()


def _fk_lines(spec, foreign_keys: Iterable[Dict[str, Any]]) -> List[str]:
    lines = []
    for foreign_key in foreign_keys:
        reference = foreign_key.get("reference") or {}
        target = reference.get("resource") or spec.name
        source_fields = ", ".join(_as_list(foreign_key.get("fields")))
        target_fields = ", ".join(_as_list(reference.get("fields")))
        predicate = foreign_key.get("predicate")
        suffix = f" ({predicate})" if predicate else ""
        lines.append(f"{source_fields} -> {target}.{target_fields}{suffix}")
    return lines


def _field_token(field: Dict[str, Any]) -> str:
    constraints = field.get("constraints") or {}
    token = str(field.get("name"))
    if constraints.get("required"):
        token += "*"
    if constraints.get("unique"):
        token += "!"
    field_type = field.get("type") or "string"
    if field_type != "string":
        token += f":{field_type}"
    has_min = "minimum" in constraints
    has_max = "maximum" in constraints
    if has_min and has_max:
        token += f"[{constraints['minimum']}..{constraints['maximum']}]"
    elif has_min:
        token += f"[>={constraints['minimum']}]"
    elif has_max:
        token += f"[<={constraints['maximum']}]"
    field_format = field.get("format")
    if field_format and field_format != "default":
        token += f"({field_format})"
    return token


def render_table_manifest(spec, include_fields: bool = True, include_guidance: bool = False) -> str:
    """Render one table's keys, relationships and complete compact field list."""
    schema = spec.schema or {}
    lines = [f"{spec.name} — {spec.title}: {spec.description}".rstrip()]
    if include_guidance:
        if schema.get("comments"):
            lines.append(f"Table guidance: {schema['comments']}")
        if schema.get("examples"):
            lines.append(f"Table examples: {schema['examples']}")

    primary_key = ", ".join(_as_list(schema.get("primaryKey"))) or "(none)"
    weak_primary_key = ", ".join(_as_list(schema.get("weakPrimaryKey"))) or "(none)"
    lines.append(f"Primary key (enforced): {primary_key}; weak primary key (preserved, not enforced): {weak_primary_key}")

    foreign_keys = _fk_lines(spec, schema.get("foreignKeys") or [])
    lines.append(
        "Foreign keys (enforced): " + ("; ".join(foreign_keys) if foreign_keys else "(none)")
    )
    weak_foreign_keys = _fk_lines(spec, schema.get("weakForeignKeys") or [])
    lines.append(
        "Weak foreign keys (not enforced): "
        + ("; ".join(weak_foreign_keys) if weak_foreign_keys else "(none)")
    )

    if include_fields:
        fields = schema.get("fields") or []
        lines.append(
            f"Fields ({len(fields)}, complete; type string unless noted; * required, ! unique): "
            + ", ".join(_field_token(field) for field in fields)
        )
    return "\n".join(lines)


def render_field_details(spec, field_names: Iterable[str]) -> str:
    """Full definitions for explicitly requested fields only."""
    descriptors = {field.get("name"): field for field in (spec.schema or {}).get("fields") or []}
    requested = []
    for name in field_names or []:
        name = str(name).strip()
        if name and name not in requested:
            requested.append(name)
    unknown = [name for name in requested if name not in descriptors]
    known = [name for name in requested if name in descriptors]

    lines = [f"Field details for `{spec.name}`:"]
    for name in known[:MAX_FIELD_DETAILS]:
        field = descriptors[name]
        lines.append(f"- {_field_token(field)}: {field.get('description', '')}")
        if field.get("comments"):
            lines.append(f"  Guidance: {field['comments']}")
        if field.get("examples"):
            lines.append(f"  Examples: {field['examples']}")
        if field.get("dcterms:isVersionOf"):
            lines.append(f"  Term: {field['dcterms:isVersionOf']}")
    if len(known) > MAX_FIELD_DETAILS:
        lines.append(
            f"Only the first {MAX_FIELD_DETAILS} requested fields are shown; "
            "request the rest only if they change a mapping decision."
        )
    if unknown:
        lines.append(f"Not fields of `{spec.name}`: {', '.join(unknown)}.")
    return "\n".join(lines)


def _parse_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}", strict=False)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def iter_tool_calls(openai_objs: Iterable[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any], Optional[str]]]:
    """Pair every stored tool call with its result: [(name, args, result_or_None)]."""
    calls: List[Tuple[str, str, Dict[str, Any]]] = []
    results: Dict[str, str] = {}
    for obj in openai_objs:
        obj = obj or {}
        role = obj.get("role")
        if role == "assistant":
            for tool_call in obj.get("tool_calls") or []:
                function = (tool_call or {}).get("function") or {}
                calls.append((
                    str((tool_call or {}).get("id")),
                    str(function.get("name") or ""),
                    _parse_arguments(function.get("arguments")),
                ))
        elif role == "tool" and obj.get("tool_call_id") is not None:
            content = obj.get("content")
            results[str(obj.get("tool_call_id"))] = content if isinstance(content, str) else json.dumps(content)
    return [(name, args, results.get(call_id)) for call_id, name, args in calls]


def schema_lookups(openai_objs: Iterable[Dict[str, Any]], known_tables: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Tables successfully looked up in this history, in lookup order, with coverage level."""
    known = set(known_tables) if known_tables is not None else None
    ledger: Dict[str, str] = {}
    for name, args, result in iter_tool_calls(openai_objs):
        if name != LOOKUP_TOOL or result is None:
            continue
        if any(result.startswith(prefix) for prefix in _FAILED_RESULT_PREFIXES):
            continue
        table = normalize_table_name(args.get("table_name"))
        if not table or (known is not None and table not in known):
            continue
        level = LEVEL_FIELDS if args.get("include_fields", True) else LEVEL_KEYS
        if ledger.get(table) != LEVEL_FIELDS:
            ledger[table] = level
    return ledger


def lookup_is_covered(ledger: Dict[str, str], table_name: Any, include_fields: bool, field_details: Any) -> bool:
    """True when a requested lookup would only repeat what the ledger already holds."""
    if field_details:
        return False
    level = ledger.get(normalize_table_name(table_name))
    if include_fields:
        return level == LEVEL_FIELDS
    return level in {LEVEL_FIELDS, LEVEL_KEYS}


def duplicate_lookup_notice(table_name: Any, include_fields: bool) -> str:
    table = normalize_table_name(table_name)
    scope = "complete field list and keys" if include_fields else "keys and relationships"
    return (
        f"Not re-run: the `{table}` schema ({scope}) was already looked up in this task and is "
        "listed in the DWC-DP SCHEMA LEDGER of the current workflow state. Use the ledger instead "
        "of repeating lookups. For the full definition of specific fields, call "
        "GetDwcDpTableInfo with field_details."
    )


SCHEMA_FILE_NOTICE = (
    "Not run: do not read DwC-DP schema files with Python. Every schema you have looked up is "
    "listed in the DWC-DP SCHEMA LEDGER of the current workflow state; use GetDwcDpTableInfo for "
    "a table that is not there yet, or field_details for specific field definitions."
)


def reads_schema_files(code: Any) -> bool:
    return "table-schemas" in str(code or "")


def latest_working_plan(openai_objs: Iterable[Dict[str, Any]]) -> str:
    plan = ""
    for name, args, result in iter_tool_calls(openai_objs):
        if name == PLAN_TOOL and result is not None and not result.startswith("ERROR CALLING FUNCTION"):
            plan = str(args.get("plan") or "").strip()[:MAX_PLAN_CHARS]
    return plan


def turns_since_progress(entries: Iterable[Tuple[Any, Dict[str, Any]]], progress_floor=None) -> int:
    """Count tool-calling assistant turns since the last evidence of progress.

    ``entries`` are (created_at, openai_obj) pairs in chronological order.
    Progress is a user message, a result from a tool outside READ_ONLY_TOOLS,
    or ``progress_floor`` (e.g. the latest table revision, which catches
    package writes made through Python).
    """
    entries = list(entries)
    call_names: Dict[str, str] = {}
    last_progress = progress_floor
    for created_at, obj in entries:
        obj = obj or {}
        role = obj.get("role")
        is_progress = False
        if role == "user":
            is_progress = True
        elif role == "assistant":
            for tool_call in obj.get("tool_calls") or []:
                function = (tool_call or {}).get("function") or {}
                call_names[str((tool_call or {}).get("id"))] = str(function.get("name") or "")
        elif role == "tool":
            is_progress = call_names.get(str(obj.get("tool_call_id"))) not in READ_ONLY_TOOLS
        if is_progress and created_at is not None and (last_progress is None or created_at > last_progress):
            last_progress = created_at
    return sum(
        1
        for created_at, obj in entries
        if (obj or {}).get("role") == "assistant"
        and (obj or {}).get("tool_calls")
        and (last_progress is None or (created_at is not None and created_at > last_progress))
    )


def latest_tool_results(openai_objs: Iterable[Dict[str, Any]], tool_name: str) -> List[str]:
    return [
        result
        for name, _args, result in iter_tool_calls(openai_objs)
        if name == tool_name and result is not None
    ]


def render_ledger(
    ledger: Dict[str, str],
    plan: str,
    get_spec: Callable[[str], Any],
    coverage_open_items: Optional[str] = None,
) -> str:
    if not ledger and not plan and coverage_open_items is None:
        return ""
    lines = [
        "DWC-DP SCHEMA LEDGER (authoritative for this task; these lookups are complete and are not "
        "re-run, so use them instead of GetDwcDpTableInfo or reading schema files):"
    ]
    for table, level in ledger.items():
        try:
            spec = get_spec(table)
        except KeyError:
            continue
        lines.append("")
        lines.append(render_table_manifest(spec, include_fields=(level == LEVEL_FIELDS)))
    if plan:
        lines.append("")
        lines.append("Working plan (latest SetWorkingPlan; update it as writes complete):")
        lines.append(plan)
    if coverage_open_items is not None:
        lines.append("")
        lines.append(
            "Latest ReconcileSourceCoverage open items (everything else it checked is verified; "
            f"give these a disposition in the coverage report): {coverage_open_items}"
        )
    return "\n".join(lines)
