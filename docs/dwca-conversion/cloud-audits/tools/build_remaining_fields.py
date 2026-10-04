"""Rebuild the remaining-field audit from the worker's term decisions.

Run inside Docker (no host Python), from the repository root:

    docker compose run --rm --no-deps -v "$PWD":/repo \
        -v /path/to/registry-audit:/registry:ro --entrypoint python back-end \
        /repo/docs/dwca-conversion/cloud-audits/tools/build_remaining_fields.py \
        --registry /registry --schemas /app/api/templates/dwc-dp/table-schemas

Source descriptions, line numbers and target dcterms:isVersionOf values are read
from sources/extension/*.xml and the pinned table schemas, never typed by hand.
The script fails if a source term has no decision, a decision names a term that
is not in the XML, or a target table/field does not exist in the pinned schema.
"""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SRC = None
SCHEMAS = None
OUT = Path(__file__).resolve().parents[1] / "remaining-fields.json"
SCHEMA_REVISION = "76898192fd298c2aa170a7059e1bdadf3ee2a828"
NS = "{http://rs.gbif.org/extension/}"
DCT = "{http://purl.org/dc/terms/}"

DC = "http://purl.org/dc/terms/"
DWC = "http://rs.tdwg.org/dwc/terms/"
AC = "http://rs.tdwg.org/ac/terms/"
XMP = "http://ns.adobe.com/xap/1.0/"
GEO = "http://www.w3.org/2003/01/geo/wgs84_pos#"
IPTC = "http://iptc.org/std/Iptc4xmpExt/1.0/xmlns/"
BIBO = "http://purl.org/ontology/bibo/"
EOLR = "http://eol.org/schema/reference/"
GT = "http://purl.org/germplasm/germplasmTerm#"
BMDE = "http://www.birdscanada.org/bmde/"
NXF = "http://rs.nbn.org.uk/dwc/nxf/0.1/terms/"

MAP, COND, REVIEW, PRESERVE = "map", "conditional", "review", "preserve"


def load_schema(table):
    data = json.loads((SCHEMAS / f"{table}.json").read_text())
    return data, {f["name"]: f for f in data["fields"]}


SCHEMA_CACHE = {}


def target(spec):
    table, field = spec.split(".", 1)
    if table not in SCHEMA_CACHE:
        SCHEMA_CACHE[table] = load_schema(table)
    fields = SCHEMA_CACHE[table][1]
    if field not in fields:
        raise SystemExit(f"missing target {spec}")
    f = fields[field]
    return {
        "table": table,
        "field": field,
        "target_isVersionOf": f.get("dcterms:isVersionOf", ""),
        "target_required": bool(f.get("constraints", {}).get("required")),
        "target_type": f.get("type", ""),
        "evidence": f"back-end/api/templates/dwc-dp/table-schemas/{table}.json#{field}",
    }


def read_source(filename):
    path = SRC / filename
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    root = ET.parse(path).getroot()
    props = []
    for p in root.iter(NS + "property"):
        q = p.get("qualName")
        line = next(i + 1 for i, l in enumerate(lines) if f"qualName=\"{q}\"" in l or f"qualName='{q}'" in l)
        props.append({
            "source_iri": q,
            "source_name": p.get("name"),
            "source_required": p.get("required", "false") == "true",
            "source_type": p.get("type", ""),
            "source_group": p.get("group", ""),
            "source_evidence": f"sources/extension/{filename}:{line}",
            "source_definition": (p.get(DCT + "description") or "").strip(),
            "source_examples": p.get("examples", ""),
        })
    meta = {
        "row_type": root.get("rowType"),
        "title": root.get(DCT + "title"),
        "issued": root.get(DCT + "issued"),
        "source_file": f"sources/extension/{filename}",
    }
    return meta, props


def D(disp, targets=(), rule="", triggers=(), why="", prereq=()):
    return {"disposition": disp, "targets": list(targets), "value_rule": rule,
            "prerequisites": list(prereq), "review_triggers": list(triggers), "rationale": why}


ASSERT_PROPOSAL = ("Proposal only (not automatic): one {table} row with assertionTypeIRI = the source "
                   "term IRI and assertionValue = the value verbatim. The term IRI is a property, not a "
                   "vetted measurementType vocabulary term, so a human must accept this encoding.")


# --------------------------------------------------------------------------
# EOL Media Extension 1.0
# --------------------------------------------------------------------------
EOL_MEDIA_ROW = [
    "Archive core is dwc:Occurrence or dwc:Event and coreid resolves to exactly one emitted core record (gen-coreid-resolution).",
    "dcterms:type is not dcmitype:Text (Text rows are taxon/text accounts: whole row goes to review, see family review_triggers).",
    "One media row per extension row; media_pk minted (gen-weak-key-uniqueness); one occurrence-media or event-media link row to the core record with subject_basis=core_attachment.",
]
EOL_MEDIA = {
    DC + "identifier": D(COND, ["media.mediaID"],
        "Copy verbatim to mediaID when non-empty and unique within the media table; media_pk is always minted.",
        ["Duplicate identifier values within the file."],
        "Exact IRI: media.mediaID isVersionOf dcterms:identifier. media_pk, derivedFromMediaID, provenance_fk and usagePolicy_fk share that isVersionOf, so the field is chosen by its definition ('An identifier for an ac:Media resource'), not by IRI alone."),
    DWC + "taxonID": D(PRESERVE, [], "Preserve verbatim.",
        ["Any non-empty value: the media item is a taxon-page item; its depiction of the core occurrence/event is not established."],
        "No media or link-table field holds a taxon reference; material.taxonID exists but no material is in play. Must not create identification or occurrence rows."),
    DC + "type": D(COND, ["media.mediaType"],
        "Copy verbatim.",
        ["Value is http://purl.org/dc/dcmitype/Text or 'Text' -> whole row to review (bibliographic/text account, see media.json comments)."],
        "Exact IRI: media.mediaType isVersionOf dcterms:type."),
    "http://rs.tdwg.org/audubon_core/subtype": D(REVIEW, ["media.subtypeLiteral", "media.subtypeIRI"],
        "Proposal: literal values from the source list (Photograph, Map, ...) -> subtypeLiteral; absolute IRIs -> subtypeIRI.",
        ["Any non-empty value."],
        "NOT an exact match: source IRI is the pre-standard namespace http://rs.tdwg.org/audubon_core/subtype; media.subtypeIRI is a version of http://rs.tdwg.org/ac/terms/subtype and media.subtypeLiteral of ac:subtypeLiteral. Equivalence of the legacy namespace is an alias decision."),
    DC + "format": D(COND, ["media.formatIRI", "media.format"],
        "Absolute IRI (http/https/urn) -> formatIRI; any other value (e.g. 'image/jpeg', the source's own example) -> format. Never both.",
        [],
        "Exact IRI only for media.formatIRI (isVersionOf dcterms:format), but the EOL definition and example are MIME literals; media.format is the literal dc:format counterpart (isVersionOf http://purl.org/dc/elements/1.1/format). Note: av-media-direct-fields copies dcterms:format to formatIRI unconditionally; EOL must not reuse that rule."),
    IPTC + "CVterm": D(REVIEW, ["media.subjectCategoryIRI"],
        "Proposal: absolute IRI -> subjectCategoryIRI; literal -> subjectCategory.",
        ["Any non-empty value; values in http://rs.tdwg.org/ontology/voc/SPMInfoItems# (the source's example) state the item is a species-profile section, which is a strong signal the row is about a taxon, not the core record."],
        "NOT an exact match: source is Iptc4xmpExt:CVterm; media.subjectCategoryIRI is a version of ac:CVterm (same gap already noted for Audiovisual in extension-catalogue.json)."),
    DC + "title": D(MAP, ["media.title"], "Copy verbatim.", [], "Exact IRI: media.title isVersionOf dcterms:title."),
    DC + "description": D(MAP, ["media.description"], "Copy verbatim (no HTML stripping).", [],
        "Exact IRI: media.description isVersionOf dcterms:description. Only reached for non-Text rows."),
    AC + "accessURI": D(MAP, ["media.accessURI"], "Copy verbatim.", [], "Exact IRI."),
    "http://eol.org/schema/media/thumbnailURL": D(REVIEW, ["media.accessURI", "media.variantLiteral", "media.derivedFromMediaID"],
        "Default preserve. Proposal: a second media row (accessURI = thumbnail URL, variantLiteral = 'Thumbnail', derivedFromMediaID = first row's mediaID or media_pk).",
        ["Any non-empty value."],
        "No field on the same media row. A second media row is a real resource, but adding it is a modelling decision, so it needs approval."),
    AC + "furtherInformationURL": D(MAP, ["media.furtherInformationURL"], "Copy verbatim.", [], "Exact IRI."),
    AC + "derivedFrom": D(REVIEW, ["media.derivedFromMediaID"],
        "Default preserve. Proposal: when the value equals the mediaID of another emitted media row or is an absolute IRI, copy to derivedFromMediaID.",
        ["Any non-empty value."],
        "NOT an exact match: media.derivedFromMediaID isVersionOf dcterms:identifier; ac:derivedFrom is 'a reference to an original resource' and may be a citation rather than an identifier."),
    XMP + "CreateDate": D(MAP, ["media.createDate"], "Copy verbatim.", [], "Exact IRI."),
    DC + "modified": D(MAP, ["media.modified"], "Copy verbatim.", [], "Exact IRI."),
    DC + "language": D(COND, ["media.languageIRI", "media.language"],
        "Absolute IRI -> languageIRI; ISO 639 code or other literal (source example 'en') -> language.",
        [], "Exact IRI only for media.languageIRI (dcterms:language); source values are codes, which belong in the dc:language literal field media.language."),
    XMP + "Rating": D(MAP, ["media.rating"], "Copy verbatim (string field; -1..5 kept as text).", [], "Exact IRI."),
    DC + "audience": D(PRESERVE, [], "Preserve verbatim.", [], "No field with dcterms:audience in any pinned table (searched all 79 schemas)."),
    XMP + "rights/UsageTerms": D(COND, ["usage-policy.usageTerms"],
        "One usage-policy row per distinct tuple (UsageTerms, dcterms:rights, xmpRights:Owner); media.usagePolicy_fk references it. Do not also copy into usage-policy.license.",
        ["Required by the source but empty -> report as a source validity issue; do not invent a licence."],
        "Exact IRI: usage-policy.usageTerms isVersionOf xmpRights:UsageTerms."),
    DC + "rights": D(COND, ["usage-policy.rightsIRI", "usage-policy.rights"],
        "Absolute IRI -> rightsIRI; literal (source example 'Photo by Jane Doe') -> rights. Part of the usage-policy tuple.",
        [], "Exact IRI only for usage-policy.rightsIRI (dcterms:rights); usage-policy.rights is the dc:rights literal counterpart. Do not reinterpret attribution text as usage-policy.credit."),
    XMP + "rights/Owner": D(MAP, ["usage-policy.owner"], "Copy verbatim; part of the usage-policy tuple.", [], "Exact IRI."),
    DC + "bibliographicCitation": D(COND, ["provenance.bibliographicCitation"],
        "One provenance row per distinct tuple (bibliographicCitation, creator); media.provenance_fk references it.",
        [], "Exact IRI: provenance.bibliographicCitation isVersionOf dcterms:bibliographicCitation."),
    DC + "publisher": D(PRESERVE, [], "Preserve verbatim.", [],
        "No media/provenance/usage-policy field is a version of dcterms:publisher (provenance.providerLiteral is ac:providerLiteral, a different concept). Same disposition as GBIF Simple Multimedia in extension-notes.md."),
    DC + "contributor": D(PRESERVE, [], "Preserve verbatim.", [],
        "No field; media-agent-role would require agent rows, and agents are never created from names."),
    DC + "creator": D(COND, ["provenance.creator", "provenance.creatorID"],
        "Literal -> provenance.creator; absolute IRI -> provenance.creatorID. Part of the provenance tuple. No agent rows.",
        [], "Not IRI-identical: provenance.creator is a version of dc:creator (literal) and creatorID of dwc:agentID. Same literal/IRI split as rules av-provenance and gbif-mm-rights-provenance."),
    "http://eol.org/schema/agent/agentID": D(REVIEW, ["media-agent-role.agent_fk"],
        "Default preserve. Proposal only when the archive also contains EOL agent rows that are converted to agent rows by an approved rule: one media-agent-role row per agentID with agentRoleOrder = position in the cell; agentRole left empty.",
        ["Any non-empty value."],
        "Points to EOL Agent records (separate EOL extension, not in the audited set). media-agent-role.agent_fk and agentRoleOrder are required; the role is not stated."),
    IPTC + "LocationCreated": D(PRESERVE, [], "Preserve verbatim.", ["Non-empty value differing from the core event's locality."],
        "No media location field; must not be written to the core event (the instrument location is not the event location by definition)."),
    DC + "spatial": D(PRESERVE, [], "Preserve verbatim.", [], "No field; must not be written to event."),
    GEO + "lat": D(PRESERVE, [], "Preserve verbatim.", ["Non-empty coordinates far from the core event's coordinates."],
        "Media has no coordinate fields; copying to event.decimalLatitude would assert a new location for the event."),
    GEO + "long": D(PRESERVE, [], "Preserve verbatim.", [], "As geo:lat."),
    GEO + "alt": D(PRESERVE, [], "Preserve verbatim.", [], "As geo:lat."),
    EOLR + "referenceID": D(PRESERVE, [], "Preserve verbatim.",
        ["Value matches dcterms:identifier of a row in an EOL References file in the same archive."],
        "The pinned schema has no media-reference table (searched the 79 table names), so a media-to-reference link cannot be expressed."),
}

# --------------------------------------------------------------------------
# EOL References Extension 1.0
# --------------------------------------------------------------------------
EOL_REF_ROW = [
    "Archive core is dwc:Occurrence or dwc:Event and coreid resolves to exactly one emitted core record.",
    "One bibliographic-resource row per extension row (reference_pk minted), plus one occurrence-reference or event-reference row with relationshipType empty.",
    "References reachable only through EOL media eol:referenceID (no coreid on an Occurrence/Event core) are not linked: no media-reference table exists.",
]
EOL_REF = {
    DC + "identifier": D(COND, ["bibliographic-resource.referenceID"],
        "Copy verbatim when non-empty and unique within bibliographic-resource; reference_pk always minted.",
        ["Duplicate values with different citations."],
        "Not IRI-identical: referenceID isVersionOf dwc:referenceID. Same pairing as rule gbif-reference-bibliographic; the EOL definition ('An identifier for a resource that is referenced') matches the target definition."),
    EOLR + "publicationType": D(REVIEW, ["bibliographic-resource.referenceType"],
        "Proposal: copy verbatim to referenceType.",
        ["Any non-empty value."],
        "Not IRI-identical: target is dwc:referenceType ('A category that best matches the nature of a dcterms:BibliographicResource'). Semantically close, but this is an alias of an EOL-namespace term."),
    EOLR + "full_reference": D(REVIEW, ["bibliographic-resource.bibliographicCitation"],
        "Proposal: copy verbatim.",
        ["Any non-empty value."],
        "Not IRI-identical: target is dcterms:bibliographicCitation. Definitions agree ('A complete bibliographic citation'), but EOL also says its presence makes structured fields be ignored; reviewers must decide whether structured fields are still copied."),
    EOLR + "primaryTitle": D(PRESERVE, [], "Preserve verbatim.", ["Non-empty together with dcterms:title (article vs container title)."],
        "Container (journal/book) title. bibliographic-resource.title would conflict with dcterms:title; a container record via isPartOfReferenceID would be an invented record."),
    DC + "title": D(MAP, ["bibliographic-resource.title"], "Copy verbatim.", [], "Exact IRI: title isVersionOf dcterms:title."),
    BIBO + "pages": D(MAP, ["bibliographic-resource.pages"], "Copy verbatim.", [], "Exact IRI."),
    BIBO + "pageStart": D(REVIEW, ["bibliographic-resource.pages"],
        "Default preserve. Proposal when bibo:pages is empty: pages = pageStart + '-' + pageEnd.",
        ["Non-empty."], "No pageStart field; composing pages is a reformatting decision."),
    BIBO + "pageEnd": D(REVIEW, ["bibliographic-resource.pages"], "See bibo:pageStart.", ["Non-empty."], "No pageEnd field."),
    BIBO + "volume": D(MAP, ["bibliographic-resource.volume"], "Copy verbatim.", [], "Exact IRI."),
    BIBO + "edition": D(MAP, ["bibliographic-resource.edition"], "Copy verbatim.", [], "Exact IRI."),
    DC + "publisher": D(COND, ["bibliographic-resource.publisher", "bibliographic-resource.publisherID"],
        "Literal -> publisher; absolute IRI -> publisherID. No agent rows.",
        [], "Not IRI-identical: publisher is dc:publisher (literal) and publisherID dwc:agentID; standard dc/dcterms literal/IRI split."),
    BIBO + "authorList": D(REVIEW, ["bibliographic-resource.author"],
        "Proposal: copy verbatim (order preserved).",
        ["Any non-empty value."],
        "Not IRI-identical: author is dc:creator. bibo:authorList is an ordered list; the target accepts a name string but separator conventions are unknown."),
    BIBO + "editorList": D(REVIEW, ["bibliographic-resource.editor"],
        "Proposal: copy verbatim.",
        ["Any non-empty value."], "Not IRI-identical: editor is bibo:editor (single property), source is bibo:editorList."),
    DC + "created": D(PRESERVE, [], "Preserve verbatim.", ["Non-empty."],
        "No dcterms:created field. bibliographic-resource.issued is dcterms:issued; creation is not formal issuance and the EOL definition was copied from the media extension."),
    DC + "language": D(PRESERVE, [], "Preserve verbatim.", [], "bibliographic-resource has no language field."),
    BIBO + "uri": D(PRESERVE, [], "Preserve verbatim.", ["Non-empty."], "No URL field; referenceID is already taken by dcterms:identifier and must not be guessed from a URI."),
    BIBO + "doi": D(PRESERVE, [], "Preserve verbatim.", ["Non-empty."], "No DOI field; not substituted for referenceID."),
    "http://schemas.talis.com/2005/address/schema#localityName": D(PRESERVE, [], "Preserve verbatim.", [], "No place-of-publication field."),
}

# --------------------------------------------------------------------------
# Germplasm Accession (v20140515)
# --------------------------------------------------------------------------
GA_ROW = [
    "Archive core is dwc:Occurrence; coreid resolves to exactly one emitted occurrence; the core emitted exactly one material row for it (basisOfRecord in the specimen classes listed in core-notes.md).",
    "At most one Germplasm Accession row per occurrence; more than one -> review (which accession is the material?).",
    "No material row is created by this family. Event core or no material -> every term preserved.",
]


def ga_assert(term, why):
    return D(REVIEW, ["material-assertion.assertionTypeIRI", "material-assertion.assertionValue"],
             "Default preserve. " + ASSERT_PROPOSAL.format(table="material-assertion"), ["Non-empty."], why)


GA = {
    GT + "germplasmID": D(COND, ["material-identifier.identifier"],
        "One material-identifier row (identifier verbatim, identifierType empty) for the material emitted for the core occurrence. Do not overwrite material.materialEntityID.",
        ["material.materialEntityID empty: reviewer may promote the value to materialEntityID.",
         "Value is an http://data.gbif.org/occurrences/ URL (source example): that identifies a GBIF occurrence, not necessarily this material."],
        "Not IRI-identical (no field has germplasmID as isVersionOf). material-identifier.identifier (skos:notation) holds any identifier of the material without asserting it is the primary one."),
    GT + "germplasmIdentifier": ga_assert("germplasmIdentifier", "Accession name/designation (e.g. 'Emma'); not an identifier and not a scientific or vernacular name. No field."),
    GT + "biologicalStatus": ga_assert("biologicalStatus", "MCPD SAMPSTAT code/label. No field (extension-catalogue already notes this)."),
    "http://purl.org/germplasm/germplasmType#storageCondition": ga_assert("storageCondition",
        "MCPD STORAGE code. Note the different namespace (germplasmType#, not germplasmTerm#)."),
    GT + "collectingInstituteID": D(PRESERVE, [], "Preserve verbatim.", ["Non-empty."],
        "Collecting institute, not the holding institution; material.institutionID (dwc:institutionID) would be wrong. No collecting-agent field without agent rows."),
    GEO + "lat": D(PRESERVE, [], "Preserve verbatim.", ["Differs from the core event's decimalLatitude."],
        "Collecting site of the source germplasm; not IRI-equal to dwc:decimalLatitude and must not overwrite the core event."),
    GEO + "lon": D(PRESERVE, [], "Preserve verbatim.", ["As geo:lat."], "As geo:lat. Note 'lon' here vs 'long' in EOL Media."),
    GEO + "alt": D(PRESERVE, [], "Preserve verbatim.", [], "Altitude datum not stated; no elevation copy."),
    DWC + "locationID": D(REVIEW, ["event.locationID"],
        "Proposal: copy to the core occurrence's event.locationID only when that is empty and a reviewer confirms the core event is the collecting event.",
        ["Non-empty."],
        "Exact IRI (event.locationID isVersionOf dwc:locationID), but the definition here is the collecting site of the source material; the core event is not established as that event."),
}
for _n in ["breedingID", "breedingIdentifier", "breedingYear", "breedingCountry", "breedingCountryCode",
           "breedingInstituteID", "breedingInstitute", "breedingPerson", "ancestralData", "purdyPedigree",
           "breedingRemarks"]:
    GA[GT + _n] = ga_assert(_n, "Breeding history of the accession. No material field. Pedigrees must not become organism-relationship/resource-relationship rows: the parents are not records in the archive.")
for _n in ["acquisitionID", "donorsID", "donorsIdentifier", "donorInstituteID", "donorInstitute", "acquisitionDate",
           "acquisitionSource", "acquisitionRemarks"]:
    GA[GT + _n] = ga_assert(_n, "Acquisition by the genebank. No field. acquisitionID identifies an acquisition event, which must not be created; donorsID is the donor's material, not a derivation (material.derivedFromMaterialEntityID would assert one).")
for _n in ["safetyDuplicationID", "safetyDuplicationDate", "safetyDuplicationInstituteID", "safetyDuplicationInstitute",
           "safetyDuplicationRemarks"]:
    GA[GT + _n] = ga_assert(_n, "A safety duplicate is a separate material held elsewhere; creating that material would invent a record. No field.")
for _n in ["treatyOrRegulationID", "treatyOrRegulationName", "treatyOrRegulationGoverningBody", "mlsStatus"]:
    GA[GT + _n] = ga_assert(_n, "Access and benefit-sharing status. usage-policy.accessRights (dcterms:accessRights) is not a version of these terms and describes access to the record, not to the germplasm.")

# --------------------------------------------------------------------------
# Trait measurement score (v20140515)
# --------------------------------------------------------------------------
MS_ROW = [
    "Archive core is dwc:Occurrence; coreid resolves to exactly one emitted occurrence.",
    "Subject: material-assertion when the core emitted exactly one material row for the occurrence AND g:germplasmID is empty or equals that material's materialEntityID or a material-identifier value. subject_basis=explicit_germplasm_key.",
    "Otherwise (no material, or germplasmID points elsewhere): review; proposal occurrence-assertion with subject_basis=core_attachment. Never a new material, organism or occurrence.",
    "Event core: review (a score is about an accession, not about an event).",
]
MS = {
    DWC + "measurementID": D(COND, ["material-assertion.assertionID"], "Non-empty and unique within the chosen assertion table (gen-weak-key-uniqueness).", [], "Same pairing as rule mof-measurement-id (assertionID isVersionOf dwc:assertionID, not dwc:measurementID)."),
    DWC + "measurementValue": D(COND, ["material-assertion.assertionValue"], "Verbatim, no numeric coercion.", [], "Same pairing as rule mof-measurement-value."),
    DWC + "measurementUnit": D(COND, ["material-assertion.assertionUnit"], "Verbatim.", [], "Same pairing as rule mof-measurement-unit."),
    DWC + "measurementAccuracy": D(COND, ["material-assertion.assertionError"], "Verbatim.", [], "Same pairing as rule mof-measurement-accuracy."),
    DWC + "measurementDeterminedDate": D(COND, ["material-assertion.assertionMadeDate"], "Verbatim; assertionEffectiveDate empty.", [], "Same pairing as rule mof-determined-date."),
    DWC + "measurementDeterminedBy": D(COND, ["material-assertion.assertionBy"], "Verbatim; no agent rows.", [], "Same pairing as rule mof-determined-by."),
    DWC + "measurementType": D(COND, ["material-assertion.assertionType"], "Verbatim.", ["Empty while measurementTraitName is filled (see measurementTraitName)."], "Same pairing as rule mof-measurement-type."),
    DWC + "measurementMethod": D(COND, ["material-assertion.assertionProtocols"], "Verbatim; no protocol rows from free text.", [], "Same pairing as rule mof-method."),
    DWC + "measurementRemarks": D(COND, ["material-assertion.assertionRemarks"], "Verbatim.", [], "Same pairing as rule mof-remarks."),
    GT + "germplasmID": D(COND, [], "Consumed as the subject check (family row rule); value also preserved verbatim in the report.",
        ["Does not match the material emitted for the core occurrence."],
        "Key to the accession; no assertion field holds a subject identifier other than the FK."),
    GT + "germplasmIdentifier": D(PRESERVE, [], "Preserve verbatim.", [], "Accession name; no field."),
    GT + "measurementTraitID": D(REVIEW, ["material-assertion.assertionTypeIRI", "material-assertion.assertionProtocol_fk"],
        "Default preserve. Proposal A: absolute IRI of an ontology trait term -> assertionTypeIRI. Proposal B: when Trait Descriptor rows are approved as protocol rows, assertionProtocol_fk = the protocol_pk whose source measurementTraitID equals this value.",
        ["Non-empty."],
        "Defined as the identifier of a trait descriptor (method description); whether it names the measured property or the method is dataset-specific."),
    GT + "measurementTraitIdentifier": D(PRESERVE, [], "Preserve verbatim; usable as a join key to Trait Descriptor rows.", [], "Local code; no field."),
    GT + "measurementTraitName": D(REVIEW, ["material-assertion.assertionType"],
        "Proposal only when dwc:measurementType is empty: assertionType = value.",
        ["Non-empty."], "Name of the measurement method/trait; not IRI-identical to dwc:assertionType."),
    GT + "measurementTrialID": D(PRESERVE, [], "Preserve verbatim.", ["Non-empty and no Trial rows match it (Trial uses measurementTrailID, see family notes)."],
        "Links to a trial (event-like). Assertions have no event FK; trials are not converted to events."),
    GT + "measurementTrialIdentifier": D(PRESERVE, [], "Preserve verbatim.", [], "As measurementTrialID."),
    GT + "measurementByInstituteID": D(REVIEW, ["material-assertion.assertionByID"],
        "Default preserve. Proposal only if agent rows exist for institute codes under the core agent policy.",
        ["Non-empty."], "assertionByID is a version of dwc:agentID; an FAO WIEWS code is not an agent row."),
    GT + "measurementGrowthStage": D(PRESERVE, [], "Preserve verbatim.", ["Non-empty."],
        "Plant stage at measurement time. occurrence.lifeStage describes the organism at the occurrence, not at a later trial measurement."),
}

# --------------------------------------------------------------------------
# Trait descriptor (v20140508/20140515)
# --------------------------------------------------------------------------
MT_ROW = [
    "Rows are dataset-level trait definitions; attachment to a core record has no meaning for them. No occurrence-protocol, event-protocol or material-protocol link is created.",
    "Whole family is review. Proposal: one protocol row per distinct measurementTraitID (else measurementTraitIdentifier), protocol_pk minted, referenced from Score assertions via assertionProtocol_fk only after approval.",
]


def mt(targets, rule, why):
    return D(REVIEW if targets else PRESERVE, targets, rule, ["Family-level approval required."], why)


MT = {
    GT + "measurementTraitID": mt(["protocol.protocolID"], "Proposal: copy verbatim.", "Not IRI-identical (protocolID isVersionOf dwc:protocolID)."),
    GT + "measurementTraitIdentifier": mt([], "Preserve; join key for Score rows.", "Local code; no protocol field other than protocolID."),
    GT + "measurementTraitName": mt(["protocol.protocolName"], "Proposal: copy verbatim.", "Not IRI-identical (protocolName isVersionOf dcterms:title)."),
    GT + "measurementTraitCategory": mt([], "Preserve.", "protocolType examples ('measurement', 'georeference', ...) describe protocol kinds, not trait categories ('morphological', 'phenological')."),
    GT + "measurementTraitScale": mt([], "Preserve.", "No scale field on protocol."),
    GT + "measurementTraitSource": mt(["protocol.protocolReferences"], "Proposal: copy verbatim.", "Not IRI-identical (eco:protocolReferences); a descriptor standard citation fits 'BibliographicResources used in a dwc:Protocol'."),
    GT + "measurementTraitRemarks": mt(["protocol.protocolRemarks"], "Proposal: copy verbatim.", "Not IRI-identical (dwc:protocolRemarks)."),
    DWC + "measurementType": mt([], "Preserve.", "Exact dwc IRI, but there is no assertion on this row: it names what the trait measures. Writing an assertion would invent a measurement."),
    DWC + "measurementMethod": mt(["protocol.protocolDescription"], "Proposal: copy verbatim.", "Not IRI-identical (dwc:protocolDescription)."),
}

# --------------------------------------------------------------------------
# Trait measurement trial (v20140508/20140515)
# --------------------------------------------------------------------------
TR_ROW = [
    "All five germplasm terms are spelled 'measurementTrail...' (sic) in the registered XML; Score uses 'measurementTrial...'. Match by the exact IRIs listed here; never treat one spelling as the other's term.",
    "Event core: whole family review. Proposal: the trial row describes the core event it is attached to (subject_basis=core_attachment, approved), and fills only empty event fields. Never creates events.",
    "Occurrence core: preserve every term (a trial is not an occurrence, and creating an event for it would invent a record).",
]
TR = {
    GT + "measurementTrailID": D(REVIEW, ["event-identifier.identifier"], "Proposal: one event-identifier row.", ["Non-empty."], "No event field is a version of this IRI."),
    GT + "measurementTrailIdentifier": D(REVIEW, ["event.fieldNumber"], "Default preserve. Proposal: copy to fieldNumber if empty.", ["Non-empty."], "Not IRI-identical (dwc:fieldNumber)."),
    GT + "measurementTrailYear": D(REVIEW, ["event.year"], "Proposal: copy when event.year and eventDate are empty or consistent.", ["Non-empty."], "Not IRI-identical (dwc:year)."),
    GT + "measurementTrailReport": D(REVIEW, ["bibliographic-resource.bibliographicCitation", "event-reference.reference_fk"],
        "Proposal: one bibliographic-resource (citation or URL verbatim) + event-reference, relationshipType empty.", ["Non-empty."], "No direct event field."),
    GT + "measurementTrailRemarks": D(REVIEW, ["event.eventRemarks"], "Proposal: append to eventRemarks only if empty.", ["Non-empty."], "Not IRI-identical (dwc:eventRemarks)."),
    DWC + "locationID": D(REVIEW, ["event.locationID"], "Proposal: copy when event.locationID is empty or equal.", ["Conflicts with core locationID."],
        "Exact IRI, but only reachable after the family-level decision that the trial describes the core event."),
    GEO + "location": D(REVIEW, ["event.locality"], "Proposal: copy when locality empty.", [], "Not IRI-identical (dwc:locality)."),
    GEO + "lon": D(REVIEW, ["event.decimalLongitude"], "Proposal: copy when both coordinates empty on the event.", ["Coordinates conflict with core."], "Not IRI-identical (dwc:decimalLongitude)."),
    GEO + "lat": D(REVIEW, ["event.decimalLatitude"], "As geo:lon.", ["As geo:lon."], "Not IRI-identical (dwc:decimalLatitude)."),
    GEO + "alt": D(REVIEW, ["event.minimumElevationInMeters", "event.maximumElevationInMeters"], "Proposal: both fields = value when empty.", [], "Not IRI-identical; unit not stated in the definition (example 100)."),
}

# --------------------------------------------------------------------------
# Bird Monitoring Data Exchange (sandbox 2020-10-19)
# --------------------------------------------------------------------------
BM_ROW = [
    "dc:subject of the extension is dwc:Occurrence. Occurrence core: one BMDE row per occurrence; more than one -> review.",
    "Event core: Occurrence-group and Taxon-group terms have no subject; preserve them. Event-group terms remain review.",
    "No survey, event, occurrence, organism or identification rows are created by this family.",
]


def bm(disp, targets=(), rule="Preserve verbatim.", triggers=("Non-empty.",), why=""):
    return D(disp, targets, rule, triggers, why)


BM = {
    BMDE + "ProjectCode": bm(REVIEW, ["provenance.projectID"], "Proposal: copy to provenance.projectID.", why="Not IRI-identical (dwc:projectID); a code, not necessarily an identifier."),
    BMDE + "ProtocolCode": bm(REVIEW, ["protocol.protocolName"], "Proposal: one protocol per distinct code, linked by event.eventProtocol_fk.", why="Not IRI-identical."),
    BMDE + "ProtocolSpeciesTargeted": bm(REVIEW, ["survey.verbatimTargetScope"], "Proposal only if a survey exists for the event (Humboldt).", why="Not IRI-identical (eco:verbatimTargetScope); creating a survey would assert survey semantics."),
    BMDE + "ProtocolReference": bm(REVIEW, ["protocol.protocolReferences"], "With ProtocolCode proposal.", why="Not IRI-identical."),
    BMDE + "ProtocolURL": bm(REVIEW, ["protocol.protocolReferences"], "With ProtocolCode proposal.", why="Not IRI-identical."),
    BMDE + "SurveyAreaIdentifier": bm(REVIEW, ["event.siteNumber"], "Proposal: copy when empty.", why="Not IRI-identical (dwc:siteNumber)."),
    BMDE + "SurveyAreaSize": bm(REVIEW, ["event-assertion.assertionValue"], "Proposal: event-assertion, unit 'ha' taken from the definition.", why="Hectares per definition; survey.totalAreaSampledValue would require a survey."),
    BMDE + "SurveyAreaPercentageCovered": bm(REVIEW, ["event-assertion.assertionValue"], "Proposal: event-assertion (unit '%'). Missing means 100% per definition; do not write 100.", why="No field."),
    BMDE + "SurveyAreaShape": bm(REVIEW, ["event-assertion.assertionValue"], "Proposal: event-assertion.", why="No field."),
    BMDE + "SurveyAreaLongAxisLength": bm(REVIEW, ["event-assertion.assertionValue"], "Proposal: event-assertion (unit 'm').", why="No field."),
    BMDE + "SurveyAreaShortAxisLength": bm(REVIEW, ["event-assertion.assertionValue"], "Proposal: event-assertion (unit 'm').", why="No field."),
    BMDE + "SurveyAreaLongAxisOrientation": bm(REVIEW, ["event-assertion.assertionValue"], "Proposal: event-assertion (unit 'degrees').", why="No field."),
    BMDE + "CoordinatesScope": bm(REVIEW, ["event.georeferenceRemarks"], "Proposal: georeferenceRemarks.", why="Changes what the event coordinates mean (route start, county centroid). Always review."),
    BMDE + "SamplingEventStructure": bm(PRESERVE, why="Describes identifier syntax; no field. Must not be used to infer parentEvent_fk."),
    BMDE + "RouteIdentifier": bm(PRESERVE, why="Higher sampling unit; creating a parent event would invent a record."),
    BMDE + "TimeObservationsStarted": bm(REVIEW, ["event.eventTime"], "Proposal: convert decimal hours to hh:mm without time zone.", why="Not IRI-identical; local time with no offset; values describe the whole event, not this record."),
    BMDE + "TimeObservationsEnded": bm(REVIEW, ["event.eventTime"], "With TimeObservationsStarted as an interval.", why="As above."),
    BMDE + "DurationInHours": bm(REVIEW, ["event-assertion.assertionValue"], "Proposal: event-assertion (unit 'h').", why="survey.eventDurationValue needs a survey."),
    BMDE + "TimeIntervalStarted": bm(REVIEW, ["occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion (unit 'h').", why="No field."),
    BMDE + "TimeIntervalEnded": bm(REVIEW, ["occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion (unit 'h').", why="No field."),
    BMDE + "TimeIntervalsAdditive": bm(REVIEW, ["event-assertion.assertionValue"], "Proposal: event-assertion.", why="Affects summability of counts."),
    BMDE + "NumberOfObservers": bm(REVIEW, ["event-assertion.assertionValue"], "Proposal: event-assertion.", why="No field."),
    BMDE + "NoObservations": bm(PRESERVE, [], "Preserve.", ("Value 'NoObs'.",), "Marks a no-detection sampling record. Must not produce occurrenceStatus='absent' for any taxon automatically."),
    BMDE + "DistanceFromObserver": bm(REVIEW, ["occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion (unit 'm').", why="No field."),
    BMDE + "DistanceFromObserverMin": bm(REVIEW, ["occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion (unit 'm').", why="No field."),
    BMDE + "DistanceFromObserverMax": bm(REVIEW, ["occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion (unit 'm'; 'Unlimited' kept verbatim).", why="No field."),
    BMDE + "DistanceFromStart": bm(REVIEW, ["occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion (unit 'm').", why="No field."),
    BMDE + "BearingInDegrees": bm(REVIEW, ["occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion (unit 'degrees').", why="Reference bearing is protocol-specific."),
    BMDE + "SpecimenDecimalLatitude": bm(PRESERVE, why="Record-level position when the event coordinates are a centroid. Occurrence has no coordinates; a new event would be invented."),
    BMDE + "SpecimenDecimalLongitude": bm(PRESERVE, why="As SpecimenDecimalLatitude."),
    BMDE + "SpecimenGeodeticDatum": bm(PRESERVE, why="As SpecimenDecimalLatitude."),
    BMDE + "SpecimenUTMZone": bm(PRESERVE, why="As SpecimenDecimalLatitude."),
    BMDE + "SpecimenUTMNorthing": bm(PRESERVE, why="As SpecimenDecimalLatitude."),
    BMDE + "SpecimenUTMEasting": bm(PRESERVE, why="As SpecimenDecimalLatitude."),
    DWC + "individualCount": D(COND, ["occurrence.organismQuantity", "occurrence.organismQuantityType"],
        "Apply core rule quantity.individualCount to the occurrence when the core row has no individualCount; if the core also has one, equal values discharge and different values go to review.",
        ["ObservationDescriptor = 'Presence/Absence' (1 means presence, not one individual) -> review, do not write 'individuals'."],
        "Exact dwc IRI; occurrence has no individualCount field (core-catalogue quantity.individualCount)."),
    BMDE + "ObservationDescriptor": bm(REVIEW, ["occurrence.organismQuantityType"], "Proposal: replaces 'individuals' when it names a count type (e.g. 'TotalCount').",
        ("Non-empty.", "'Presence/Absence'."), "Qualifies individualCount; may be a distance/time band instead of a count type."),
    BMDE + "ObsCountAtLeast": bm(REVIEW, ["occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion.", why="Range count; organismQuantity cannot hold a range without changing meaning."),
    BMDE + "ObsCountAtMost": bm(REVIEW, ["occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion.", why="As ObsCountAtLeast."),
    BMDE + "DateUncertaintyInDays": bm(REVIEW, ["event.eventDate"], "Proposal: reviewer may widen eventDate to an interval.", why="No field; changes the event date."),
    BMDE + "AllIndividualsReported": bm(REVIEW, ["survey.isAbundanceReported"], "Proposal only with an existing survey.", why="Absence/abundance inference; not IRI-identical."),
    BMDE + "AllSpeciesReported": bm(REVIEW, ["survey.taxonCompletenessReported"], "Proposal only with an existing survey.", why="Controls absence inference; not IRI-identical."),
    BMDE + "UTMZone": bm(REVIEW, ["event.verbatimCoordinates", "event.verbatimCoordinateSystem"], "Proposal: verbatimCoordinates = zone + easting + northing, verbatimCoordinateSystem = 'UTM' when the event's are empty.", why="Not IRI-identical; composition."),
    BMDE + "UTMNorthing": bm(REVIEW, ["event.verbatimCoordinates"], "With UTMZone.", why="As UTMZone."),
    BMDE + "UTMEasting": bm(REVIEW, ["event.verbatimCoordinates"], "With UTMZone.", why="As UTMZone."),
    BMDE + "CoordinatesUncertaintyInDecimalDegrees": bm(PRESERVE, why="Converting degrees to coordinateUncertaintyInMeters depends on latitude and axis; not lossless."),
    BMDE + "RecordPermissions": bm(REVIEW, ["occurrence.informationWithheld"], "Preserve; reviewer writes a statement if needed.", ("Any value other than 5 (public).",),
        "Access level 1-5; a value below 5 means restricted display. Must be resolved before publication."),
    BMDE + "TaxonomicAuthorityVersion": bm(PRESERVE, why="Taxon-linked; no identification is created from it."),
    BMDE + "TaxonomicAuthorityYear": bm(PRESERVE, why="Taxon-linked."),
    BMDE + "BreedingBirdAtlasCode": bm(REVIEW, ["occurrence.behavior", "occurrence-assertion.assertionValue"], "Proposal: occurrence-assertion with the NORAC code verbatim.", why="Not IRI-identical; codes combine behaviour and breeding evidence."),
    BMDE + "PrimaryMarkerType": bm(REVIEW, ["organism-identifier.identifierType"], "Proposal only when an organism row exists for the occurrence.", why="Band type of a marked bird."),
    BMDE + "PrimaryMarkerNumber": bm(REVIEW, ["organism-identifier.identifier"], "With PrimaryMarkerType.", why="Band number identifies the organism; needs an existing organism row (no invention)."),
    BMDE + "AuxiliaryMarkerType": bm(REVIEW, ["organism-identifier.identifierType"], "As PrimaryMarkerType.", why="As PrimaryMarkerType."),
    BMDE + "AuxiliaryMarkerNumber": bm(REVIEW, ["organism-identifier.identifier"], "As PrimaryMarkerNumber.", why="As PrimaryMarkerNumber."),
    BMDE + "LastModifiedAction": bm(PRESERVE, [], "Preserve.", ("Value 'DELETE' -> the occurrence must be reviewed before publication.",), "Record-management state; no field."),
    BMDE + "RecordReviewStatus": bm(REVIEW, ["occurrence.identificationVerificationStatus"], "Preserve; reviewer decides.", ("'not accepted' or 'pending review'.",),
        "Field does not exist on occurrence; status could be about the record, not the identification."),
}
for _i in range(1, 19):
    BM[BMDE + f"EffortMeasurement{_i}"] = bm(REVIEW, ["event-assertion.assertionValue"], f"Proposal: event-assertion with unit from EffortUnits{_i}.", why="Effort; survey.samplingEffortValue holds one value and needs a survey.")
    BM[BMDE + f"EffortUnits{_i}"] = bm(REVIEW, ["event-assertion.assertionUnit"], f"With EffortMeasurement{_i}.", why="Unit for the paired effort.")
for _i in range(2, 15):
    BM[BMDE + f"ObservationCount{_i}"] = bm(REVIEW, ["occurrence-assertion.assertionValue"], f"Proposal: occurrence-assertion, assertionType = ObservationDescriptor{_i}.", why="Sub-count (band/interval) of the same occurrence.")
    BM[BMDE + f"ObservationDescriptor{_i}"] = bm(REVIEW, ["occurrence-assertion.assertionType"], f"With ObservationCount{_i}.", why="Describes the paired count.")
for _i in range(1, 7):
    BM[BMDE + f"MultiScientificName{_i}"] = bm(PRESERVE, triggers=("Non-empty.",), why="Candidate taxa for a group record. Must not become identifications or occurrences.")
_MOF_PAIR = {"MeasurementType": ("assertionType", "mof-measurement-type"), "MeasurementValue": ("assertionValue", "mof-measurement-value"),
             "MeasurementAccuracy": ("assertionError", "mof-measurement-accuracy"), "MeasurementUnit": ("assertionUnit", "mof-measurement-unit"),
             "MeasurementDeterminedBy": ("assertionBy", "mof-determined-by"), "MeasurementMethod": ("assertionProtocols", "mof-method")}
for _i in range(1, 13):
    for _k, (_f, _rule) in _MOF_PAIR.items():
        BM[BMDE + f"{_k}{_i}"] = D(COND, [f"occurrence-assertion.{_f}"],
            f"Occurrence core only: one occurrence-assertion per populated group {_i} (MeasurementType{_i} or MeasurementValue{_i} non-empty); verbatim. Event core: preserve.",
            ["mof-subject-retarget-review triggers apply."],
            f"Unpivoted MoF group; field pairing as rule {_rule}. Source IRIs are bmde:, not dwc:, so this is a documented equivalence, not an exact match.")

# --------------------------------------------------------------------------
# NBN eXchange Format (sandbox draft)
# --------------------------------------------------------------------------
NX_ROW = ["Occurrence core: one NXF row per occurrence (fields describe its event). Event core: same terms describe the event.",
          "No events are created; event fields are filled only on the event already emitted for the core record."]
NX = {
    NXF + "eventDateTypeCode": D(REVIEW, ["event.eventDate"], "Proposal: with eventDateStart/End, eventDate = 'start/end' (or 'start' when equal) when the core eventDate is empty or consistent.",
        ["Code not in the NBN vague-date list.", "Core eventDate conflicts."], "Not IRI-identical; required by the source."),
    NXF + "eventDateStart": D(REVIEW, ["event.eventDate"], "See eventDateTypeCode.", ["Conflict with core eventDate."], "Not IRI-identical (dwc:eventDate)."),
    NXF + "eventDateEnd": D(REVIEW, ["event.eventDate"], "See eventDateTypeCode.", ["As eventDateStart."], "Not IRI-identical."),
    NXF + "gridReference": D(REVIEW, ["event.verbatimCoordinates"], "Proposal: copy when empty.", ["Core coordinates conflict with the grid square."], "Not IRI-identical (dwc:verbatimCoordinates)."),
    NXF + "gridReferenceType": D(REVIEW, ["event.verbatimCoordinateSystem"], "Proposal: copy (BNG/ING/UTM) when empty.", [], "Not IRI-identical (dwc:verbatimCoordinateSystem)."),
    NXF + "gridReferencePrecision": D(REVIEW, ["event.coordinateUncertaintyInMeters"], "Default preserve.", ["Non-empty."],
        "Grid square side length is not an uncertainty radius; conversion needs a reviewer."),
    NXF + "siteFeatureKey": D(REVIEW, ["event.locationID"], "Proposal: copy when locationID empty.", ["Non-empty."], "NBN Gateway site key; not IRI-identical."),
    NXF + "sensitiveOccurrence": D(REVIEW, ["occurrence.informationWithheld", "occurrence.dataGeneralizations"], "Preserve; reviewer writes statements.",
        ["Value true -> mandatory review before publication."], "Required boolean; no boolean sensitivity field exists and generalisation must not be invented."),
}

# --------------------------------------------------------------------------
# Family table with corrections and fixtures
# --------------------------------------------------------------------------
FAMILIES = [
    {
        "file": "c34fa59ea7e7-media_extension.xml", "registry": "production", "decisions": EOL_MEDIA, "row_rules": EOL_MEDIA_ROW,
        "family_disposition": "conditional",
        "core_applicability": {"Occurrence": "conditional (media + occurrence-media)", "Event": "conditional (media + event-media)", "Taxon": "unsupported: preserve whole row, no orphan media"},
        "grain": "One extension row = one media item (or text item) about the core record's taxon page.",
        "family_review_triggers": [
            "dcterms:type is Text: route whole row to review (candidate bibliographic-resource or preserve); do not emit media.",
            "dwc:taxonID non-empty or Iptc4xmpExt:CVterm in SPMInfoItems: media may depict the taxon generally, not this occurrence/event.",
            "Same media identifier attached to several core records: one media row, several link rows (dedupe by mediaID only when every mapped field agrees)."],
        "catalogue_corrections": [
            "Prior target_tables ['media'] is incomplete: UsageTerms is required by the source and lands in usage-policy; bibliographicCitation/creator land in provenance; the link needs occurrence-media or event-media.",
            "'Could follow the Audiovisual path' is wrong for dcterms:format and dcterms:language: av-media-direct-fields copies them to formatIRI/languageIRI by IRI equality, but EOL values are MIME and ISO-code literals.",
            "audubon_core/subtype and Iptc4xmpExt:CVterm are not the IRIs of media.subtypeIRI (ac:subtype) or media.subjectCategoryIRI (ac:CVterm); they are review aliases.",
            "eol:referenceID cannot be linked: the pinned schema has no media-reference table.",
        ],
        "fixtures": {
            "occurrence_core": {
                "core": {"rowType": DWC + "Occurrence", "id": "occ-17", "occurrenceID": "urn:catalog:NHMO:V:17", "basisOfRecord": "HumanObservation"},
                "extension_rows": [{"coreid": "occ-17", "dcterms:identifier": "eol-media-9001", "dcterms:type": "http://purl.org/dc/dcmitype/StillImage", "ac:subtype": "Photograph",
                                    "dcterms:format": "image/jpeg", "ac:accessURI": "https://example.org/img/9001.jpg", "dcterms:language": "en", "xmpRights:UsageTerms": "http://creativecommons.org/licenses/by/4.0/",
                                    "xmpRights:Owner": "Jane Doe", "dcterms:creator": "Jane Doe", "geo:lat": "59.91", "geo:long": "10.75", "eol:referenceID": "ref-3"}],
                "expected_rows": {
                    "media": [{"media_pk": "<minted>", "mediaID": "eol-media-9001", "mediaType": "http://purl.org/dc/dcmitype/StillImage", "format": "image/jpeg", "accessURI": "https://example.org/img/9001.jpg", "language": "en", "usagePolicy_fk": "<up-1>", "provenance_fk": "<pv-1>"}],
                    "usage-policy": [{"usagePolicy_pk": "<up-1>", "usageTerms": "http://creativecommons.org/licenses/by/4.0/", "owner": "Jane Doe"}],
                    "provenance": [{"provenance_pk": "<pv-1>", "creator": "Jane Doe"}],
                    "occurrence-media": [{"media_fk": "<minted>", "occurrence_fk": "<occurrence_pk of occ-17>"}]},
                "preserved": ["geo:lat", "geo:long", "eol:referenceID"],
                "review": ["audubon_core/subtype = 'Photograph' (proposal subtypeLiteral)"],
            },
            "event_core": {
                "core": {"rowType": DWC + "Event", "id": "ev-2", "eventID": "plot-A-2019"},
                "extension_rows": [{"coreid": "ev-2", "dcterms:identifier": "eol-txt-55", "dcterms:type": "http://purl.org/dc/dcmitype/Text", "dcterms:description": "Grassland plot dominated by ...", "dwc:taxonID": "taxon-12",
                                    "xmpRights:UsageTerms": "http://creativecommons.org/publicdomain/zero/1.0/"}],
                "expected_rows": {},
                "preserved": ["all terms (row in review)"],
                "review": ["Text row: candidate bibliographic-resource or preserve", "dwc:taxonID present: subject is a taxon"],
            },
        },
    },
    {
        "file": "650d0e7712e0-reference_extension.xml", "registry": "production", "decisions": EOL_REF, "row_rules": EOL_REF_ROW,
        "family_disposition": "conditional",
        "core_applicability": {"Occurrence": "conditional (bibliographic-resource + occurrence-reference)", "Event": "conditional (bibliographic-resource + event-reference)", "Taxon": "unsupported"},
        "grain": "One extension row = one bibliographic resource cited by the core record (or, in EOL archives, by taxa/media).",
        "family_review_triggers": [
            "Row has no resolvable coreid but its identifier is used by EOL media eol:referenceID: no linkable subject; preserve.",
            "Same identifier attached to several core records: one bibliographic-resource, several link rows (dedupe only when every mapped field agrees)."],
        "catalogue_corrections": [
            "'Convertible when the referencing record is convertible' is incomplete: references reached via EOL media have no target join table, so even convertible media cannot carry them.",
            "dcterms:identifier -> referenceID, full_reference -> bibliographicCitation and publicationType -> referenceType are not IRI matches; only title, pages, volume and edition are.",
        ],
        "fixtures": {
            "occurrence_core": {
                "core": {"rowType": DWC + "Occurrence", "id": "occ-17", "occurrenceID": "urn:catalog:NHMO:V:17"},
                "extension_rows": [{"coreid": "occ-17", "dcterms:identifier": "ref-3", "eol:full_reference": "Doe J. (2012) Birds of Oslo. Oslo Univ. Press. 210 pp.", "dcterms:title": "Birds of Oslo",
                                    "bibo:volume": "2", "bibo:pageStart": "33", "bibo:pageEnd": "35", "bibo:doi": "10.1234/boo.2012"}],
                "expected_rows": {"bibliographic-resource": [{"reference_pk": "<minted>", "referenceID": "ref-3", "title": "Birds of Oslo", "volume": "2"}],
                                  "occurrence-reference": [{"reference_fk": "<minted>", "occurrence_fk": "<occurrence_pk of occ-17>", "relationshipType": ""}]},
                "preserved": ["bibo:doi"],
                "review": ["full_reference -> bibliographicCitation", "pageStart/pageEnd -> pages '33-35'"],
            },
            "event_core": {
                "core": {"rowType": DWC + "Event", "id": "ev-2", "eventID": "plot-A-2019"},
                "extension_rows": [{"coreid": "ev-2", "dcterms:identifier": "ref-9", "dcterms:title": "Vegetation survey report 2019", "dcterms:publisher": "County Environmental Office", "dcterms:created": "2019-12-01"}],
                "expected_rows": {"bibliographic-resource": [{"reference_pk": "<minted>", "referenceID": "ref-9", "title": "Vegetation survey report 2019", "publisher": "County Environmental Office"}],
                                  "event-reference": [{"reference_fk": "<minted>", "event_fk": "<event_pk of ev-2>", "relationshipType": ""}]},
                "preserved": ["dcterms:created"],
                "review": [],
            },
        },
    },
    {
        "file": "ae90e94b2293-GermplasmAccession.xml", "registry": "production", "decisions": GA, "row_rules": GA_ROW,
        "family_disposition": "conditional (germplasmID only); review/preserve for the rest",
        "core_applicability": {"Occurrence": "conditional when the core emitted a material row", "Event": "preserve all", "Taxon": "unsupported"},
        "grain": "One extension row = MCPD passport data of one genebank accession (a living material).",
        "family_review_triggers": ["More than one accession row per occurrence.", "basisOfRecord is not a specimen class (no material emitted)."],
        "catalogue_corrections": [
            "Prior criterion says MCPD terms 'overlapping material fields (institution, catalogue/accession number)': this XML has no institutionCode, catalogNumber or accession-number term. The only IRI-exact term is dwc:locationID, and it describes the collecting site, not the material.",
            "Target 'material' (as a table to fill) is wrong: no material row is created and no material field receives a value; only material-identifier gets germplasmID.",
            "storageCondition is in the germplasmType# namespace, not germplasmTerm#.",
        ],
        "fixtures": {
            "occurrence_core": {
                "core": {"rowType": DWC + "Occurrence", "id": "NGB1234", "occurrenceID": "urn:nordgen:NGB1234", "basisOfRecord": "LivingSpecimen", "institutionCode": "NGB", "catalogNumber": "NGB1234"},
                "extension_rows": [{"coreid": "NGB1234", "germplasmID": "https://doi.org/10.18730/ABC12", "germplasmIdentifier": "Emma", "biologicalStatus": "300",
                                    "lat": "60.39", "lon": "5.32", "breedingCountryCode": "SWE", "purdyPedigree": "Hanna/7*Atlas//Turk/8*Atlas", "mlsStatus": "Yes"}],
                "expected_rows": {"material-identifier": [{"materialEntity_fk": "<materialEntity_pk emitted for NGB1234>", "identifier": "https://doi.org/10.18730/ABC12"}]},
                "preserved": ["germplasmIdentifier", "biologicalStatus", "lat", "lon", "breedingCountryCode", "purdyPedigree", "mlsStatus"],
                "review": ["each preserved germplasm term: optional material-assertion proposal"],
            },
            "event_core": {
                "core": {"rowType": DWC + "Event", "id": "coll-1988-04", "eventID": "coll-1988-04"},
                "extension_rows": [{"coreid": "coll-1988-04", "germplasmID": "https://doi.org/10.18730/XYZ9", "donorInstitute": "Nordic Genetic Resources Center (NordGen)"}],
                "expected_rows": {}, "preserved": ["all terms"], "review": [],
            },
        },
    },
    {
        "file": "2dadad759a37-MeasurementScore.xml", "registry": "production", "decisions": MS, "row_rules": MS_ROW,
        "family_disposition": "conditional (material-assertion) with review fallback",
        "core_applicability": {"Occurrence": "conditional", "Event": "review", "Taxon": "unsupported"},
        "grain": "One extension row = one trait score for one accession in one trial.",
        "family_review_triggers": ["germplasmID does not match the material of the core occurrence.", "No material row for the occurrence."],
        "catalogue_corrections": [
            "The nine dwc:measurement* terms were not listed; they use the same field pairings as the MoF rules and are deterministic once the subject is fixed.",
            "measurementTrialID/Identifier (Trial spelled correctly) do not equal the Trial extension's measurementTrailID/Identifier IRIs; any join is by value, not by term.",
        ],
        "fixtures": {
            "occurrence_core": {
                "core": {"rowType": DWC + "Occurrence", "id": "NGB1234", "basisOfRecord": "LivingSpecimen"},
                "extension_rows": [{"coreid": "NGB1234", "germplasmID": "https://doi.org/10.18730/ABC12", "measurementType": "plant height", "measurementValue": "87", "measurementUnit": "cm",
                                    "measurementDeterminedDate": "2013-07-02", "measurementTraitIdentifier": "PLANTHT", "measurementTrialIdentifier": "T2013-ALNARP", "measurementGrowthStage": "heading"}],
                "prerequisite_state": "Accession row for NGB1234 produced material-identifier 'https://doi.org/10.18730/ABC12'.",
                "expected_rows": {"material-assertion": [{"materialEntity_fk": "<materialEntity_pk emitted for NGB1234>", "assertionType": "plant height", "assertionValue": "87", "assertionUnit": "cm", "assertionMadeDate": "2013-07-02"}]},
                "preserved": ["measurementTraitIdentifier", "measurementTrialIdentifier", "measurementGrowthStage"], "review": [],
            },
            "event_core": {
                "core": {"rowType": DWC + "Event", "id": "T2013-ALNARP"},
                "extension_rows": [{"coreid": "T2013-ALNARP", "germplasmID": "https://doi.org/10.18730/ABC12", "measurementType": "stem rust susceptibility", "measurementValue": "3"}],
                "expected_rows": {}, "preserved": ["all terms"], "review": ["subject is an accession, not the event"],
            },
        },
    },
    {
        "file": "e3c2cb4ff61a-MeasurementTrait.xml", "registry": "production", "decisions": MT, "row_rules": MT_ROW,
        "family_disposition": "review",
        "core_applicability": {"Occurrence": "review (protocol proposal)", "Event": "review (protocol proposal)", "Taxon": "unsupported"},
        "grain": "One extension row = one trait descriptor (method/scale definition) used by Score rows.",
        "family_review_triggers": ["Always."],
        "catalogue_corrections": ["Protocol is a reasonable proposal, but none of the 9 terms is an IRI match for a protocol field, and no core-record link table should be filled from coreid attachment."],
        "fixtures": {
            "occurrence_core": {
                "core": {"rowType": DWC + "Occurrence", "id": "NGB1234"},
                "extension_rows": [{"coreid": "NGB1234", "measurementTraitIdentifier": "PLANTHT", "measurementTraitName": "plant height", "measurementTraitScale": "ratio",
                                    "measurementTraitSource": "UPOV TG/19/10", "measurementMethod": "Measured from soil to tip of ear, excluding awns"}],
                "expected_rows": {}, "preserved": ["all terms until approved"],
                "review": ["protocol {protocolName: 'plant height', protocolDescription: 'Measured from soil ...', protocolReferences: 'UPOV TG/19/10'}"],
            },
            "event_core": {
                "core": {"rowType": DWC + "Event", "id": "T2013-ALNARP"},
                "extension_rows": [{"coreid": "T2013-ALNARP", "measurementTraitID": "http://purl.obolibrary.org/obo/TO_0000207", "measurementTraitName": "plant height"}],
                "expected_rows": {}, "preserved": ["all terms until approved"], "review": ["protocol proposal; no event-protocol link"],
            },
        },
    },
    {
        "file": "5014798f62ff-MeasurementTrial.xml", "registry": "production", "decisions": TR, "row_rules": TR_ROW,
        "family_disposition": "review",
        "core_applicability": {"Occurrence": "preserve all", "Event": "review", "Taxon": "unsupported"},
        "grain": "One extension row = one trial season at one location.",
        "family_review_triggers": ["Always (Event core)."],
        "catalogue_corrections": [
            "The registered term IRIs are measurementTrailID/Identifier/Year/Report/Remarks (sic); a rule keyed on measurementTrial* would miss every germplasm-namespace column.",
            "Only dwc:locationID is IRI-exact; the W3C geo terms are not dwc:decimalLatitude/Longitude.",
        ],
        "fixtures": {
            "occurrence_core": {
                "core": {"rowType": DWC + "Occurrence", "id": "NGB1234"},
                "extension_rows": [{"coreid": "NGB1234", "measurementTrailIdentifier": "T2013-ALNARP", "measurementTrailYear": "2013", "location": "Alnarp, Sweden", "lat": "55.66", "lon": "13.08"}],
                "expected_rows": {}, "preserved": ["all terms"], "review": [],
            },
            "event_core": {
                "core": {"rowType": DWC + "Event", "id": "T2013-ALNARP", "eventID": "T2013-ALNARP", "eventDate": "2013"},
                "extension_rows": [{"coreid": "T2013-ALNARP", "measurementTrailYear": "2013", "dwc:locationID": "http://sws.geonames.org/2725201/", "measurementTrailRemarks": "Dry June; randomised block design"}],
                "expected_rows": {}, "preserved": ["all terms until approved"],
                "review": ["event.locationID = http://sws.geonames.org/2725201/", "event.year = 2013", "event.eventRemarks"],
            },
        },
    },
    {
        "file": "7977dd7db803-bmde_2020_10_19.xml", "registry": "sandbox", "decisions": BM, "row_rules": BM_ROW,
        "family_disposition": "review (conditional only for dwc:individualCount and the 12 MoF groups)",
        "core_applicability": {"Occurrence": "mostly review", "Event": "Occurrence-group terms preserve", "Taxon": "unsupported"},
        "grain": "One extension row = BMDE attributes of one occurrence; Event-group terms repeat event-level values on every occurrence.",
        "family_review_triggers": ["Event-group values differ between occurrences of the same event.", "RecordPermissions < 5, LastModifiedAction = DELETE, NoObservations = NoObs."],
        "catalogue_corrections": ["Previously not term-audited. Only one of 195 terms (dwc:individualCount) has a dwc IRI; 'many duplicating DwC core terms' is true in meaning, not in IRIs."],
        "fixtures": {
            "occurrence_core": {
                "core": {"rowType": DWC + "Occurrence", "id": "BBS-0042-03-AMRO", "eventID": "BBS-0042-03", "scientificName": "Turdus migratorius"},
                "extension_rows": [{"coreid": "BBS-0042-03-AMRO", "individualCount": "3", "ObservationDescriptor": "TotalCount", "DistanceFromObserver": "40",
                                    "MeasurementType1": "wing chord", "MeasurementValue1": "128", "MeasurementUnit1": "mm", "RecordPermissions": "5"}],
                "expected_rows": {"occurrence": [{"organismQuantity": "3", "organismQuantityType": "individuals"}],
                                  "occurrence-assertion": [{"assertionType": "wing chord", "assertionValue": "128", "assertionUnit": "mm"}]},
                "preserved": [], "review": ["ObservationDescriptor 'TotalCount'", "DistanceFromObserver 40 m", "RecordPermissions"],
            },
            "event_core": {
                "core": {"rowType": DWC + "Event", "id": "BBS-0042-03"},
                "extension_rows": [{"coreid": "BBS-0042-03", "DurationInHours": "0.05", "NumberOfObservers": "1", "individualCount": "3"}],
                "expected_rows": {}, "preserved": ["individualCount (no occurrence subject)"], "review": ["DurationInHours", "NumberOfObservers"],
            },
        },
    },
    {
        "file": "f12e834ca1b0-NBNeXchangeFormat.xml", "registry": "sandbox", "decisions": NX, "row_rules": NX_ROW,
        "family_disposition": "review",
        "core_applicability": {"Occurrence": "review", "Event": "review", "Taxon": "unsupported"},
        "grain": "One extension row = UK vague date, grid reference and sensitivity of one record.",
        "family_review_triggers": ["sensitiveOccurrence = true."],
        "catalogue_corrections": ["Previously not term-audited. No term is IRI-exact; three terms are required by the source."],
        "fixtures": {
            "occurrence_core": {
                "core": {"rowType": DWC + "Occurrence", "id": "NBN-77", "eventDate": ""},
                "extension_rows": [{"coreid": "NBN-77", "eventDateTypeCode": "Y", "eventDateStart": "1950-01-01", "eventDateEnd": "1950-12-31", "gridReference": "SD4261", "gridReferenceType": "BNG", "sensitiveOccurrence": "true"}],
                "expected_rows": {}, "preserved": ["all terms until approved"], "review": ["eventDate 1950-01-01/1950-12-31", "verbatimCoordinates SD4261 (BNG)", "sensitive record"],
            },
            "event_core": {
                "core": {"rowType": DWC + "Event", "id": "visit-5"},
                "extension_rows": [{"coreid": "visit-5", "eventDateTypeCode": "D", "eventDateStart": "2015-06-03", "eventDateEnd": "2015-06-03", "sensitiveOccurrence": "false"}],
                "expected_rows": {}, "preserved": ["all terms until approved"], "review": ["eventDate 2015-06-03"],
            },
        },
    },
]


def build():
    out_families = []
    totals = {}
    for fam in FAMILIES:
        meta, props = read_source(fam["file"])
        decisions = fam["decisions"]
        source_iris = [p["source_iri"] for p in props]
        missing = [i for i in source_iris if i not in decisions]
        extra = [i for i in decisions if i not in source_iris]
        if missing or extra or len(set(source_iris)) != len(source_iris):
            raise SystemExit(f"{fam['file']}: missing={missing} extra={extra}")
        terms = []
        for p in props:
            d = decisions[p["source_iri"]]
            tgts = [target(t) for t in d["targets"]]
            for t in tgts:
                t["iri_match"] = t["target_isVersionOf"] == p["source_iri"]
            if d["disposition"] == MAP and not all(t["iri_match"] for t in tgts):
                raise SystemExit(f"'map' used without IRI match: {p['source_iri']}")
            if d["disposition"] in (MAP, REVIEW) and not tgts:
                raise SystemExit(f"{d['disposition']} without a target: {p['source_iri']}")
            terms.append({**p, **d, "targets": tgts})
            totals[d["disposition"]] = totals.get(d["disposition"], 0) + 1
        counts = {}
        for t in terms:
            counts[t["disposition"]] = counts.get(t["disposition"], 0) + 1
        out_families.append({
            "row_type": meta["row_type"], "title": meta["title"], "issued": meta["issued"], "registry": fam["registry"],
            "source_file": meta["source_file"], "term_count": len(terms), "disposition_counts": counts,
            "exact_iri_matches": sorted({t["source_iri"] for t in terms if any(x["iri_match"] for x in t["targets"])}),
            "family_disposition": fam["family_disposition"], "core_applicability": fam["core_applicability"], "grain": fam["grain"],
            "row_rules": fam["row_rules"], "family_review_triggers": fam["family_review_triggers"],
            "catalogue_corrections": fam["catalogue_corrections"], "fixtures": fam["fixtures"], "terms": terms,
        })
    return {
        "audit": "remaining-fields",
        "date": "2026-10-02",
        "schema_revision": SCHEMA_REVISION,
        "schema_path": "back-end/api/templates/dwc-dp/table-schemas",
        "schema_revision_note": "The snapshot does not carry the TDWG commit hash in the schema files; the revision is the one recorded in docs/dwca-conversion/README.md. The 79 table-schema files present were used as-is.",
        "registry_versions": "Each audited family has exactly one registered XML in sources/extension (all versions supplied were checked by rowType).",
        "dispositions": {
            MAP: "Deterministic copy; source IRI equals target dcterms:isVersionOf; only family row rules apply.",
            COND: "Deterministic with term-level conditions (value shape, uniqueness, subject check) or a documented non-IRI pairing already used by an existing catalogue rule. iri_match on each target says whether it is exact.",
            REVIEW: "Proposal needing human approval; the value is preserved until approved. Targets are candidates, not mappings.",
            PRESERVE: "No destination in the pinned schema without inventing records or facts; kept verbatim and reported (gen-preserve-unconsumed).",
        },
        "verification": {
            "generator": "docs/cloud-audits/tools/build_remaining_fields.py, run in python:3.12-slim via docker run",
            "checks": ["every XML property has exactly one decision", "map/review always name at least one target", "no decision for a term absent from the XML",
                       "every target table/field exists in the pinned schema", "'map' only where source IRI == target isVersionOf"],
            "not_run": "Backend test suite and conversion runtime: docker compose back-end needs back-end/.env.dev, which is absent; no runtime code was changed.",
        },
        "totals": totals,
        "families": out_families,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--schemas', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=OUT)
    args = parser.parse_args()
    SRC = args.registry / 'extension'
    SCHEMAS = args.schemas
    OUT = args.output
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT}")
