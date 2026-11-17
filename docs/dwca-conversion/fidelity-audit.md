# DwC-A to DwC-DP fidelity audit

The conversion report now carries `value_disposition` and `semantic_value_audit`.
`value_disposition.source_terms` accounts for every nonempty source term as
mapped, derived, withheld, retained only in originals, or unverified. Its counts
are source cells, not distinct target rows: several occurrences may share one
event. `emitted_but_flagged_values` annotates mapped values and overlaps the
other counts. Join keys without cell-level evidence remain **unverified**. The
`untraced_or_generated_output_fields` list identifies target fields without an
explicit field-level source trace; generated keys and links are expected there.

`semantic_value_audit` gives bounded examples for invalid `countryCode`, the
exact `dateIdentified` placeholder `0-0-0`, and zeros in elevation/depth.
Each distinct non-code country label on an Occurrence core has a review choice:
keep it in originals, or copy the exact text to `event.country` or
`event.waterBody`. Age-like `eventRemarks` on occurrence rows with unambiguous
event subjects can similarly remain on the event, move unchanged to
`occurrence.lifeStage` or `occurrence.occurrenceRemarks`, or remain only in
originals. The report records these decisions in `reviewed_value_routes`.
Unreviewed invalid country codes and `0-0-0` identification dates are withheld
from their semantic target fields and logged individually in `withheld_values`.
All source bytes stay in `source-originals.zip`. A zero elevation or depth can be genuine, so it is
reported without automatic suppression. Source `individualCount` becomes the
paired occurrence `organismQuantity`/`organismQuantityType=individuals` only
when it is a nonnegative integer and neither quantity field was supplied. This
holds for Occurrence extensions of Event cores as well as Occurrence cores; an
extension row kept in originals is counted as retained. Beside a different
supplied quantity the count becomes an `individualCount` occurrence-assertion;
`derived_routes` reports how many rows took each route.
Zero counts never determine occurrence status.
eMoF vocabulary identifiers (`measurementTypeID`, `measurementValueID`,
`measurementUnitID`) reach the assertion `*IRI` fields. Missing-value tokens
such as `NA` are empty cells, counted once per column as `empty_placeholder`;
other non-IRI text is withheld and logged in `withheld_values`. A supplied
`identificationQualifier` follows the name text in a generated
`verbatimIdentification` and is reported as a derived verbatim copy; qualifiers
that cannot be placed are counted in `retained_reasons`.
The count transformation reports total mapped rows, its deterministic rule,
and up to 20 exact source/target examples; the existing row crosswalk and
original archive carry the full row provenance without duplicating every cell
in the report.

A source Occurrence table that declares every row `PreservedSpecimen` and has a
complete, unique `(institutionCode, collectionCode, catalogNumber)` triple for
every row now creates one material entity per row. The catalog, preparation,
institution, and collection fields can move onto that material record. The
generated material primary key is an internal key; the converter does not
invent a persistent `materialEntityID`. Duplicated or incomplete catalog
identities still require review.

The converter also emits `agent` and the relevant `*-agent-role` rows from
mapped `*By` and `*ByID` fields. A single explicit agent IRI reuses its Agent
record; the ID alone decides identity, so two different IDs that share a name
remain two Agents. A name without an identifier links, by default, to one
Agent per exact name within the dataset, with one role row per mention
(production archives otherwise produced 4,209 Agent rows for one collector in
572 and 1,229 for one company in 563). Name agents carry no `agentID` and a
remark saying they stand for an exact name only. A name-only mention is never
merged into an explicit-ID Agent, even when the names are equal: it gets the
separate name Agent; `agent_roles.name_agents_matching_id_agents` counts such
names. This is an automatic choice shown as a notice with a link to the
setting: `agent-names` keeps every name without an identifier as text only, and
each repeated name has its own `agent-share:` choice to keep that name as text
only. Per-name choices are inactive while `agent-names` keeps all names as text.
Names are counted from every column that can map to an agent role, so a
remapped column's names have the same choice. During review the notice gives an
upper bound ("Links up to N mentions of M names…"), since keeping a column in
the originals or a name as text lowers it. The conversion report restates the
notice from what was linked ("Linked N mentions of M names…; K mentions were
kept as text only by your choices") and drops it when nothing was linked by
name. Placeholder names never become
Agents: the converter's empty-cell tokens and values such as `unknown`,
`[Ukjent]`, `Unknown collector`, `Samler ukjent`, `Ikke angitt`, `s. n.`,
`N.N`, `Indet.` or `<NA>` (a placeholder word with only role nouns, brackets,
quotes or dotted initials around it). Names containing `?` (`Rosaag?`,
`Ø.O.(Ørjan Olsen?)`) doubt the identity they give and stay text, reported as
`uncertain`. A missing-value token such as `NA` in a `*ByID` field counts as an
empty cell, so the name is still linked by name.
Names are compared after collapsing whitespace runs, in the converter's
explicit-ID agents and in role rows alike. A `|`-delimited `*ByID` list of
distinct single agent IRIs links one Agent and one role row per IRI when its
name field is empty or lists the same number of single names. `agentRoleOrder`
is the source ID order. Darwin Core states that list order conveys no meaning,
so names are never paired with IDs by position: an ID's `preferredAgentName`
comes only from mentions where that ID stands alone, and is empty otherwise.
Lists with empty segments, repeated IDs, values that are not absolute IRIs (such
as bare ORCID numbers) or a different number of names, and other composite
names and placeholders, remain in the mapped text fields and originals, with
skip reasons and examples in `agent_roles`. The converter does not infer whether
a name denotes a person or organization.

A foreign key is declared in `datapackage.json` only when both its source
fields and its target fields are present. An empty `recordedByID` column beside
an Agent table that holds only confirmed name-only agents (no `agentID` field)
therefore declares no key to `agent.agentID`.

Source EML metadata is selected through `meta.xml`'s declared metadata path,
with a single `eml.xml` as fallback. The Data Package descriptor can promote
contributors, keywords, and an unambiguous recognized license URL. Citation,
coverage, and free-text rights are described in the conversion report and the
original EML remains in `source-originals.zip`. A source EML document is placed
at the package root as `eml.xml` only when it passes the package's EML 2.2.0
validation. No EML version conversion or license inference is performed.

## Four production examples

| Dataset | Main fidelity check after reconversion |
| --- | --- |
| 556 | Shared event grouping still requires agreeing source event details; occurrence counts should gain a typed quantity pair. |
| 557 | Per-row event context and counts remain; each non-code `countryCode` label gets a reviewed route to country, water body, or originals. |
| 558 | Occurrence assertions retain their linked subjects and measurement units; collectors and identifiers gain one Agent per exact name, with a role link per mention. Source dates absent from the archive remain absent. |
| 559 | Unique preserved specimen catalog identities create material records; counts gain a typed quantity pair. `0-0-0` identification dates are withheld; zero elevation/depth is flagged, age-like event remarks get reviewed routes, and agents gain roles. |

## Schema status

Packages are pinned to the final DwC-DP 1.0, vendored from `tdwg/rs.tdwg.org`
`dwc-dp/1.0` at commit `88a4b91f191849cd6dfb1aad78ed0510f1a47823` ("Publish
Darwin Core 2026-10-13 schema artifacts"). The vendored profile and all 81 table
schemas are byte-identical to `tdwg/dwc` `dwc-dp/` and to the files deployed at
<https://rs.tdwg.org/dwc-dp/1.0/> (checked 2026-11-04). Descriptors reference
`https://rs.tdwg.org/dwc-dp/1.0/dwc-dp-profile.json` and record the revision and
content hash in `dwcDpSchema`. The local `index.json` and `version.json` are
generated from the table metadata (version `1.0`, issued 2026-10-13); TDWG does
not publish them.

GBIF's DwC-DP ingestion (`gbif/pipelines` dev, last DwC-DP change 2026-10-01)
still reads the 1.0_DEV names: `occurrence.recordedBy`,
`event.eventConductedBy`, and `material.evidenceForOccurrenceID`. Until it is
updated, GBIF may not pick up recorders or specimen links from a 1.0 package.
ChatIPT's own provisional GBIF check reads the 1.0 names.

### Changes from 1.0_DEV to 1.0

This compares the previous snapshot (rs.tdwg.org `dwc` branch commit
`76898192fd298c2aa170a7059e1bdadf3ee2a828`) with 1.0, field by field.

**Profile.** The profile now declares JSON Schema 2020-12 (it was draft-04), with
`$id` `https://rs.tdwg.org/dwc-dp/1.0/dwc-dp-profile.json` and
`version` `http://rs.tdwg.org/dwc-dp/1.0`. The resource-name list adds
`event-usage-policy` and `geological-material`. Field `dcterms:isVersionOf` must
match `^https?://.+$` (it was `^http.*$`). `dcterms:references` is no longer
constrained. The base Data Package `$ref` still resolves offline.

**Metadata on every table.** Table identifiers move from
`http://rs.tdwg.org/dwc/dwc-dp/<table>` to `http://rs.tdwg.org/dwc-dp/1.0/<table>`,
and every table gains `dcterms:isPartOf` `http://www.tdwg.org/standards/450`.
Tables and fields rename `comments` to `notes`. `rdfs:comment` and
`dcterms:references` are dropped everywhere. The 1.0_DEV placeholder
references (`"pending"`), which ChatIPT used to strip, are gone.

**New tables.**

| Table | Fields (required) | Links |
| --- | --- | --- |
| `event-usage-policy` | `event_fk` (required), `usagePolicy_fk` | event, usage-policy |
| `geological-material` | `materialEntity_fk` (required and unique: at most one row per material), `mineralogicalAnalysisProtocol_fk` (required), `materialEntityID`, `geologicalMaterialNames`, `geologicMaterialNameID`, `geologicalMaterialNameTypes`, `classificationCodes`, `mineralSequence`, `measuredChemistry`, `measuredChemistrySource`, `mineralogicalAnalysisProtocol`, `chemistryRemarks` | material, protocol |

No table was removed.

**Changes to `required`.** One field changed:
`nucleotide-analysis.nucleotideSequence_fk` is no longer required
(tdwg/dwc#934). Every added field is optional, except in the new tables above.

**Renamed, moved and removed fields.**

| Table | Removed | Added or replacing |
| --- | --- | --- |
| `event` | `eventConductedBy`, `eventConductedByID` | `recordedBy`, `recordedByID`, `recordedBy_fk` (the same terms, renamed) |
| `occurrence` | `recordedBy`, `recordedByID` | none: recorders belong to the event |
| `occurrence` | `externalClassificationSource` | `classificationSystem` |
| `identification`, `identification-taxon` | `geoName`, `geoClassificationCode`, `externalClassificationSource` | `geologicalMaterialNames`, `geologicalClassificationCodes`, `classificationSystem` |
| `material` | `evidenceForOccurrenceID` | `evidenceForOccurrence_fk`, a foreign key to `occurrence.occurrence_pk` (not a weak key to `occurrenceID`) |
| `material` | `geoName`, `geoClassificationCode`, `externalClassificationSource` | `classificationSystem` (the geological names move to `geological-material`) |
| `molecular-protocol` | `DNA_sequence` (`gbif:dna_sequence`) | `sequence` (`dwc:sequence`) |
| `survey` | `eventDurationValue`, `eventDurationUnit`, `isVegetationCoverReported`, `reportedWeather`, `reportedExtremeConditions` | the same fields on `event` |
| `survey` | `abundanceCap` | `survey-target.abundanceCap` |
| `resource-relationship`, `organism-relationship` | `relationshipOrder` | none |

**Added fields.**

- `nucleotide-analysis`: `rawTotalReadCount`.
- `event`: `namedPlace`, `geologicalContext_fk`, `georeferencedBy_fk`, besides the moved fields above.
- `occurrence`: `recordNumber`, `substrate`, `organism_fk`, `identifiedBy_fk`.
- `identification`: `identifiedBy_fk`, besides the geological and classification fields above.
- `material`: `derivedFromMaterialEntity_fk`, `isPartOfMaterialEntity_fk`, `derivationEventID`, `materialRole`, `materialProportion`, `sampledFeatureType`, `collectedBy_fk`, `identifiedBy_fk`, `measuredMassInGrams`, `verbatimMass`, `treatments`, `handlingRequirements`, `hazardType`, `hazardRemarks`, `damageRemarks`, `materialDescription`, `owner`, `owner_fk`, `license`.
- `media`: `derivedFromMedia_fk`, `isPartOfMedia_fk`, `commenter_fk`, `reviewer_fk`, `owner`, `owner_fk`, `license`.
- `molecular-protocol`: `source_mat_fk`.
- `geological-context`: `geologicEvent`, `geologicProvince`, `lithodemicUnit`, `tectonicUnits`.
- `chronometric-age`: `materialDated_fk`, `chronometricAgeDeterminedBy_fk`.
- `bibliographic-resource`: `isPartOfReference_fk`, `bibliographicIdentifier`, `bibliographicIdentifierType`, `author_fk`, `editor_fk`, `publisher_fk`.
- `provenance`: `datasetName`, `fundingAttribution_fk`, `creator_fk`, `provider_fk`, `metadataCreator_fk`, `metadataProvider_fk`.
- `usage-policy`: `rightsHolder_fk`, `owner_fk`.
- `survey`: `surveySiteType`, `identifiedBy_fk`, `samplingPerformedBy_fk`.
- `organism-interaction`: `relatedOccurrence_fk`. `organism-relationship`: `relatedOrganism_fk`, `relationshipAccordingTo_fk`.
- Every `*-assertion` table: `assertionBy_fk`.

Each new `*_fk` comes with a matching (strong) foreign key, beside the existing
weak `*ID` key where there is one (for example `occurrence.organism_fk` beside
`organismID`, and `identification.identifiedBy_fk` to `agent.agent_pk`). Weak
keys change only with the renamed fields: `event.recordedByID` → `agent.agentID`
replaces `eventConductedByID`; the weak keys of `occurrence.recordedByID` and
`material.evidenceForOccurrenceID` go with those fields; and
`material.derivationEventID` → `event.eventID` is new. The `event_fk` predicate of `occurrence`, `survey` and
`organism-interaction` now reads "has context" (it was "happened during"), and
`identification.materialEntity_fk` reads "of a".

**Changed term IRIs (`dcterms:isVersionOf`).**

- `*-agent-role.agentRoleDate`: `dwc:relationshipEstablishedDate` → `dwc:assertionEffectiveDate`.
- `*-media.subjectCategoryIRI` and `media.subjectCategoryIRI`: `ac:CVterm` → `Iptc4xmpExt:CVterm`.
- `organism-interaction.subjectOrganismPart`, `relatedOrganismPart`: `ac:subjectPart` → `dwc:organismPart`.
- `*.provenance_fk` → `dwc:provenanceID`; `*.usagePolicy_fk` and `usage-policy.usagePolicyID` → `dwc:usagePolicyID` (all were `dcterms:identifier`).
- `material.isPartOfMaterialEntityID`: `dwc:materialEntityID` → `dwc:isPartOfMaterialEntityID`.

No field changed its `type`, `format` or other constraints, apart from the one
`required` change above.

### What the converter does with them (rule version 31)

- **Recorders.** `recordedBy` and `recordedByID` from an Occurrence core go to
  `event.recordedBy` and `event.recordedByID`, with `event-agent-role` rows.
  Specimen rows still default to `material.collectedBy`. When occurrences
  share an event, recorders who disagree make the combined event conflict,
  like any other event detail. Each row can then get its own event instead.
  Occurrence extensions of an Event core take recorders through the existing
  "event details on occurrence rows" choice.
- **Specimen links.** `material.evidenceForOccurrence_fk` holds the occurrence's
  key, set when the material is evidence for exactly one occurrence. A
  missing or repeated `occurrenceID` no longer stops collectors defaulting to
  the specimen record. That still happens only when each specimen record
  stands for one row.
- **Record numbers.** `recordNumber` defaults to `occurrence.recordNumber`, and
  to `material.collectorNumber` when specimen records are created.
  `material.derivationEventID` shares the `dwc:eventID` IRI, but is never offered
  for a row's own `eventID`.
- **Humboldt.** The event-level Humboldt properties are written onto the
  survey's event. `abundanceCap` goes to the survey target. A survey target is
  created only from a reviewed scope with a completeness flag, so otherwise the
  cap is withheld and reported.
- **DNA.** A DNA Derived Data row without a sequence becomes a
  `nucleotide-analysis` and `molecular-protocol` with no sequence link. No
  stand-in sequence is created (see [extension notes](extension-notes.md)).
  `rawTotalReadCount` stays unmapped: no DwC-A term or source column supplies it.
- **Media rights.** `xmpRights:Owner` and `dcterms:license` match both `media` and
  `usage-policy` in 1.0. They stay on the usage policy with the other rights.
- **Dataset name on provenance.** A media column holding `dwc:datasetName` now maps to the
  new `provenance.datasetName`, which is the only field of that IRI among the media targets. On
  Occurrence and Event cores, `datasetName` still goes to `event.datasetName`.
- The other added fields have no DwC-A source in the registry, or are keys that
  the converter does not fill. A source column whose only target was a removed
  field, such as `relationshipOrder` or `externalClassificationSource`, now stays
  only in the originals.
