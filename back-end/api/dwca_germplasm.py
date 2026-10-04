"""Pure helpers for the four germplasm extensions (Accession, Score, Trait, Trial).

Audited against registry XMLs ae90e94b2293-GermplasmAccession.xml, 2dadad759a37-MeasurementScore.xml,
e3c2cb4ff61a-MeasurementTrait.xml and 5014798f62ff-MeasurementTrial.xml and DwC-DP schema revision
76898192fd298c2aa170a7059e1bdadf3ee2a828. No network calls, no Django state. Every target is an explicit
alias: none of these source IRIs equals the pinned field IRI except dwc:locationID, whose meaning still
needs review. Trial terms are registered as measurementTrail* (sic); Score uses measurementTrial*.
"""
from __future__ import annotations

import re
import math

from api.dwca_import import DWC, ImportFailure

G = "http://purl.org/germplasm/germplasmTerm#"
GTYPE = "http://purl.org/germplasm/germplasmType#"
GEO = "http://www.w3.org/2003/01/geo/wgs84_pos#"
ACCESSION_ROW_TYPE = G + "GermplasmAccession"
SCORE_ROW_TYPE = G + "MeasurementScore"
TRAIT_ROW_TYPE = G + "MeasurementTrait"
TRIAL_ROW_TYPE = G + "MeasurementTrial"
GERMPLASM_FAMILIES = {
    ACCESSION_ROW_TYPE: "germplasm-accession",
    SCORE_ROW_TYPE: "germplasm-score",
    TRAIT_ROW_TYPE: "germplasm-trait",
    TRIAL_ROW_TYPE: "germplasm-trial",
}
SUBJECT_TABLES = {
    "germplasm-accession": ("material",),
    "germplasm-score": ("material", "occurrence", "event"),
    "germplasm-trait": ("protocol",),
    "germplasm-trial": ("event",),
}
_FK = {"material": "materialEntity_fk", "occurrence": "occurrence_fk", "event": "event_fk"}

# Audited passport statements. Each nonempty approved value becomes one material-assertion keyed by the
# exact source IRI. germplasmID, collectingInstituteID, locationID and geo:* are deliberately absent.
ACCESSION_STATEMENT_TERMS = tuple(G + name for name in (
    "germplasmIdentifier", "biologicalStatus", "breedingID", "breedingIdentifier", "breedingYear",
    "breedingCountry", "breedingCountryCode", "breedingInstituteID", "breedingInstitute", "breedingPerson",
    "ancestralData", "purdyPedigree", "breedingRemarks", "acquisitionID", "donorsID", "donorsIdentifier",
    "donorInstituteID", "donorInstitute", "acquisitionDate", "acquisitionSource", "acquisitionRemarks",
    "safetyDuplicationID", "safetyDuplicationDate", "safetyDuplicationInstituteID", "safetyDuplicationInstitute",
    "safetyDuplicationRemarks", "treatyOrRegulationID", "treatyOrRegulationName",
    "treatyOrRegulationGoverningBody", "mlsStatus")) + (GTYPE + "storageCondition",)

_STATEMENT_REASON = ("Proposal: one material-assertion on the approved material with assertionTypeIRI = this exact "
                     "source term IRI and the value verbatim. The term is a property, not a vetted measurement type. "
                     "Pedigrees, donors, acquisitions and safety duplicates create no materials, agents, events or "
                     "relationships.")
_MOF_REASON = ("Existing MoF pairing (measurement* to assertion*), not an IRI match. The planning table is "
               "occurrence-assertion; rows are written to the explicitly approved material, occurrence or event.")
_TRIAL_REASON = ("Trial rows describe the attached Event core only. Values patch empty or equal event fields; "
                 "conflicts are rejected and no event is created.")

_TARGETS = {
    ACCESSION_ROW_TYPE: {
        G + "germplasmID": ("material-identifier.identifier",
                            "germplasmID is an accession identifier, not skos:notation by IRI. Confirm it identifies the "
                            "approved material; a data.gbif.org/occurrences URL identifies an occurrence instead."),
    },
    SCORE_ROW_TYPE: {DWC + source: ("occurrence-assertion." + target, _MOF_REASON) for source, target in {
        "measurementID": "assertionID", "measurementType": "verbatimAssertionType", "measurementValue": "assertionValue",
        "measurementUnit": "assertionUnit", "measurementAccuracy": "assertionError",
        "measurementDeterminedDate": "assertionMadeDate", "measurementDeterminedBy": "assertionBy",
        "measurementMethod": "assertionProtocols", "measurementRemarks": "assertionRemarks",
    }.items()},
    TRAIT_ROW_TYPE: {term: ("protocol." + field, "Trait Descriptor rows are dataset-level method definitions. Proposal: "
                            "a protocol description with no occurrence, event or material link.")
                     for term, field in {
                         G + "measurementTraitID": "protocolID", G + "measurementTraitName": "protocolName",
                         G + "measurementTraitSource": "protocolReferences", G + "measurementTraitRemarks": "protocolRemarks",
                         DWC + "measurementMethod": "protocolDescription"}.items()},
    TRIAL_ROW_TYPE: {term: (target, _TRIAL_REASON) for term, target in {
        G + "measurementTrailID": "event-identifier.identifier",
        G + "measurementTrailIdentifier": "event.fieldNumber",
        G + "measurementTrailYear": "event.year",
        G + "measurementTrailRemarks": "event.eventRemarks",
        G + "measurementTrailReport": "bibliographic-resource.bibliographicCitation",
        DWC + "locationID": "event.locationID",
        GEO + "location": "event.locality",
    }.items()},
}

GERMPLASM_DERIVED_TERMS = {
    **{(ACCESSION_ROW_TYPE, term): _STATEMENT_REASON for term in ACCESSION_STATEMENT_TERMS},
    (SCORE_ROW_TYPE, G + "measurementTraitName"): (
        "Proposal: fills verbatimAssertionType only where dwc:measurementType is empty in that row. A differing "
        "measurementType is a contradiction and is rejected, never overwritten."),
    (TRIAL_ROW_TYPE, GEO + "lat"): "W3C geo latitude; patches decimalLatitude only together with geo:lon. No datum is inferred.",
    (TRIAL_ROW_TYPE, GEO + "lon"): "W3C geo longitude; patches decimalLongitude only together with geo:lat. No datum is inferred.",
    (TRIAL_ROW_TYPE, GEO + "alt"): "W3C geo altitude; one value patches both minimum and maximumElevationInMeters.",
}

# Columns kept in originals with an explanation for the reviewer.
_PRESERVE_REASONS = {
    (ACCESSION_ROW_TYPE, DWC + "locationID"): "Collecting site of the source material; never copied to an event.",
    (SCORE_ROW_TYPE, G + "germplasmID"): "Used by root as the material subject check only; ambiguous as an identifier.",
    (SCORE_ROW_TYPE, G + "measurementTraitID"): ("May be a trait ontology IRI or a Trait Descriptor key. Any protocol link "
                                                "is a separate reviewed decision; no protocol is created from Score."),
    (SCORE_ROW_TYPE, G + "measurementByInstituteID"): "An institute code; no agent records are inferred.",
}


def _allowed():
    allowed = {}
    for row_type, targets in _TARGETS.items():
        for target, _ in targets.values():
            table, field = target.split(".", 1)
            allowed.setdefault(GERMPLASM_FAMILIES[row_type], {}).setdefault(table, set()).add(field)
    return allowed


_FIELDS = _allowed()
_YEAR = re.compile(r"-?\d{1,4}")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def germplasm_targets(row_type, term, values):
    """Return (targets, review_reason) for one source column; exact registered IRIs only."""
    derived = GERMPLASM_DERIVED_TERMS.get((row_type, term))
    if derived:
        return [], derived
    target = _TARGETS.get(row_type, {}).get(term)
    if not target:
        return [], _PRESERVE_REASONS.get((row_type, term))
    if term == G + "germplasmID" and any("data.gbif.org/occurrences" in value for value in values):
        return [], "Values are GBIF occurrence URLs, which identify occurrences rather than this material. Originals retain them."
    return [target[0]], target[1]


def _filled(value):
    return isinstance(value, str) and value.strip() != ""


def _values(family, mapped, subject_table):
    if not isinstance(mapped, dict):
        raise ImportFailure("Mapped extension values must be a mapping of target tables to fields.")
    allowed = _FIELDS.get(family, {})
    collected = {}
    for table, fields in mapped.items():
        neutral = table
        if family == "germplasm-score" and table == f"{subject_table}-assertion":
            neutral = "occurrence-assertion"
        if not isinstance(fields, dict):
            raise ImportFailure(f"Mapped values for {table} must be a field mapping.")
        for field, value in fields.items():
            if field not in allowed.get(neutral, set()):
                raise ImportFailure(f"{table}.{field} is not an audited {family} target for a {subject_table} subject.")
            value = "" if value is None else value
            if not isinstance(value, str):
                raise ImportFailure(f"{table}.{field} must be text.")
            if collected.setdefault(neutral, {}).setdefault(field, value) != value:
                raise ImportFailure(f"Contradictory values for {field} in one {family} extension row.")
    return {table: {field: value for field, value in fields.items() if _filled(value)} for table, fields in collected.items()}


def _derived(row_type, source):
    if not isinstance(source, dict):
        raise ImportFailure("Source values must be a mapping of term IRIs to text.")
    values = {}
    for term, value in source.items():
        if (row_type, term) not in GERMPLASM_DERIVED_TERMS:
            continue
        if value is not None and not isinstance(value, str):
            raise ImportFailure(f"Source value for {term} must be text.")
        if _filled(value):
            values[term] = value
    return values


def _accession(source, values, subject_key):
    rows = []
    identifier = values.get("material-identifier", {}).get("identifier")
    if identifier:
        rows.append(("material-identifier", {"materialEntity_fk": subject_key, "identifier": identifier}))
    statements = _derived(ACCESSION_ROW_TYPE, source)
    # Registered order, one row per term; source multiplicity is kept by root (one call per source row).
    rows += [("material-assertion", {"materialEntity_fk": subject_key, "assertionTypeIRI": term, "assertionValue": statements[term]})
             for term in ACCESSION_STATEMENT_TERMS if term in statements]
    return rows


def _score(source, values, subject_table, subject_key):
    record = dict(values.get("occurrence-assertion", {}))
    name = _derived(SCORE_ROW_TYPE, source).get(G + "measurementTraitName")
    if name:
        if record.get("verbatimAssertionType", name) != name:
            raise ImportFailure("measurementType and measurementTraitName disagree in one Score row; keep one in the originals.")
        record["verbatimAssertionType"] = name
    if not record.get("assertionValue") or not record.get("verbatimAssertionType"):
        raise ImportFailure("A converted Score row needs a nonempty measurement value and type.")
    return [(f"{subject_table}-assertion", {_FK[subject_table]: subject_key, **record})]


def _trait(values, subject_key):
    description = values.get("protocol", {})
    if not {"protocolName", "protocolDescription", "protocolReferences", "protocolRemarks"} & set(description):
        raise ImportFailure("A Trait Descriptor row needs a name, method, source or remarks to become a protocol description.")
    return [("protocol", {"protocol_pk": subject_key, **description})]


def _number(term, value, low, high):
    if not _NUMBER.fullmatch(value) or not math.isfinite(float(value)) or not low <= float(value) <= high:
        raise ImportFailure(f"{term} value {value!r} is not a number in [{low}, {high}].")
    return value


def _trial(source, values, subject_key, record_key):
    patch = dict(values.get("event", {}))
    if "year" in patch and not _YEAR.fullmatch(patch["year"]):
        raise ImportFailure(f"measurementTrailYear {patch['year']!r} is not a year.")
    geo = _derived(TRIAL_ROW_TYPE, source)
    if (GEO + "lat" in geo) != (GEO + "lon" in geo):
        raise ImportFailure("Trial coordinates need both geo:lat and geo:lon.")
    if GEO + "lat" in geo:
        patch["decimalLatitude"] = _number("geo:lat", geo[GEO + "lat"], -90, 90)
        patch["decimalLongitude"] = _number("geo:lon", geo[GEO + "lon"], -180, 180)
    if GEO + "alt" in geo:
        patch["minimumElevationInMeters"] = patch["maximumElevationInMeters"] = _number("geo:alt", geo[GEO + "alt"], float('-inf'), float('inf'))
    rows = [("event", {"event_pk": subject_key, **patch})] if patch else []
    identifier = values.get("event-identifier", {}).get("identifier")
    if identifier:
        rows.append(("event-identifier", {"event_fk": subject_key, "identifier": identifier}))
    citation = values.get("bibliographic-resource", {}).get("bibliographicCitation")
    if citation:
        if not _filled(record_key):
            raise ImportFailure("A trial report requires a deterministic record key.")
        # relationshipType is never inferred; the report is not deduplicated across rows.
        rows += [("bibliographic-resource", {"reference_pk": record_key, "bibliographicCitation": citation}),
                 ("event-reference", {"reference_fk": record_key, "event_fk": subject_key})]
    return rows


def emit_germplasm_records(family, source, mapped, subject_table, subject_key, record_key):
    if family not in SUBJECT_TABLES:
        raise ImportFailure(f"Unsupported germplasm family: {family!r}.")
    if subject_table not in SUBJECT_TABLES[family]:
        raise ImportFailure(f"{family} rows cannot describe a {subject_table!r} subject.")
    if not _filled(subject_key):
        raise ImportFailure(f"A {family} row does not resolve to an approved {subject_table}.")
    values = _values(family, mapped, subject_table)
    if family == "germplasm-accession":
        rows = _accession(source, values, subject_key)
    elif family == "germplasm-score":
        rows = _score(source, values, subject_table, subject_key)
    elif family == "germplasm-trait":
        rows = _trait(values, subject_key)
    else:
        rows = _trial(source, values, subject_key, record_key)
    if not rows:
        raise ImportFailure(f"A {family} row has no approved value; keep the table in the originals instead.")
    return rows
