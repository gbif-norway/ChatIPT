"""Compact DwC-DP schema rendering and the per-agent schema/decision ledger.

History compaction in ``Agent.messages_for_model`` trims older tool output to a
few thousand characters and later drops it entirely. Verbose schema lookups
therefore disappeared from the model's context one or two turns after they were
fetched, and agents re-fetched the same schemas dozens of times. This module
keeps schema lookups small and derives a ledger from the stored tool-call log so
looked-up schemas and the working plan survive compaction in the per-turn state.

Everything here is pure (no Django or pandas imports) so it can be unit tested
in isolation. Callers pass spec objects exposing ``name``, ``title``,
``description`` and ``schema`` (see ``api.dwc_dp_specs.DwcDpTableSpec``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

LOOKUP_TOOL = "GetDwcDpTableInfo"
PLAN_TOOL = "SetWorkingPlan"
DWC_TERM_TOOL = "GetDarwinCoreInfo"
EXTENSION_TOOL = "GetDwCExtensionInfo"

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
    DWC_TERM_TOOL,
    EXTENSION_TOOL,
    "PreviewDwcDpDescriptor",
    "BasicValidationForSomeDwCTerms",
})

_FAILED_RESULT_PREFIXES = (
    "Unknown DwC-DP table",
    "ERROR CALLING FUNCTION",
)

# Tools that change publication artifacts only when their result says so. A
# no-op re-export or a rejected upload used to count as progress, so dataset
# 526 could keep re-exporting an unchanged package between term lookups
# without ever tripping the no-progress limit.
PROGRESS_RESULT_PREFIXES = {
    "ExportDwcDp": ("DwC-DP successfully created and uploaded",),
    "UploadDwCA": ("DwCA successfully created and uploaded",),
}


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


# -- Darwin Core and DwC-A extension lookups ---------------------------------
#
# Package preparation looks up Darwin Core and extension terms before building
# the DwC-A projection. Those results were compacted away like the DwC-DP
# schemas, and dataset 526 re-requested the same extension terms 5-6 times each
# for eight minutes instead of building the projection. The lookups made so far
# are rebuilt from the tool log into a compact term ledger, and repeats return
# only what is new.

MAX_TERM_DEFINITION_CHARS = 140
MAX_LEDGER_TERM_DEFINITIONS = 160

_DWC_FAILED_RESULT_PREFIXES = (
    "ERROR CALLING FUNCTION",
    "Unable to load",
    "Extension schema",
)


class DwcReference(Protocol):
    """Resolves Darwin Core and extension names against the vendored references."""

    def dwc_section(self, section: str) -> Optional[Tuple[str, Sequence[str]]]:
        """(canonical section name, its term names) or None."""

    def dwc_term(self, term: str) -> Optional[Tuple[str, str, str]]:
        """(canonical term name, section, definition without examples) or None."""

    def extensions(self) -> Sequence[str]:
        """Every registered extension key, in catalogue order."""

    def extension(self, value: str) -> Optional[Tuple[str, str, Sequence[str], Sequence[str]]]:
        """(key, title, compatible cores, registered term names) or None."""

    def extension_term(self, key: str, term: str) -> Optional[Tuple[str, str]]:
        """(canonical term name, definition) for a registered extension term, or None."""


def term_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


@dataclass
class DwcLookups:
    catalogue: bool = False
    # canonical section name -> max_terms listed (None = every term)
    sections: Dict[str, Optional[int]] = field(default_factory=dict)
    # term key -> (name, section, definition), in lookup order
    terms: Dict[str, Tuple[str, str, str]] = field(default_factory=dict)
    # extension key -> {term key: (name, definition)}; a key means its guidance was shown
    extensions: Dict[str, Dict[str, Tuple[str, str]]] = field(default_factory=dict)

    def __bool__(self):
        return bool(self.catalogue or self.sections or self.terms or self.extensions)


def _requested_terms(value: Any) -> List[str]:
    return [term for term in (str(item).strip() for item in _as_list(value)) if term]


def _record_dwc_terms(lookups: DwcLookups, ref: DwcReference, names: Iterable[str]) -> None:
    for name in names:
        resolved = ref.dwc_term(name)
        if resolved:
            lookups.terms.setdefault(term_key(resolved[0]), resolved)


def _section_listing_limit(args: Dict[str, Any], section_terms: Sequence[str]) -> Optional[int]:
    """Terms a section listing shows; None when it shows the whole section."""
    try:
        limit = int(args["max_terms"]) if args.get("max_terms") is not None else None
    except (TypeError, ValueError):
        return None
    return None if limit is None or limit >= len(section_terms) else limit


def dwc_lookups(openai_objs: Iterable[Dict[str, Any]], ref: DwcReference) -> DwcLookups:
    """Darwin Core sections/terms and extensions successfully looked up in this history."""
    lookups = DwcLookups()
    for name, args, result in iter_tool_calls(openai_objs):
        if name not in {DWC_TERM_TOOL, EXTENSION_TOOL} or result is None:
            continue
        if any(result.startswith(prefix) for prefix in _DWC_FAILED_RESULT_PREFIXES):
            continue
        if name == DWC_TERM_TOOL:
            if args.get("terms"):
                _record_dwc_terms(lookups, ref, _requested_terms(args.get("terms")))
            elif args.get("section"):
                section = ref.dwc_section(str(args["section"]))
                if not section:
                    continue
                section_name, section_terms = section
                limit = _section_listing_limit(args, section_terms)
                previous = lookups.sections.get(section_name, 0)
                if section_name not in lookups.sections or (
                    previous is not None and (limit is None or limit > previous)
                ):
                    lookups.sections[section_name] = limit
                _record_dwc_terms(lookups, ref, list(section_terms)[:limit] if limit else section_terms)
            continue

        if not args.get("extension"):
            lookups.catalogue = True
            continue
        extension = ref.extension(str(args["extension"]))
        if not extension:
            continue
        key = extension[0]
        defined = lookups.extensions.setdefault(key, {})
        for term in _requested_terms(args.get("terms")):
            resolved = ref.extension_term(key, term)
            if resolved:
                defined.setdefault(term_key(resolved[0]), resolved)
    return lookups


def _covered_notice(scope: str) -> str:
    return (
        f"Not re-run: {scope} already looked up in this task and listed in the DWC-A TERM LEDGER "
        "of the current workflow state. Map from the ledger instead of repeating lookups; if a "
        "term's fit is still uncertain, record it as a projection limitation or preserve it in "
        "dynamicProperties rather than looking it up again."
    )


def _partial_note(names: Sequence[str]) -> str:
    return (
        "Already in the DWC-A TERM LEDGER, not repeated: " + ", ".join(names) + ".\n\n"
    )


def plan_dwc_lookup(
    lookups: DwcLookups, tool_name: str, args: Dict[str, Any], ref: DwcReference
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Decide what part of a Darwin Core or extension lookup still needs running.

    Returns ``(args, prefix)``: run the tool with ``args`` and prepend ``prefix``
    to its result, or, when ``args`` is None, return ``prefix`` instead of
    running it because the ledger already holds everything requested.
    """
    args = dict(args)
    if tool_name == DWC_TERM_TOOL:
        requested = _requested_terms(args.get("terms"))
        if requested:
            covered = [term for term in requested if term_key(term) in lookups.terms]
            remaining = [term for term in requested if term_key(term) not in lookups.terms]
            if not remaining:
                return None, _covered_notice("these Darwin Core terms were")
            args["terms"] = remaining
            return args, _partial_note(covered) if covered else ""
        if args.get("section"):
            section = ref.dwc_section(str(args["section"]))
            if section and section[0] in lookups.sections:
                listed = lookups.sections[section[0]]
                limit = _section_listing_limit(args, section[1])
                if listed is None or (limit is not None and limit <= listed):
                    return None, _covered_notice(f"the Darwin Core {section[0]} section was")
        return args, ""

    if tool_name != EXTENSION_TOOL:
        return args, ""
    if not args.get("extension"):
        if lookups.catalogue:
            return None, _covered_notice("the extension catalogue was")
        return args, ""
    extension = ref.extension(str(args["extension"]))
    if not extension or extension[0] not in lookups.extensions:
        return args, ""
    key, _title, _cores, registered = extension
    defined = lookups.extensions[key]
    requested = _requested_terms(args.get("terms"))
    if not requested:
        return None, (
            _covered_notice(f"guidance for the `{key}` extension was")
            + f"\nRegistered `{key}` terms: {', '.join(registered)}"
        )
    covered = [term for term in requested if term_key(term) in defined]
    remaining = [term for term in requested if term_key(term) not in defined]
    if not remaining:
        return None, _covered_notice(f"these `{key}` term definitions were")
    args["terms"] = remaining
    return args, _partial_note(covered) if covered else ""


def _short_definition(definition: str) -> str:
    text = " ".join(str(definition or "").split())
    sentence_end = text.find(". ")
    if 0 < sentence_end < MAX_TERM_DEFINITION_CHARS:
        return text[: sentence_end + 1]
    if len(text) > MAX_TERM_DEFINITION_CHARS:
        return text[: MAX_TERM_DEFINITION_CHARS - 1].rstrip() + "…"
    return text


def render_dwc_ledger(lookups: DwcLookups, ref: DwcReference) -> str:
    if not lookups:
        return ""
    lines = [
        "DWC-A TERM LEDGER (Darwin Core and extension lookups already made in this task; they are "
        "not re-run, so map from these definitions and move on to building the projection):"
    ]
    budget = [MAX_LEDGER_TERM_DEFINITIONS]

    def term_lines(entries: Iterable[Tuple[str, str]]) -> List[str]:
        rendered, names_only = [], []
        for label, definition in entries:
            if budget[0] > 0:
                rendered.append(f"- {label}: {_short_definition(definition)}")
                budget[0] -= 1
            else:
                names_only.append(label)
        if names_only:
            rendered.append("- Also looked up (definitions omitted for space): " + ", ".join(names_only))
        return rendered

    if lookups.catalogue:
        catalogue = []
        for key in ref.extensions():
            extension = ref.extension(key)
            if extension:
                catalogue.append(f"{key} ({', '.join(extension[2]) or 'any core'})")
        lines.append("")
        lines.append("Extension catalogue (key and compatible cores): " + "; ".join(catalogue))
    if lookups.sections:
        lines.append("")
        lines.append(
            "Darwin Core sections listed: "
            + ", ".join(
                section if limit is None else f"{section} (first {limit} terms)"
                for section, limit in lookups.sections.items()
            )
        )
    if lookups.terms:
        lines.append("")
        lines.append("Darwin Core terms:")
        lines.extend(term_lines(
            (f"{name} ({section})", definition) for name, section, definition in lookups.terms.values()
        ))
    for key, defined in lookups.extensions.items():
        extension = ref.extension(key)
        if not extension:
            continue
        _key, title, cores, registered = extension
        lines.append("")
        lines.append(
            f"Extension `{key}` ({title}; cores: {', '.join(cores) or 'any'}; "
            f"{len(registered)} registered terms; guidance already shown):"
        )
        lines.extend(term_lines(defined.values()))
    return "\n".join(lines)


def latest_working_plan(openai_objs: Iterable[Dict[str, Any]]) -> str:
    plan = ""
    for name, args, result in iter_tool_calls(openai_objs):
        if name == PLAN_TOOL and result is not None and not result.startswith("ERROR CALLING FUNCTION"):
            plan = str(args.get("plan") or "").strip()[:MAX_PLAN_CHARS]
    return plan


def result_is_progress(tool_name: Optional[str], result: Any) -> bool:
    if tool_name in READ_ONLY_TOOLS:
        return False
    text = result if isinstance(result, str) else json.dumps(result)
    if text.startswith("ERROR CALLING FUNCTION"):
        return False
    success_prefixes = PROGRESS_RESULT_PREFIXES.get(tool_name or "")
    return success_prefixes is None or text.startswith(success_prefixes)


def turns_since_progress(entries: Iterable[Tuple[Any, Dict[str, Any]]], progress_floor=None) -> int:
    """Count tool-calling assistant turns since the last evidence of progress.

    ``entries`` are (created_at, openai_obj) pairs in chronological order.
    Progress is a user message, a result from a tool outside READ_ONLY_TOOLS
    that reports a change (see ``result_is_progress``), or ``progress_floor`` (e.g. the latest table revision, which catches
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
            is_progress = result_is_progress(call_names.get(str(obj.get("tool_call_id"))), obj.get("content"))
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


def render_schema_manifests(ledger: Dict[str, str], get_spec: Callable[[str], Any]) -> str:
    manifests = []
    for table, level in ledger.items():
        try:
            spec = get_spec(table)
        except KeyError:
            continue
        manifests.append(render_table_manifest(spec, include_fields=(level == LEVEL_FIELDS)))
    if not manifests:
        return ""
    return "\n\n".join([
        "DWC-DP SCHEMA LEDGER (authoritative for this task; these lookups are complete and are not "
        "re-run, so use them instead of GetDwcDpTableInfo or reading schema files):",
        *manifests,
    ])


def render_working_state(plan: str, coverage_open_items: Optional[str] = None) -> str:
    lines = []
    if plan:
        lines.append("Working plan (latest SetWorkingPlan; update it as writes complete):")
        lines.append(plan)
    if coverage_open_items is not None:
        if lines:
            lines.append("")
        lines.append(
            "Latest ReconcileSourceCoverage open items (everything else it checked is verified; "
            f"give these a disposition in the coverage report): {coverage_open_items}"
        )
    return "\n".join(lines)
