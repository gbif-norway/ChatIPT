"""Pure helpers for GBIF Alternative Identifiers and Literature References on Event/Occurrence cores.

Audited against registry XMLs e4e4b0b7aaaa-identifier.xml and a6d313919dbb-references.xml (the only
registry versions of these row types) and DwC-DP schema revision 76898192fd298c2aa170a7059e1bdadf3ee2a828.
No network calls, no Django state. Targets use occurrence-* as the neutral planning subject.
"""
from __future__ import annotations

from api.dwca_import import ImportFailure

DC = "http://purl.org/dc/terms/"
IDENTIFIER_ROW_TYPE = "http://rs.gbif.org/terms/1.0/Identifier"
REFERENCE_ROW_TYPE = "http://rs.gbif.org/terms/1.0/Reference"
REFERENCE_FAMILIES = {IDENTIFIER_ROW_TYPE: "identifier", REFERENCE_ROW_TYPE: "reference"}
SUBJECT_TABLES = ("occurrence", "event")

# Explicit, audited term IRIs only. Every other term of these extensions is preserved in the originals:
# Identifier: dcterms:title, dcterms:subject, dcterms:format (not identifierType), dwc:datasetID.
# Reference: dcterms:source, description, subject, language, rights, type, dwc:taxonRemarks, dwc:datasetID.
_TARGETS = {
    IDENTIFIER_ROW_TYPE: {
        DC + "identifier": "occurrence-identifier.identifier",
    },
    REFERENCE_ROW_TYPE: {
        DC + "identifier": "bibliographic-resource.referenceID",
        DC + "bibliographicCitation": "bibliographic-resource.bibliographicCitation",
        DC + "title": "bibliographic-resource.title",
        DC + "creator": "bibliographic-resource.author",
        DC + "date": "bibliographic-resource.issued",
    },
}
# Targets whose pinned field IRI differs from the source term. These are definition-based, not exact
# matches. The planner permits the two audited identifier aliases automatically;
# generic creator/date aliases still require a semantic decision.
NON_EXACT_TARGETS = {
    (IDENTIFIER_ROW_TYPE, DC + "identifier"): "Target field is skos:notation; the extension defines another identifier for the core record.",
    (REFERENCE_ROW_TYPE, DC + "identifier"): "Target field is dwc:referenceID; the extension defines an identifier of the referenced work.",
    (REFERENCE_ROW_TYPE, DC + "creator"): "Target field is dc:creator (elements namespace), not dcterms:creator.",
    (REFERENCE_ROW_TYPE, DC + "date"): "Target field is dcterms:issued; dcterms:date is generic and examples are not ISO dates.",
}


def _allowed_fields():
    allowed = {}
    for row_type, targets in _TARGETS.items():
        for target in targets.values():
            table, field = target.split(".", 1)
            allowed.setdefault(REFERENCE_FAMILIES[row_type], {}).setdefault(table, set()).add(field)
    return allowed


_FIELDS = _allowed_fields()


def reference_targets(row_type, term):
    target = _TARGETS.get(row_type, {}).get(term)
    return [target] if target else []


def _filled(value):
    return isinstance(value, str) and value.strip() != ""


def _subject_values(family, mapped, subject_table):
    if not isinstance(mapped, dict):
        raise ImportFailure("Mapped extension values must be a mapping of target tables to fields.")
    allowed = _FIELDS[family]
    collected = {}
    for table, fields in mapped.items():
        neutral = table
        prefix, _, rest = table.partition("-")
        if prefix in SUBJECT_TABLES and rest:
            if prefix != subject_table and prefix != "occurrence":
                raise ImportFailure(f"{table} values cannot describe a linked {subject_table}.")
            neutral = "occurrence-" + rest
        if not isinstance(fields, dict):
            raise ImportFailure(f"Mapped values for {table} must be a field mapping.")
        for field, value in fields.items():
            if field not in allowed.get(neutral, set()):
                raise ImportFailure(f"{table}.{field} is not an audited {family} extension target.")
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ImportFailure(f"{table}.{field} must be text.")
            previous = collected.setdefault(neutral, {}).setdefault(field, value)
            if previous != value:
                raise ImportFailure(f"Contradictory values for {field} in one {family} extension row.")
    return {table: {field: value for field, value in fields.items() if _filled(value)} for table, fields in collected.items()}


def emit_reference_records(family, mapped, subject_table, subject_key, record_key):
    if family not in _FIELDS:
        raise ImportFailure(f"Unsupported reference family: {family!r}.")
    if subject_table not in SUBJECT_TABLES:
        raise ImportFailure(f"{family} extension rows can only describe occurrences or events.")
    if not _filled(subject_key):
        raise ImportFailure(f"A {family} extension row does not resolve to a converted {subject_table}.")
    values = _subject_values(family, mapped, subject_table)
    if family == "identifier":
        identifier = values.get("occurrence-identifier", {}).get("identifier")
        if not identifier:
            raise ImportFailure("Alternative Identifiers rows require a nonempty dcterms:identifier.")
        return [(f"{subject_table}-identifier", {f"{subject_table}_fk": subject_key, "identifier": identifier})]
    if not _filled(record_key):
        raise ImportFailure("A literature reference row requires a deterministic record key.")
    bibliographic = values.get("bibliographic-resource", {})
    if not bibliographic:
        raise ImportFailure("A literature reference row has no mapped bibliographic value; map one or keep the table in the originals.")
    # Internal keys are separate from any supplied referenceID; relationshipType is never inferred.
    return [("bibliographic-resource", {"reference_pk": record_key, **bibliographic}),
            (f"{subject_table}-reference", {"reference_fk": record_key, f"{subject_table}_fk": subject_key})]
