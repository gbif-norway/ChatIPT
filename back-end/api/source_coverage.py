"""Deterministic source-to-package coverage reconciliation.

Step 8 of the transformation task asks the agent to prove that every populated
source column reached the DwC-DP package (or was deliberately omitted). Doing
that through ad-hoc Python calls with truncated output took agents dozens of
turns and repeated checks. This module does the comparison in one pass:

* every populated source column is compared by distinct values against every
  destination resource field, after light normalisation (trim, whitespace,
  case-folding, numeric canonicalisation, ISO ``T`` date-time separator);
* values that were embedded into keys (``occurrence:ABC``) are found through a
  token index;
* a unique identifier column in each source table shows how many source rows
  are represented;
* constant values found in dataset metadata (EML, title, description) are
  reported as metadata-level dispositions.

Pure pandas; no Django imports so it can be unit tested in isolation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

MAPPED_RATE = 0.98
PARTIAL_RATE = 0.5
LOW_CARDINALITY = 3
OPEN_ITEMS_PREFIX = "COVERAGE OPEN ITEMS:"
STATE_PREFIX = "COVERAGE STATE:"
REPORT_PREFIX = "SOURCE COVERAGE CHECK"

_EMPTY = {"", "nan", "none", "null", "<na>", "nat"}
_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_DATETIME_T_RE = re.compile(r"(\d)T(\d)")
_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_SPLIT_RE = re.compile(r"[\s:|;,/=]+")
_IDENTIFIER_NAME_RE = re.compile(r"(?:id|identifier|number|code|key)$", re.IGNORECASE)


def normalize_value(value) -> Optional[str]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = _WHITESPACE_RE.sub(" ", str(value)).strip()
    if text.casefold() in _EMPTY:
        return None
    if _NUMBER_RE.match(text):
        try:
            number = float(text)
            if number == int(number) and abs(number) < 1e15:
                return str(int(number))
            return format(number, ".10g")
        except (ValueError, OverflowError):
            pass
    text = _DATETIME_T_RE.sub(r"\1 \2", text)
    return text.casefold()


def normalized_series(series: pd.Series) -> pd.Series:
    values = series.map(normalize_value)
    return values[values.notna()]


def _tokens(value: str) -> Iterable[str]:
    for token in _TOKEN_SPLIT_RE.split(value):
        if token and token != value:
            yield token


def _metadata_contains(metadata: str, value: str) -> bool:
    """Match a complete metadata value, not a short substring inside another word."""
    return bool(re.search(rf"(?<!\w){re.escape(value)}(?!\w)", metadata))


@dataclass
class ColumnResult:
    name: str
    populated: int
    distinct: int
    status: str  # mapped | partial | metadata | not_found | blank
    targets: List[Tuple[str, float, int]] = field(default_factory=list)  # (table.field, rate, dest populated)
    via_tokens: bool = False
    weak: bool = False
    missing_examples: List[str] = field(default_factory=list)
    example: str = ""


@dataclass
class SourceResult:
    label: str
    row_count: int
    columns: List[ColumnResult]
    identifier: Optional[Tuple[str, str, int, int]] = None  # (source col, target, found, checked)


class DestinationIndex:
    def __init__(self, destinations: Dict[str, pd.DataFrame]):
        self.populated: Dict[str, int] = {}
        self.distinct: Dict[str, int] = {}
        self.value_index: Dict[str, List[str]] = defaultdict(list)
        self.token_index: Dict[str, set] = defaultdict(set)
        for table_name, df in destinations.items():
            for column in df.columns:
                target = f"{table_name}.{column}"
                values = normalized_series(df[column])
                self.populated[target] = int(len(values))
                distinct = values.drop_duplicates()
                self.distinct[target] = int(len(distinct))
                for value in distinct:
                    self.value_index[value].append(target)
                    for token in _tokens(value):
                        self.token_index[token].add(target)

    def match_counts(self, values: Sequence[str], use_tokens: bool) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for value in values:
            targets = set(self.value_index.get(value, ()))
            if use_tokens:
                targets |= self.token_index.get(value, set())
            for target in targets:
                counts[target] += 1
        return counts


def _rank_targets(counts: Dict[str, int], total: int, source_name: str, index: DestinationIndex):
    def key(item):
        target, count = item
        same_name = target.split(".", 1)[1].casefold() == source_name.casefold()
        return (-count, not same_name, -index.populated.get(target, 0), target)

    ranked = sorted(counts.items(), key=key)
    return [(target, count / total, index.populated.get(target, 0)) for target, count in ranked]


def reconcile_table(
    label: str,
    df: pd.DataFrame,
    index: DestinationIndex,
    metadata_text: str = "",
) -> SourceResult:
    metadata = _WHITESPACE_RE.sub(" ", metadata_text).casefold()
    row_count = int(len(df.index))
    results: List[ColumnResult] = []
    identifier_candidates = []
    for column in df.columns:
        name = str(column)
        values = normalized_series(df[column])
        populated = int(len(values))
        if not populated:
            results.append(ColumnResult(name, 0, 0, "blank"))
            continue
        distinct_values = values.drop_duplicates()
        distinct = int(len(distinct_values))
        # The destination index already contains every distinct package value, so
        # sampling here would save little memory while allowing a late source value
        # to be incorrectly reported as mapped. Coverage is intentionally complete.
        sample = list(distinct_values)
        example = str(df[column].dropna().astype(str).str.strip().iloc[0])[:40] if populated else ""

        counts = index.match_counts(sample, use_tokens=False)
        ranked = _rank_targets(counts, len(sample), name, index)
        via_tokens = False
        if (
            (not ranked or ranked[0][1] < MAPPED_RATE)
            and _IDENTIFIER_NAME_RE.search(name)
        ):
            token_counts = index.match_counts(sample, use_tokens=True)
            token_ranked = _rank_targets(token_counts, len(sample), name, index)
            if token_ranked and (not ranked or token_ranked[0][1] > ranked[0][1]):
                ranked, via_tokens = token_ranked, True

        best_rate = ranked[0][1] if ranked else 0.0
        result = ColumnResult(name, populated, distinct, "not_found", via_tokens=via_tokens, example=example)
        if best_rate >= MAPPED_RATE:
            result.status = "mapped"
            result.targets = [t for t in ranked if t[1] >= MAPPED_RATE][:3]
        elif best_rate >= PARTIAL_RATE:
            result.status = "partial"
            result.targets = ranked[:1]
            best_target = ranked[0][0]
            result.missing_examples = [
                value for value in sample
                if best_target not in index.value_index.get(value, ())
                and (
                    not via_tokens
                    or best_target not in index.token_index.get(value, set())
                )
            ][:3]
        if result.status in {"mapped", "partial"} and distinct <= LOW_CARDINALITY:
            best_field = result.targets[0][0].split(".", 1)[1]
            result.weak = best_field.casefold() != name.casefold()
        if result.status in {"not_found", "partial"} and distinct <= LOW_CARDINALITY and metadata:
            if all(_metadata_contains(metadata, value) for value in sample):
                result.status = "metadata"
                result.targets = []
        results.append(result)

        if (
            populated == row_count
            and distinct == row_count
            and row_count > 1
            and result.targets
        ):
            target, rate, _destination_populated = result.targets[0]
            target_name = target.split(".", 1)[1]
            identifier_candidates.append((
                not bool(_IDENTIFIER_NAME_RE.search(name)),
                target_name.casefold() != name.casefold(),
                -rate,
                name,
                target,
                round(rate * len(sample)),
                len(sample),
            ))

    identifier = None
    if identifier_candidates:
        _, _, _, name, target, found, checked = min(identifier_candidates)
        identifier = (name, target, found, checked)
    return SourceResult(label, row_count, results, identifier)


def reconcile(
    sources: Sequence[Tuple[str, pd.DataFrame]],
    destinations: Dict[str, pd.DataFrame],
    metadata_text: str = "",
) -> List[SourceResult]:
    index = DestinationIndex(destinations)
    return [reconcile_table(label, df, index, metadata_text) for label, df in sources]


def _target_text(column: ColumnResult) -> str:
    parts = []
    for target, rate, dest_populated in column.targets:
        text = target
        if rate < 0.9995:
            text += f" {rate:.0%}"
        if dest_populated != column.populated:
            text += f" ({column.populated}->{dest_populated} values)"
        parts.append(text)
    suffix = ""
    if column.via_tokens:
        suffix += " [inside composite values]"
    if column.weak:
        suffix += " [weak: few distinct values, different field name]"
    return " | ".join(parts) + suffix


def render_report(
    results: Sequence[SourceResult],
    destinations: Dict[str, pd.DataFrame],
    state: str = "",
) -> str:
    lines = [
        f"{REPORT_PREFIX} (deterministic: distinct values of every populated source column compared "
        "with every package field after trimming, case-folding and numeric/date-time normalisation).",
        "Package resources: "
        + ", ".join(f"{name} ({len(df.index)} rows)" for name, df in destinations.items()),
    ]
    open_items: List[str] = []
    for source in results:
        lines.append("")
        lines.append(f"== {source.label} ({source.row_count} rows, {len(source.columns)} columns)")
        if source.identifier:
            name, target, found, checked = source.identifier
            sampled = "" if checked == source.row_count else f" (first {checked} of {source.row_count} checked)"
            lines.append(f"Rows: unique `{name}` -> {target}: {found}/{checked} values found{sampled}.")
        else:
            lines.append("Rows: no fully populated unique identifier column matched the package; row representation not checked.")
        grouped: Dict[str, List[ColumnResult]] = defaultdict(list)
        for column in source.columns:
            grouped[column.status].append(column)
        if grouped["mapped"]:
            lines.append(
                f"Mapped ({len(grouped['mapped'])}): "
                + "; ".join(f"{c.name} -> {_target_text(c)}" for c in grouped["mapped"])
            )
        if grouped["partial"]:
            lines.append(
                f"Partial ({len(grouped['partial'])}): "
                + "; ".join(
                    f"{c.name} -> {_target_text(c)}, e.g. missing {', '.join(repr(v) for v in c.missing_examples)}"
                    for c in grouped["partial"]
                )
            )
        if grouped["metadata"]:
            lines.append(
                f"Found only in dataset metadata ({len(grouped['metadata'])}): "
                + "; ".join(f"{c.name} ({c.example!r})" for c in grouped["metadata"])
            )
        if grouped["not_found"]:
            lines.append(
                f"Not found in package ({len(grouped['not_found'])}): "
                + "; ".join(
                    f"{c.name} ({c.populated} values, {c.distinct} distinct, e.g. {c.example!r})"
                    for c in grouped["not_found"]
                )
            )
        if grouped["blank"]:
            lines.append(f"Blank-only ({len(grouped['blank'])}): " + ", ".join(c.name for c in grouped["blank"]))
        for column in grouped["partial"] + grouped["not_found"] + [c for c in grouped["mapped"] if c.weak]:
            open_items.append(f"{source.label}: {column.name}")

    lines.append("")
    lines.append(
        "Use this as the evidence for the coverage report. Do not re-verify Mapped columns. Give every "
        "Partial, Not found and weak column a disposition from context you already have: mapped with a "
        "named transformation, deliberately omitted with a reason, or unresolved. Then save the report "
        "with SetStructureNotes."
    )
    lines.append(f"{OPEN_ITEMS_PREFIX} " + ("; ".join(open_items) if open_items else "(none)"))
    if state:
        lines.append(f"{STATE_PREFIX} {state}")
    return "\n".join(lines)


def coverage_state(
    tables: Iterable[Tuple[int, str, object]],
    user_files: Iterable[Tuple[int, str, object, object]] = (),
    metadata: Any = None,
) -> str:
    """Digest every persisted input that can change reconciliation output."""
    digest = hashlib.sha256()
    for table_id, title, updated_at in sorted(tables, key=lambda item: item[0]):
        digest.update(f"{table_id}|{title}|{updated_at}\n".encode("utf-8"))
    for file_id, filename, uploaded_at, source_manifest in sorted(user_files, key=lambda item: item[0]):
        digest.update(f"{file_id}|{filename}|{uploaded_at}|".encode("utf-8"))
        digest.update(
            json.dumps(source_manifest or {}, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
        )
        digest.update(b"\n")
    digest.update(json.dumps(metadata, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8"))
    return digest.hexdigest()[:16]


def latest_open_items(tool_results: Iterable[str]) -> Optional[str]:
    """The open-items line of the most recent coverage check, if any."""
    latest = None
    for result in tool_results:
        for line in (result or "").splitlines():
            if line.startswith(OPEN_ITEMS_PREFIX):
                latest = line[len(OPEN_ITEMS_PREFIX):].strip()
    return latest


def result_state(result: str) -> Optional[str]:
    for line in (result or "").splitlines():
        if line.startswith(STATE_PREFIX):
            return line[len(STATE_PREFIX):].strip()
    return None
