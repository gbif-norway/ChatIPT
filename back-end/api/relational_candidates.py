from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import pandas as pd


NULL_LIKE = {"", "nan", "none", "null", "<na>"}


@dataclass(frozen=True)
class ColumnSignal:
    table: str
    column: str
    populated: int
    distinct: int
    ordered_rows: int = 0


def _normalise_column(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _clean_values(series: pd.Series) -> pd.Series:
    values = series.dropna().astype(str).str.strip()
    return values[~values.str.casefold().isin(NULL_LIKE)]


def _signal(table, column: object, *, ordered: bool = False) -> ColumnSignal | None:
    values = _clean_values(table.df[column])
    if values.empty:
        return None
    ordered_rows = 0
    if ordered:
        ordered_rows = int(values.str.contains(r"\s*[|;]\s*|\s+&\s+", regex=True).sum())
    return ColumnSignal(
        table=table.title or "(untitled)",
        column=str(column),
        populated=int(len(values)),
        distinct=int(values.str.casefold().nunique()),
        ordered_rows=ordered_rows,
    )


def _columns_matching(
    tables: Iterable,
    names: set[str],
    *,
    ordered: bool = False,
    excluded_titles: set[str] | None = None,
) -> list[ColumnSignal]:
    signals = []
    excluded_titles = excluded_titles or set()
    for table in tables:
        if str(table.title or "").strip().casefold() in excluded_titles:
            continue
        if not isinstance(table.df, pd.DataFrame) or table.df.empty:
            continue
        for column in table.df.columns:
            if _normalise_column(column) not in names:
                continue
            signal = _signal(table, column, ordered=ordered)
            if signal:
                signals.append(signal)
    return signals


def _format_signal(signal: ColumnSignal) -> str:
    detail = f"{signal.populated:,} populated / {signal.distinct:,} distinct"
    if signal.ordered_rows:
        detail += f"; {signal.ordered_rows:,} ordered multi-value rows"
    return f"- `{signal.table}.{signal.column}`: {detail}"


def _candidate(
    family: str,
    strength: str,
    rationale: str,
    signals: list[ColumnSignal],
    table_titles: set[str],
    resources: set[str],
) -> dict:
    return {
        "family": family,
        "strength": strength,
        "rationale": rationale,
        "signals": signals,
        "present_resources": sorted(table_titles & resources),
    }


def find_relational_candidates(tables: Iterable) -> list[dict]:
    """Return compact, advisory evidence for potentially useful relational resources."""
    tables = list(tables)
    table_titles = {str(table.title).strip() for table in tables if table.title}
    candidates = []

    agent_name_fields = {
        "recordedby", "identifiedby", "collectedby", "eventconductedby",
        "georeferencedby", "samplingperformedby", "measurementdeterminedby",
    }
    agent_id_fields = {
        "recordedbyid", "identifiedbyid", "collectedbyid", "eventconductedbyid",
        "georeferencedbyid", "samplingperformedbyid", "measurementdeterminedbyid",
    }
    agent_resources = {
        "agent", "agent-agent-role", "event-agent-role", "identification-agent-role",
        "material-agent-role", "media-agent-role", "occurrence-agent-role",
        "organism-interaction-agent-role", "survey-agent-role",
    }
    agent_names = _columns_matching(
        tables, agent_name_fields, ordered=True, excluded_titles=agent_resources
    )
    agent_ids = _columns_matching(tables, agent_id_fields, excluded_titles=agent_resources)
    if agent_names or agent_ids:
        reused = any(signal.populated > signal.distinct and signal.populated >= 3 for signal in agent_names)
        ordered = any(signal.ordered_rows for signal in agent_names)
        strong = bool(agent_ids) or reused or ordered
        reasons = []
        if agent_ids:
            reasons.append("supplied agent identifiers")
        if reused:
            reasons.append("agents reused across records")
        if ordered:
            reasons.append("ordered multi-agent values")
        candidates.append(_candidate(
            "Agent / Agent Role",
            "strong" if strong else "possible",
            ", ".join(reasons) if reasons else "agent-like literal values",
            [*agent_names, *agent_ids],
            table_titles,
            agent_resources,
        ))

    identification_fields = {
        "identificationid", "identifiedby", "identifiedbyid", "dateidentified",
        "identificationremarks", "identificationqualifier", "identificationverificationstatus",
        "identificationmethod", "typestatus",
    }
    rank_fields = {
        "kingdom", "phylum", "class", "order", "family", "subfamily", "tribe",
        "genus", "subgenus", "specificepithet", "infraspecificepithet",
    }
    identification_resources = {
        "identification", "identification-agent-role", "identification-reference",
        "identification-taxon",
    }
    identification = _columns_matching(
        tables, identification_fields, excluded_titles=identification_resources
    )
    ranks = _columns_matching(tables, rank_fields, excluded_titles=identification_resources)
    substantive = [signal for signal in identification if _normalise_column(signal.column) != "typestatus"]
    if identification or len(ranks) >= 2:
        strong = bool(substantive) or len(ranks) >= 2
        reasons = []
        if substantive:
            reasons.append("determination-specific fields")
        if len(ranks) >= 2:
            reasons.append("supplied higher classification")
        if not reasons:
            reasons.append("type-status evidence")
        candidates.append(_candidate(
            "Identification",
            "strong" if strong else "possible",
            ", ".join(reasons),
            [*identification, *ranks],
            table_titles,
            identification_resources,
        ))

    protocol_fields = {
        "protocolid", "protocolname", "protocoldescription", "protocoltype",
        "samplingprotocol", "measurementmethod", "georeferenceprotocol",
        "identificationmethod", "method", "methodology",
    }
    protocol_resources = {
        "protocol", "event-protocol", "material-protocol", "molecular-protocol",
        "molecular-protocol-reference", "occurrence-protocol", "survey-protocol",
    }
    protocols = _columns_matching(tables, protocol_fields, excluded_titles=protocol_resources)
    if protocols:
        explicit_id = any(_normalise_column(signal.column) == "protocolid" for signal in protocols)
        structured = len({_normalise_column(signal.column) for signal in protocols}) >= 2
        candidates.append(_candidate(
            "Protocol",
            "strong" if explicit_id or structured else "possible",
            "protocol identifier or structured protocol fields" if explicit_id or structured else "method-like values",
            protocols,
            table_titles,
            protocol_resources,
        ))

    reference_fields = {
        "doi", "isbn", "referenceid", "bibliographiccitation", "associatedreferences",
        "references", "publication", "citation",
    }
    reference_resources = {
        "bibliographic-resource", "event-reference", "identification-reference",
        "material-reference", "occurrence-reference", "organism-reference",
        "protocol-reference", "survey-reference",
    }
    references = _columns_matching(tables, reference_fields, excluded_titles=reference_resources)
    if references:
        strong = any(_normalise_column(signal.column) in {"doi", "isbn", "referenceid"} for signal in references)
        strong = strong or len({_normalise_column(signal.column) for signal in references}) >= 2
        candidates.append(_candidate(
            "Bibliographic Resource / Reference",
            "strong" if strong else "possible",
            "stable or structured reference evidence" if strong else "reference-like literals",
            references,
            table_titles,
            reference_resources,
        ))

    usage_fields = {"license", "rights", "accessrights", "rightsholder", "usagepolicyid"}
    usage_resources = {"usage-policy", "material-usage-policy", "media-usage-policy"}
    usage = _columns_matching(tables, usage_fields, excluded_titles=usage_resources)
    if usage:
        varying = any(signal.distinct > 1 for signal in usage)
        structured = len({_normalise_column(signal.column) for signal in usage}) >= 2
        candidates.append(_candidate(
            "Usage Policy",
            "strong" if varying and structured else "possible",
            "varying entity-level rights fields" if varying and structured else "rights or licence values that may remain dataset metadata",
            usage,
            table_titles,
            usage_resources,
        ))

    provenance_fields = {
        "provenanceid", "derivedfrom", "lineage", "sourcesystem", "sourceversion",
        "processingstep", "provenance", "source",
    }
    provenance_resources = {
        "provenance", "event-provenance", "material-provenance", "media-provenance",
    }
    provenance = _columns_matching(
        tables, provenance_fields, excluded_titles=provenance_resources
    )
    if provenance:
        explicit = any(_normalise_column(signal.column) in {"provenanceid", "derivedfrom", "lineage"} for signal in provenance)
        structured = len({_normalise_column(signal.column) for signal in provenance}) >= 2
        candidates.append(_candidate(
            "Provenance",
            "strong" if explicit or structured else "possible",
            "explicit or structured lineage evidence" if explicit or structured else "source-like values",
            provenance,
            table_titles,
            provenance_resources,
        ))

    return candidates


def render_relational_candidate_report(tables: Iterable) -> str:
    candidates = find_relational_candidates(tables)
    lines = [
        "RELATIONAL MODELLING CANDIDATES — REVIEWER ATTENTION",
        "Advisory evidence only: use source meaning and DwC-DP schemas to decide whether dedicated resources help. This is not a completion gate.",
    ]
    if not candidates:
        lines.append("No strong or possible dedicated-resource candidates were detected by the lightweight scan.")
        return "\n".join(lines)

    strong = [candidate for candidate in candidates if candidate["strength"] == "strong"]
    possible = [candidate for candidate in candidates if candidate["strength"] == "possible"]
    for candidate in [*strong, *possible]:
        resources = candidate["present_resources"]
        present = ", ".join(f"`{name}`" for name in resources) if resources else "none"
        lines.append(
            f"\n{candidate['family']} — {candidate['strength'].upper()} evidence; "
            f"current related resources: {present}."
        )
        lines.append(f"Why surfaced: {candidate['rationale']}.")
        displayed = candidate["signals"][:5]
        lines.extend(_format_signal(signal) for signal in displayed)
        remaining = len(candidate["signals"]) - len(displayed)
        if remaining:
            lines.append(f"- …and {remaining} more matching populated field(s).")
    return "\n".join(lines)
