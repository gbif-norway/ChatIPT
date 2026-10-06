# Core-term mapping notes: Occurrence and Event cores

These notes accompany `core-catalogue.json` (catalogue version 1). The only target is
ChatIPT's vendored TDWG DwC-DP `1.0_DEV` profile and table schemas at
`76898192fd298c2aa170a7059e1bdadf3ee2a828` (`back-end/api/templates/dwc-dp`).
None of the rules relies on GBIF's newer working schemas. Where those schemas
differ in a way that matters (for example, `recordedBy` moves to `event` and an
`event-usage-policy` table is added), the rule says so and keeps the 1.0_DEV
placement.

Sources were used only for definitions:

- the vendored table schemas and profile;
- `tdwg-dwc-standard/vocabulary/term_versions.csv`;
- the `docs/text`, `docs/cm` and `docs/dp` guides;
- the GBIF registry snapshots under `audit/2026-10-02/extension`: Occurrence
  core 2026-05-26, Event core 2026-05-26 and Taxon core 2026-05-26.

## How to read the catalogue

- **Matching.** A rule applies to a source column only when the column's
  `meta.xml` term IRI exactly equals an IRI the rule lists. Columns that no rule
  names are kept unmapped (`archive.term-match.exact-iri`). Only three aliases
  exist, and each is its own rule:
  - `https://rs.tdwg.org/dwc/terms/` is read as `http://rs.tdwg.org/dwc/terms/`;
  - `materialSampleID` maps to `materialEntityID`;
  - case and spacing variants of `basisOfRecord` and `occurrenceStatus` values
    are accepted.
- **Grouped rules.** Rules with parallel `source_terms` and `targets` lists map
  item N in one list to item N in the other. Other rules list every field they
  can write.
- **Dispositions.**
  - `automatic`: a deterministic copy.
  - `conditional`: automatic only when the listed checks pass. If a check fails,
    the rule says what happens instead, usually review.
  - `review`: needs a person, unless one of the discharge conditions in
    `review.discharge-policy` holds.
  - `unsupported`: the data is preserved but not mapped.
- **Why target fields are named explicitly.** Do not derive targets from a
  field's `dcterms:isVersionOf`:
  - Several fields share one value. `occurrence_pk`, `occurrenceID`,
    `isPartOfOccurrence_fk` and `material.evidenceForOccurrenceID` all point to
    `dwc:occurrenceID`.
  - Some values are malformed. `recordedByID`, `eventConductedByID` and
    `collectedByID` point to `.../terms/version/recordedByID`.
  - A field description can deliberately differ from the source term. For
    example, `scientificName` in DwC-DP excludes authorship.

## Identifiers: join keys vs. identifiers

| Concept | Source | Target | Rule |
| --- | --- | --- | --- |
| Archive join key | core `<id>` column / extension `<coreid>` | `occurrence_pk` or `event_pk` (verbatim if complete and unique, else derived) | `ids.core-id-to-pk` |
| Occurrence identifier | `dwc:occurrenceID` | `occurrence.occurrenceID` (never generated) | `ids.occurrenceID` |
| Event identifier | `dwc:eventID` | `event.eventID` (never generated) | `ids.eventID` |
| Everything else | derived | `*_pk` via SHA-256 over canonical JSON | `ids.derived-key-scheme` |

The core id can differ from `occurrenceID` or `eventID`. Extension rows join only
through the core id, so it decides the primary key. `*_pk` values MAY change in
aggregation. `occurrenceID` and `eventID` MUST be preserved, so derived keys are
never written into them.

Two references point to identifiers rather than primary keys, so empty
identifiers break them:

- `parentEventID` refers to an `eventID`. It is resolved to the parent's
  `event_pk` before it is written to `parentEvent_fk`.
- `material.evidenceForOccurrenceID` refers to `occurrence.occurrenceID`, not
  to `occurrence_pk`.

## Entity grain

- **Occurrence core.** Each row produces one `occurrence`, because
  `occurrence.event_fk` is required.
  - Rows that share an `eventID` share one event, and that event's field values
    must agree.
  - Rows without an `eventID` get one synthesised context event each.
  - Rows are never merged because they have the same date and place.
- **Event core.** Each row produces one `event`. Occurrences come from the
  Occurrence extension. When an extension row has event-level values that differ
  from its event, the row goes to review; the user can accept a child event.
- **Identification.** Each occurrence with name or determination terms gets one
  identification. The same values are also written to the occurrence's
  identification fields. Higher ranks go only to `identification`.
- **Material.** A material row is created only when `basisOfRecord` names a
  specimen class (PreservedSpecimen, FossilSpecimen, LivingSpecimen,
  MaterialSample or MaterialEntity). Shared physical objects go to review.
- **Organism.** An organism row is created only from a non-empty `organismID`.
- **Protocol.** `samplingProtocol` produces `protocol` rows plus `event-protocol`
  links.
- **Assertions.** Sample size and sampling effort produce `event-assertion` rows.
- **Survey.** A survey is created only when the user opts in.

## basisOfRecord (no DwC-DP field)

| Value | Outcome |
| --- | --- |
| PreservedSpecimen, FossilSpecimen, LivingSpecimen, MaterialSample, MaterialEntity | `material` row as evidence for the occurrence; material terms are copied |
| HumanObservation, MachineObservation, Occurrence | occurrence only |
| MaterialCitation, Event, legacy values, missing | review |
| Taxon | checklist routing |

`materialEntityCategory` is not derived automatically. The original value is kept
in the conversion log.

## Taxon core outcomes

These are the original catalogue proposals. The [implemented Taxon-core path](taxon-conversion.md)
retains a complete checklist as additional taxonomy resources, uses a generic
Data Package for a standalone checklist, and supports reviewed extraction of
declared Occurrence extensions. Types/specimen and Distribution extensions remain
taxonomy resources; they are not promoted to occurrences.

| Situation | Disposition |
| --- | --- |
| Taxon core alone, or only taxon-centred extensions (Distribution, Vernacular, etc.) | **unsupported**: there is no taxon table in 1.0_DEV. Keep the DwC-A and never synthesise occurrences. |
| Taxon core with an Occurrence extension (or dated, located Types and Specimen rows) | **review**: the user confirms the reshape, or the case becomes unsupported |
| Occurrence core shaped like a checklist (`basisOfRecord = Taxon`, or no date, place or recorder in any row) | **review** |

## Decisions that always need user input

- Merge conflicts in grouped events, organisms, provenance and identifications.
- Non-ISO or ambiguous dates without a usable `dateFormat`.
- Coordinate, depth or uncertainty values outside the schema constraints.
- Unknown `basisOfRecord` or `occurrenceStatus` values, and missing status. The
  exception is held specimens with a non-zero count, which are discharged
  automatically.
- `dynamicProperties` keys: which entity each key describes and whether to
  create an assertion.
- Material terms on observation rows.
- Local (non-resolvable) `taxonID` values.
- Unresolved parent events.
- Upgrading an event to Survey or MaterialGathering.
- Licences that vary per record or differ from the EML.
- `associatedMedia`, `associatedOccurrences` and `previousIdentifications`.
- Pairing lists of agent names with lists of agent IDs, except `|` lists of equal length whose IDs are all single agent IRIs.

Model output may explain options, but it never discharges a review.

## Gap list

1. **G1: `eventCategory` vocabulary.** The vendored field references
   `eventCategory-2026-05-26`, which recommends `Event`, `MaterialGathering`,
   `Occurrence`, `OrganismInteraction` and `Survey`. The schema's examples use
   lowercase `occurrence`, `material collection`, `survey` and `context`, and
   current ChatIPT DP code and tests use `occurrence`. A maintainer needs to pick
   one set before implementation. The field is required.
2. **G2: `occurrenceStatus` vocabulary.** DwC-DP recommends `detected` and
   `notDetected`. ChatIPT's DwC-A path validates `present` and `absent`. The
   conversion keeps source values; canonicalisation is a separate decision.
3. **G3: `basisOfRecord` is lossy.** There is no field for it, so HumanObservation
   and MachineObservation become indistinguishable, and so do the specimen
   classes. The DwC-DP → DwC-A projection should read the conversion log so a
   round trip does not change published values.
4. **G4: missing fields.** `identificationQualifier`, `individualCount` (handled
   only when the count maps losslessly), `taxonomicStatus`, `acceptedNameUsage*`,
   `higherClassification`, `verbatimTaxonRank`, record-level `dc:type`,
   `language`, and `modified` on occurrence and event have no 1.0_DEV fields.
5. **G5: no event or occurrence licence target.** Per-record licences on
   occurrence-only data cannot be represented without GBIF's newer
   `event-usage-policy`, which this target excludes.
6. **G6: provenance is not linkable from occurrence.** Record-level citations on
   observation rows need a user decision.
7. **G7: `materialEntityCategory` vocabulary.** The values in
   `term_versions.csv` (`liveOrganism`, `fossil`, …) conflict with the schema
   examples (`preserved`, `living`, `fossilized`).
8. **G8: geological context without an ID.** It cannot be linked from an event
   unless a `geologicalContextID` is generated, which would be a new identifier.
9. **G9: one physical object as evidence for several occurrences.**
   `evidenceForOccurrenceID` holds only one value.
10. **G10: no extension rules here.** The Occurrence-extension and
    Identification History merge points are defined here, but the column rules
    belong to the extension catalogue.

## Critical implementation constraints

- Read `meta.xml` exactly. Apply `<field default>` values, concatenate multi-file
  cores, and never trim key columns. Duplicate, empty or near-duplicate core ids
  block conversion when extensions exist.
- Derived keys must be deterministic. Their inputs exclude time, random values
  and `catalogue_version`. Keys are checked for uniqueness per table after
  generation.
- Never write a generated value into an identifier field that DwC-DP requires to
  be preserved (`occurrenceID`, `eventID`, `identificationID`, `materialEntityID`,
  `organismID`, `geologicalContextID`).
- Hard foreign keys must resolve to a primary key in the package; unresolved
  values go to the log, never into dangling foreign keys. Weak keys reference
  `*ID` fields, not `*_pk`.
- Copy values verbatim unless a rule names a specific transformation. Do not
  normalise vocabularies, convert time zones, derive date parts, default the
  datum, or match names against a backbone.
- Unmapped data is preserved and reported, never dropped. Each target cell must
  be traceable to a rule id and a source column in the conversion log, and the
  log is not added as custom table fields.
- The `datapackage.json` descriptor must copy field descriptors from the vendored
  schemas, without re-deriving them.
