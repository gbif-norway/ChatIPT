# Remaining field audits: EOL, germplasm, BMDE, NBN

Date: 2 October 2026. Target: the pinned 79-table DwC-DP schema
(`76898192fd298c2aa170a7059e1bdadf3ee2a828`, `back-end/api/templates/dwc-dp/table-schemas`).
Machine-readable result: [`remaining-fields.json`](remaining-fields.json), built by
[`tools/build_remaining_fields.py`](tools/build_remaining_fields.py).

This pass covered all six production families term by term. It also covered the two
sandbox families (BMDE and NBN eXchange Format). No runtime code was changed.

## How to read the dispositions

| Disposition | Meaning |
| --- | --- |
| `map` | Deterministic copy. The source IRI equals the target field's `dcterms:isVersionOf`. Only the family row rules apply. |
| `conditional` | Deterministic, but with term-level conditions such as value shape (literal vs IRI), uniqueness or a subject check. It is also used for a non-IRI pairing that an existing catalogue rule already uses (for example the MoF `measurement*` → `assertion*` pairs, or `dcterms:creator` → `provenance.creator`). Each target carries `iri_match` true/false. |
| `review` | A named candidate target that needs human approval. The value is preserved until then. |
| `preserve` | No destination without inventing records or facts. The value is kept verbatim and reported under `gen-preserve-unconsumed`. |

Every term entry in the JSON records:

- the source IRI, `sources/extension/<file>:<line>`, the definition and the `required` flag;
- for each target: table, field, the target `isVersionOf`, whether it is required, and `table-schemas/<table>.json#<field>`.

The family-level row rules (core applicability, grain, keys, link rows) are listed once per family as `row_rules`. They are prerequisites for every term in that family.

## Totals

| Family (registry) | Terms | map | conditional | review | preserve | IRI-exact terms |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| EOL Media 1.0 (production) | 31 | 8 | 8 | 5 | 10 | 15 |
| EOL References 1.0 (production) | 18 | 4 | 2 | 6 | 6 | 4 |
| Germplasm Accession v20140515 (production) | 37 | 0 | 1 | 32 | 4 | 1 (`dwc:locationID`, review) |
| Trait Measurement Score v20140515 (production) | 18 | 0 | 10 | 3 | 5 | 0 |
| Trait Descriptor v20140515 (production) | 9 | 0 | 0 | 5 | 4 | 0 |
| Trait Measurement Trial v20140515 (production) | 10 | 0 | 0 | 10 | 0 | 1 (`dwc:locationID`, review) |
| Bird Monitoring Data Exchange 2020-10-19 (sandbox) | 195 | 0 | 73 | 103 | 19 | 0 |
| NBN eXchange Format 0.1 (sandbox) | 8 | 0 | 0 | 8 | 0 | 0 |

Each of these row types has exactly one registered XML in `sources/extension`. I checked every supplied file by `rowType`. There are no further versions to reconcile.

## Findings that change the existing catalogue

### EOL Media (`http://eol.org/schema/media/Document`)

**Target tables.** The catalogue lists only `media`, which is incomplete. A converted row writes to:

- `media`;
- `usage-policy` (`xmpRights:UsageTerms` is *required* by the source; with `dcterms:rights` and `xmpRights:Owner`);
- `provenance` (`dcterms:bibliographicCitation` and `dcterms:creator`);
- `occurrence-media` or `event-media`.

**The Audiovisual rule cannot be reused unchanged.** `av-media-direct-fields` copies `dcterms:format` → `media.formatIRI` and `dcterms:language` → `media.languageIRI` because the IRIs are equal. EOL's own definitions and examples are literals (`image/jpeg`, `en`). EOL therefore needs a value-shape rule: an absolute IRI goes to the `*IRI` field, anything else goes to `media.format` / `media.language` (the dc: literal versions). `dcterms:rights` follows the same pattern (`rightsIRI` vs `rights`).

**Not exact matches:**

- `http://rs.tdwg.org/audubon_core/subtype` is a legacy namespace. It is not `ac:subtype`.
- `Iptc4xmpExt:CVterm` is not `ac:CVterm`.

Both are review aliases.

**References cannot be linked.** There is no `media-reference` table in the 79 tables, so `eol:referenceID` cannot be linked and is preserved.

**Text rows.** When `dcterms:type` is Text, the row is a taxon text account. The whole row goes to review; no media row is emitted. The `media.json` comment itself points textual media to BibliographicResource.

**Taxon-linked content.** A non-empty `dwc:taxonID`, or an SPMInfoItems subject term, means the item is a taxon-page item. The occurrence/event link is placed by core attachment only. Nothing becomes an identification.

**Location terms.** `geo:lat/long/alt`, `LocationCreated` and `dcterms:spatial` are preserved. They are never written to the event.

### EOL References (`http://eol.org/schema/reference/Reference`)

**Exact matches.** Only `dcterms:title`, `bibo:pages`, `bibo:volume` and `bibo:edition` are IRI-exact.

**Conditional pairings.** These two pairings follow `gbif-reference-bibliographic`:

- `dcterms:identifier` → `referenceID`;
- `dcterms:publisher` → `publisher` / `publisherID`, split by value shape.

**Review aliases.** `full_reference`, `publicationType`, `authorList` and `editorList` are review aliases. EOL also says `full_reference` makes the structured fields be ignored, so a reviewer must decide how the two interact.

**Which references are convertible.** "Convertible when the referencing record is convertible" is too broad. Only rows attached by coreid to an Occurrence/Event core can be linked (via `occurrence-reference` / `event-reference`, with `relationshipType` left empty). References used only by EOL media have no join table.

**Preserved.** `dcterms:created`, `language`, `bibo:uri`, `bibo:doi`, `primaryTitle`, `pageStart`/`pageEnd` (with a review proposal to compose `pages`) and the place of publication. In particular, `created` is not `issued`, and the DOI and URI are never substituted for `referenceID`.

### Germplasm Accession (`http://purl.org/germplasm/germplasmTerm#GermplasmAccession`)

**The catalogue's premise is wrong.** It says MCPD terms "overlap material fields (institution, catalogue/accession number)", but this XML has no institution, catalogue-number or accession-number term. Of the 37 terms, only `dwc:locationID` is IRI-exact. Its definition is the *collecting site of the source material*, so it stays in review. It is never copied to the core event automatically.

**What is converted.** The only deterministic output is one `material-identifier` row holding `germplasmID`. That happens only when the core is Occurrence and the core already emitted exactly one material row for the occurrence (specimen-class `basisOfRecord`). No material row is ever created by this family.

**Everything else:**

- Breeding, pedigree, acquisition, donor, safety-duplicate, treaty and MLS terms, MCPD status and storage codes are review. The only proposal is a `material-assertion` keyed by the source term IRI.
- A pedigree must not become relationship rows, because its parents are not records.
- A safety duplicate must not become a second material.
- `acquisitionID` must not become an event.
- Coordinates are preserved.

**Namespace.** `storageCondition` is `http://purl.org/germplasm/germplasmType#storageCondition`, not `germplasmTerm#`.

### Trait Measurement Score (`…#MeasurementScore`)

**MoF fields.** The catalogue omitted the nine `dwc:measurement*` terms. They take the existing MoF field pairings (`mof-*` rules) once the subject is fixed.

**Subject rule.** The target is `material-assertion` only when both of these hold:
- the core occurrence has exactly one emitted material row;
- `germplasmID` is empty or equals that material's `materialEntityID` or one of its `material-identifier` values.

Otherwise the row goes to review, with `occurrence-assertion` as the candidate. On an Event core the whole row is review.

**Spelling mismatch.** `measurementTrialID` and `measurementTrialIdentifier` here are **not the same IRIs** as the Trial extension's `measurementTrailID` and `measurementTrailIdentifier` (sic). A join is by value only.

**Other review items.**
- `measurementTraitID` could be `assertionTypeIRI` or a protocol FK.
- `measurementTraitName` can fill `assertionType` only when `measurementType` is empty.
- `measurementByInstituteID` would need agent rows.

### Trait Descriptor (`…#MeasurementTrait`)

These rows are dataset-level method/scale definitions, so attaching them to a core record means nothing. The whole family is review:

- **Proposal.** One `protocol` row per distinct trait. Score rows reference it via `assertionProtocol_fk` only after approval.
- **No link tables.** No `occurrence-protocol` / `event-protocol` / `material-protocol` rows are filled from coreid.
- **No IRI matches.** None of the 9 terms is an IRI match.
- **`dwc:measurementType` is preserved.** Writing an assertion from a definition row would invent a measurement.

**Title discrepancy.** The XML title says v20140515 and `dc:issued` is 2014-05-08. The catalogue name says v20140508.

### Trait Measurement Trial (`…#MeasurementTrial`)

**Misspelled IRIs.** All five germplasm-namespace terms are registered as `measurementTrail*`. A converter keyed on `measurementTrial*` would silently miss every column.

**Occurrence core:** every term is preserved, because creating an event would invent a record.

**Event core:** the whole family is review. The proposal is that the trial describes the attached event and fills only empty fields:
- `locationID` (IRI-exact);
- `locality` (from `geo:location`);
- coordinates (from `geo:lat` / `geo:lon`, which are W3C geo, not dwc);
- `year`;
- `eventRemarks`;
- an `event-identifier`;
- the report as `bibliographic-resource` plus `event-reference`.

### BMDE (sandbox, `http://www.birdscanada.org/bmde/Observation`)

**IRIs.** There are 195 terms. Only `dwc:individualCount` has a dwc IRI, and it reuses the core rule `quantity.individualCount`. When `ObservationDescriptor` is "Presence/Absence", the value 1 means presence, so it goes to review.

**Measurement groups.** The 12 numbered measurement groups (72 terms) are conditional. Each populated group becomes one `occurrence-assertion` with the MoF pairings, on Occurrence cores only. They are documented equivalences of bmde: IRIs, not exact matches.

**Review items.**
- Effort pairs, survey-area geometry, time intervals, distances and sub-counts.
- Coordinate scope (it changes what the event coordinates mean).
- `RecordPermissions` below 5 and `LastModifiedAction = DELETE`. Both must be resolved before publication.

**Preserved.** `MultiScientificName1..6`, the taxonomic authority terms, record-level specimen coordinates, `RouteIdentifier` and `SamplingEventStructure`. No identifications, occurrences, parent events or surveys are created.

### NBN eXchange Format (sandbox)

All 8 terms are review; none is IRI-exact.

- **Vague dates.** `eventDateTypeCode`, `eventDateStart` and `eventDateEnd` are required by the source. The proposal is an ISO `start/end` `eventDate`, only where the core date is empty or consistent.
- **Grid references.** The proposal fills `verbatimCoordinates` / `verbatimCoordinateSystem`. Grid precision is not an uncertainty radius.
- **Sensitive records.** `sensitiveOccurrence = true` forces review before publication. No generalisation text is invented.

## Fixtures

Each family in the JSON has `fixtures.occurrence_core` and `fixtures.event_core`. Each fixture gives:

- a core row and extension rows;
- the expected emitted rows (`<minted>` marks deterministic keys);
- the preserved terms;
- the review items.

These are expected-behaviour specifications only. They were **not** executed against the converter.

## Uncertainties

- **Pinned revision.** The pinned commit hash is recorded only in `docs/dwca-conversion/README.md`; the schema files don't carry it. The 79 files in the snapshot were taken as that revision.
- **dc:/dcterms: literal/IRI split.** The splits used here (`creator`, `publisher`, `rights`, `format`, `language`) follow existing catalogue rules. They are not IRI equality.
- **Value-shape test.** The `conditional` value-shape test (absolute `http`/`https`/`urn` IRI) is the same as `emof-iri-columns`.
- **Germplasm/score subject matching** assumes the core emitted the accession as the occurrence's material. Real genebank archives may instead use a dataset-level occurrence per accession. That is still the same subject, but it should be confirmed on a real archive.
- **EOL archives in practice** are usually Taxon-core, which is unsupported. The Occurrence/Event paths here apply only to the rarer non-taxon archives.

## Verification

- **Docker.** I started the Docker daemon. `docker compose` services were not started, because `back-end/.env.dev` is absent.
- **Generator.** The generator ran in a plain `python:3.12-slim` container. It asserts that:
  - every XML property has exactly one decision;
  - no decision exists for a term absent from the XML;
  - every target table/field exists in the pinned schemas;
  - `map` is used only where the IRIs are equal;
  - `map`/`review` entries always name a target.
- **Not run:** the backend test suite and the conversion runtime. No runtime files were edited.

## Per-term tables (production families)

In the tables below, `=` marks an IRI-exact target. BMDE and NBN per-term detail is in the JSON only.

### EOL Media Extension 1.0

Source: `sources/extension/c34fa59ea7e7-media_extension.xml`

| Source term (line) | Disposition | Target(s) |
| --- | --- | --- |
| `dcterms:identifier` (required) (14) | conditional | =`media.mediaID` |
| `dwc:taxonID` (15) | preserve | — |
| `dcterms:type` (required) (16) | conditional | =`media.mediaType` |
| `http://rs.tdwg.org/audubon_core/subtype` (17) | review | `media.subtypeLiteral`, `media.subtypeIRI` |
| `dcterms:format` (18) | conditional | =`media.formatIRI`, `media.format` |
| `Iptc4xmpExt:CVterm` (19) | review | `media.subjectCategoryIRI` |
| `dcterms:title` (20) | map | =`media.title` |
| `dcterms:description` (21) | map | =`media.description` |
| `ac:accessURI` (22) | map | =`media.accessURI` |
| `eol:media/thumbnailURL` (23) | review | `media.accessURI`, `media.variantLiteral`, `media.derivedFromMediaID` |
| `ac:furtherInformationURL` (24) | map | =`media.furtherInformationURL` |
| `ac:derivedFrom` (25) | review | `media.derivedFromMediaID` |
| `xmp:CreateDate` (26) | map | =`media.createDate` |
| `dcterms:modified` (27) | map | =`media.modified` |
| `dcterms:language` (28) | conditional | =`media.languageIRI`, `media.language` |
| `xmp:Rating` (29) | map | =`media.rating` |
| `dcterms:audience` (30) | preserve | — |
| `xmpRights:UsageTerms` (required) (31) | conditional | =`usage-policy.usageTerms` |
| `dcterms:rights` (32) | conditional | =`usage-policy.rightsIRI`, `usage-policy.rights` |
| `xmpRights:Owner` (33) | map | =`usage-policy.owner` |
| `dcterms:bibliographicCitation` (34) | conditional | =`provenance.bibliographicCitation` |
| `dcterms:publisher` (35) | preserve | — |
| `dcterms:contributor` (36) | preserve | — |
| `dcterms:creator` (37) | conditional | `provenance.creator`, `provenance.creatorID` |
| `eol:agent/agentID` (38) | review | `media-agent-role.agent_fk` |
| `Iptc4xmpExt:LocationCreated` (39) | preserve | — |
| `dcterms:spatial` (40) | preserve | — |
| `geo:lat` (41) | preserve | — |
| `geo:long` (42) | preserve | — |
| `geo:alt` (43) | preserve | — |
| `eol:reference/referenceID` (44) | preserve | — |

### EOL References Extension 1.0

Source: `sources/extension/650d0e7712e0-reference_extension.xml`

| Source term (line) | Disposition | Target(s) |
| --- | --- | --- |
| `dcterms:identifier` (required) (14) | conditional | `bibliographic-resource.referenceID` |
| `eol:reference/publicationType` (15) | review | `bibliographic-resource.referenceType` |
| `eol:reference/full_reference` (16) | review | `bibliographic-resource.bibliographicCitation` |
| `eol:reference/primaryTitle` (17) | preserve | — |
| `dcterms:title` (18) | map | =`bibliographic-resource.title` |
| `bibo:pages` (19) | map | =`bibliographic-resource.pages` |
| `bibo:pageStart` (20) | review | `bibliographic-resource.pages` |
| `bibo:pageEnd` (21) | review | `bibliographic-resource.pages` |
| `bibo:volume` (22) | map | =`bibliographic-resource.volume` |
| `bibo:edition` (23) | map | =`bibliographic-resource.edition` |
| `dcterms:publisher` (24) | conditional | `bibliographic-resource.publisher`, `bibliographic-resource.publisherID` |
| `bibo:authorList` (25) | review | `bibliographic-resource.author` |
| `bibo:editorList` (26) | review | `bibliographic-resource.editor` |
| `dcterms:created` (27) | preserve | — |
| `dcterms:language` (28) | preserve | — |
| `bibo:uri` (29) | preserve | — |
| `bibo:doi` (30) | preserve | — |
| `http://schemas.talis.com/2005/address/schema#localityName` (31) | preserve | — |

### Germplasm accession (v20140515)

Source: `sources/extension/ae90e94b2293-GermplasmAccession.xml`

| Source term (line) | Disposition | Target(s) |
| --- | --- | --- |
| `g:germplasmID` (26) | conditional | `material-identifier.identifier` |
| `g:germplasmIdentifier` (35) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:biologicalStatus` (44) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `gType:storageCondition` (56) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:collectingInstituteID` (72) | preserve | — |
| `geo:lat` (81) | preserve | — |
| `geo:lon` (90) | preserve | — |
| `geo:alt` (99) | preserve | — |
| `dwc:locationID` (108) | review | =`event.locationID` |
| `g:breedingID` (119) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:breedingIdentifier` (128) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:breedingYear` (137) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:breedingCountry` (146) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:breedingCountryCode` (155) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:breedingInstituteID` (164) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:breedingInstitute` (173) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:breedingPerson` (182) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:ancestralData` (191) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:purdyPedigree` (200) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:breedingRemarks` (209) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:acquisitionID` (220) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:donorsID` (229) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:donorsIdentifier` (238) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:donorInstituteID` (247) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:donorInstitute` (256) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:acquisitionDate` (265) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:acquisitionSource` (274) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:acquisitionRemarks` (286) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:safetyDuplicationID` (297) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:safetyDuplicationDate` (306) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:safetyDuplicationInstituteID` (315) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:safetyDuplicationInstitute` (324) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:safetyDuplicationRemarks` (333) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:treatyOrRegulationID` (344) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:treatyOrRegulationName` (353) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:treatyOrRegulationGoverningBody` (362) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |
| `g:mlsStatus` (371) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionValue` |

### Trait measurement score (v20140515)

Source: `sources/extension/2dadad759a37-MeasurementScore.xml`

| Source term (line) | Disposition | Target(s) |
| --- | --- | --- |
| `dwc:measurementID` (26) | conditional | `material-assertion.assertionID` |
| `dwc:measurementValue` (35) | conditional | `material-assertion.assertionValue` |
| `dwc:measurementUnit` (44) | conditional | `material-assertion.assertionUnit` |
| `dwc:measurementAccuracy` (53) | conditional | `material-assertion.assertionError` |
| `dwc:measurementDeterminedDate` (62) | conditional | `material-assertion.assertionMadeDate` |
| `dwc:measurementDeterminedBy` (71) | conditional | `material-assertion.assertionBy` |
| `dwc:measurementType` (80) | conditional | `material-assertion.assertionType` |
| `dwc:measurementMethod` (88) | conditional | `material-assertion.assertionProtocols` |
| `dwc:measurementRemarks` (95) | conditional | `material-assertion.assertionRemarks` |
| `g:germplasmID` (108) | conditional | — |
| `g:germplasmIdentifier` (117) | preserve | — |
| `g:measurementTraitID` (128) | review | `material-assertion.assertionTypeIRI`, `material-assertion.assertionProtocol_fk` |
| `g:measurementTraitIdentifier` (137) | preserve | — |
| `g:measurementTraitName` (146) | review | `material-assertion.assertionType` |
| `g:measurementTrialID` (157) | preserve | — |
| `g:measurementTrialIdentifier` (166) | preserve | — |
| `g:measurementByInstituteID` (177) | review | `material-assertion.assertionByID` |
| `g:measurementGrowthStage` (186) | preserve | — |

### Trait descriptor (v20140515)

Source: `sources/extension/e3c2cb4ff61a-MeasurementTrait.xml`

| Source term (line) | Disposition | Target(s) |
| --- | --- | --- |
| `g:measurementTraitID` (27) | review | `protocol.protocolID` |
| `g:measurementTraitIdentifier` (36) | preserve | — |
| `g:measurementTraitName` (45) | review | `protocol.protocolName` |
| `g:measurementTraitCategory` (54) | preserve | — |
| `g:measurementTraitScale` (63) | preserve | — |
| `g:measurementTraitSource` (72) | review | `protocol.protocolReferences` |
| `g:measurementTraitRemarks` (81) | review | `protocol.protocolRemarks` |
| `dwc:measurementType` (91) | preserve | — |
| `dwc:measurementMethod` (100) | review | `protocol.protocolDescription` |

### Trait measurement trial (v20140515)

Source: `sources/extension/5014798f62ff-MeasurementTrial.xml`

| Source term (line) | Disposition | Target(s) |
| --- | --- | --- |
| `g:measurementTrailID` (28) | review | `event-identifier.identifier` |
| `g:measurementTrailIdentifier` (37) | review | `event.fieldNumber` |
| `g:measurementTrailYear` (46) | review | `event.year` |
| `g:measurementTrailReport` (55) | review | `bibliographic-resource.bibliographicCitation`, `event-reference.reference_fk` |
| `g:measurementTrailRemarks` (64) | review | `event.eventRemarks` |
| `dwc:locationID` (75) | review | =`event.locationID` |
| `geo:location` (84) | review | `event.locality` |
| `geo:lon` (93) | review | `event.decimalLongitude` |
| `geo:lat` (102) | review | `event.decimalLatitude` |
| `geo:alt` (111) | review | `event.minimumElevationInMeters`, `event.maximumElevationInMeters` |
