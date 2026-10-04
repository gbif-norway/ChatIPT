# Humboldt extension audit

Worker: Humboldt audit (`CLOUD_TASKS.md`). Machine-readable results:
[`humboldt.json`](humboldt.json). Fixtures: [`fixtures/humboldt/`](fixtures/humboldt/).

Target: the pinned DwC-DP `1.0_DEV` snapshot (79 table schemas) at
`76898192fd298c2aa170a7059e1bdadf3ee2a828`, `back-end/api/templates/dwc-dp`.
No runtime code was changed.

## Verification status: not run

I started the Docker daemon in this session, but `back-end/.env.dev` is missing.
`docker-compose.yml` names it as the back-end `env_file`, so the Compose services
could not run. As `CLOUD_TASKS.md` requires, I did not use host Python or Node
instead. No application code or tests were executed, and the fixtures were not
converted. Their expected outcomes are derived by hand from the rules below.

The source checks below used only xmllint, jq, grep, awk and sha256sum:

- Read every `qualName` from all four supplied Humboldt XMLs. Each has the same 57 IRIs.
- Compared SHA-256 hashes with `sources/registry-files.json`. All match, and
  `c6866ebee757` (sandbox 2025-07-10) is byte-identical to `f16a0de0c5bd`
  (production 2025-07-10).
- Diffed `group`, `name`, `thesaurus`, `required`, `type` and `dc:description`
  across versions. Only three definitions changed (see below).
- Compared each Humboldt IRI, by exact string, with `dcterms:isVersionOf` on every
  field of all 79 table schemas.
- Checked the fixture files: column counts per line with awk, `meta.xml`
  well-formedness with xmllint, and that every Humboldt IRI used in them is one of
  the audited 57.

## Sources and versions

| File | Registry | `dc:issued` | Notes |
| --- | --- | --- | --- |
| `0989d3de604f-Humboldt_2024-04-16.xml` | production, sandbox (not latest) | 2024-04-16 | `areNonTargetTaxaFullyReported` definition cites `eco:protocolDescription` (typo) |
| `f16a0de0c5bd-humboldt_2025-07-10.xml` | production (latest), sandbox | 2026-02-12 | reference version for definitions below |
| `c6866ebee757-humboldt_2025-07-10.xml` | sandbox | 2026-02-12 | byte-identical to `f16a0de0c5bd` |
| `ae99b9f7c33c-humboldt_2026-05-26.xml` | sandbox (latest) | 2026-07-21 | `dwc:identifiedBy` and `dwc:identificationReferences` definitions changed to match the pinned `survey` fields |

All versions have the same 57 IRIs and `required='false'` everywhere. No property
has a `type` attribute. The same 15 boolean terms cite
`https://rs.gbif.org/vocabulary/basic/boolean.xml`, which is not supplied.
Filename dates are not issue dates, so version detection must use the term set.
The version differences do not change any mapping.

## Results at a glance

- **43 of 57 terms have an exact-IRI field on `survey`.** Each target's
  `dcterms:isVersionOf` equals the source `qualName`, which confirms the
  catalogue's `hum-survey-direct-fields` pairs.
- **The other 14 (scope terms) have no exact-IRI field in any of the 79 tables.**
  They can only be restated structurally as `survey-target` +
  `survey-target-descriptor` + `survey-survey-target`.
- **`dwc:identifiedBy` and `dwc:identificationReferences` also match fields on
  `identification`, `occurrence` and `material`. `eco:protocolReferences` also
  matches `protocol`.** None of these is a target, because the Humboldt subject is
  the event/survey and no protocol row is emitted.
- **Dispositions: 22 automatic, 30 conditional, 5 review or review-first.**
  - The 22 automatic terms are verbatim string copies.
  - The 30 conditional terms are:
    - 17 typed `survey` fields (2 integer, 4 number, 11 boolean) that need a lexical check;
    - 4 string fields gated by a contradiction check;
    - 9 single-dimension scope terms.
  - The 5 review terms are:
    - the 2 Habitat scope terms (there is no fully-reported flag for habitat);
    - the 3 Degree-of-establishment terms (the target-type label is not attested in the pinned schema).
- **Every source value is preserved.** Values that are unconsumed or withheld for
  review are kept and reported.

## Per-term disposition

The full prerequisites, review triggers, evidence and catalogue assessment for
each term are in `humboldt.json` → `terms[]`.

| Source IRI | Target (type) | Disposition |
| --- | --- | --- |
| `eco:siteCount` | `survey.siteCount` (integer, min 1) | conditional |
| `eco:siteNestingDescription` | `survey.siteNestingDescription` (string) | automatic |
| `eco:verbatimSiteDescriptions` | `survey.verbatimSiteDescriptions` (string) | automatic |
| `eco:verbatimSiteNames` | `survey.verbatimSiteNames` (string) | automatic |
| `eco:geospatialScopeAreaValue` | `survey.geospatialScopeAreaValue` (number, min 0) | conditional |
| `eco:geospatialScopeAreaUnit` | `survey.geospatialScopeAreaUnit` (string) | automatic |
| `eco:totalAreaSampledValue` | `survey.totalAreaSampledValue` (number, min 0) | conditional |
| `eco:totalAreaSampledUnit` | `survey.totalAreaSampledUnit` (string) | automatic |
| `eco:reportedWeather` | `survey.reportedWeather` (string) | automatic |
| `eco:reportedExtremeConditions` | `survey.reportedExtremeConditions` (string) | automatic |
| `eco:targetHabitatScope` | `survey-target-descriptor.surveyTargetValue` (habitat, include) | review |
| `eco:excludedHabitatScope` | `survey-target-descriptor.surveyTargetValue` (habitat, exclude) | review |
| `eco:eventDurationValue` | `survey.eventDurationValue` (number, min 0) | conditional |
| `eco:eventDurationUnit` | `survey.eventDurationUnit` (string) | automatic |
| `eco:targetTaxonomicScope` | `survey-target-descriptor.surveyTargetValue` (taxon, include) | conditional |
| `eco:excludedTaxonomicScope` | `survey-target-descriptor.surveyTargetValue` (taxon, exclude) | conditional |
| `eco:taxonCompletenessReported` | `survey.taxonCompletenessReported` (string) | automatic |
| `eco:taxonCompletenessProtocols` | `survey.taxonCompletenessProtocols` (string) | automatic |
| `eco:isTaxonomicScopeFullyReported` | `survey-target.isSurveyTargetFullyReported` (Taxonomic) | conditional |
| `eco:isAbsenceReported` | `survey.isAbsenceReported` (boolean) | conditional |
| `eco:absentTaxa` | `survey.absentTaxa` (string) | conditional |
| `eco:hasNonTargetTaxa` | `survey.hasNonTargetTaxa` (boolean) | conditional |
| `eco:nonTargetTaxa` | `survey.nonTargetTaxa` (string) | conditional |
| `eco:areNonTargetTaxaFullyReported` | `survey.areNonTargetTaxaFullyReported` (boolean) | conditional |
| `eco:targetLifeStageScope` | `survey-target-descriptor.surveyTargetValue` (lifeStage, include) | conditional |
| `eco:excludedLifeStageScope` | `survey-target-descriptor.surveyTargetValue` (lifeStage, exclude) | conditional |
| `eco:isLifeStageScopeFullyReported` | `survey-target.isSurveyTargetFullyReported` (LifeStage) | conditional |
| `eco:targetDegreeOfEstablishmentScope` | `survey-target-descriptor.surveyTargetValue` (degreeOfEstablishment, include) | review (policy first) |
| `eco:excludedDegreeOfEstablishmentScope` | `survey-target-descriptor.surveyTargetValue` (degreeOfEstablishment, exclude) | review (policy first) |
| `eco:isDegreeOfEstablishmentScopeFullyReported` | `survey-target.isSurveyTargetFullyReported` (DegreeOfEstablishment) | review (policy first) |
| `eco:targetGrowthFormScope` | `survey-target-descriptor.surveyTargetValue` (growthForm, include) | conditional |
| `eco:excludedGrowthFormScope` | `survey-target-descriptor.surveyTargetValue` (growthForm, exclude) | conditional |
| `eco:isGrowthFormScopeFullyReported` | `survey-target.isSurveyTargetFullyReported` (GrowthForm) | conditional |
| `eco:hasNonTargetOrganisms` | `survey.hasNonTargetOrganisms` (boolean) | conditional |
| `eco:verbatimTargetScope` | `survey.verbatimTargetScope` (string) | automatic |
| `dwc:identifiedBy` | `survey.identifiedBy` (string) | automatic |
| `dwc:identificationReferences` | `survey.identificationReferences` (string) | automatic |
| `eco:compilationTypes` | `survey.compilationTypes` (string) | automatic |
| `eco:compilationSourceTypes` | `survey.compilationSourceTypes` (string) | automatic |
| `eco:inventoryTypes` | `survey.inventoryTypes` (string) | automatic |
| `eco:protocolNames` | `survey.protocolNames` (string) | automatic |
| `eco:protocolDescriptions` | `survey.protocolDescriptions` (string) | automatic |
| `eco:protocolReferences` | `survey.protocolReferences` (string) | automatic |
| `eco:isAbundanceReported` | `survey.isAbundanceReported` (boolean) | conditional |
| `eco:isAbundanceCapReported` | `survey.isAbundanceCapReported` (boolean) | conditional |
| `eco:abundanceCap` | `survey.abundanceCap` (integer, min 0) | conditional |
| `eco:isVegetationCoverReported` | `survey.isVegetationCoverReported` (boolean) | conditional |
| `eco:isLeastSpecificTargetCategoryQuantityInclusive` | `survey.isLeastSpecificTargetCategoryQuantityInclusive` (boolean) | conditional |
| `eco:hasVouchers` | `survey.hasVouchers` (boolean) | conditional |
| `eco:voucherInstitutions` | `survey.voucherInstitutions` (string) | conditional |
| `eco:hasMaterialSamples` | `survey.hasMaterialSamples` (boolean) | conditional |
| `eco:materialSampleTypes` | `survey.materialSampleTypes` (string) | conditional |
| `eco:samplingPerformedBy` | `survey.samplingPerformedBy` (string) | automatic |
| `eco:isSamplingEffortReported` | `survey.isSamplingEffortReported` (boolean) | conditional |
| `eco:samplingEffortProtocol` | `survey.samplingEffortProtocol` (string) | automatic |
| `eco:samplingEffortValue` | `survey.samplingEffortValue` (number, min 0) | conditional |
| `eco:samplingEffortUnit` | `survey.samplingEffortUnit` (string) | automatic |

## Field-type checks (the XML declares no types)

These checks gate the conditional `survey` fields. A value that fails is withheld
from that field, sent to review and kept in the preserved originals. Nothing is
rounded, stripped or coerced.

- **Integer** (`siteCount` min 1, `abundanceCap` min 0). The value must match
  `^[+-]?[0-9]+$` and meet the minimum. ChatIPT's validator requires the same
  (`back-end/api/dwc_dp_specs.py:441-442`), so `3.0`, `~10` and `1,200` all fail.
- **Number** (`geospatialScopeAreaValue`, `totalAreaSampledValue`,
  `eventDurationValue`, `samplingEffortValue`; min 0). The value must be a plain
  decimal or exponent number and finite, and the paired unit must be non-empty.
  The XML says each of these values "must have a corresponding" unit.
- **Boolean** (11 `survey` fields and the 4 scope flags). Accepted values are
  `true`/`True`/`TRUE`/`false`/`False`/`FALSE`, copied verbatim. The local
  validator lower-cases and accepts only `true`/`false` (`dwc_dp_specs.py:450-456`).
  - Values such as `yes`, `no`, `1`, `0`, `y` and `n` go to review with a
    proposed reading. They are never normalised automatically, because the GBIF
    boolean vocabulary is not supplied.

## Row placement

**Event core (`hum-row-event-core`, conditional).** The archive's core must be
`dwc:Event`, and the Humboldt row's coreid must resolve to exactly one event. Each
such row then gets one `survey`:

- `survey_pk` = derived key `['survey-of', event_pk]`.
- `survey.event_fk` = `event_pk` (required).
- `surveyID` stays empty. Humboldt has no survey identifier, and eventID is not copied into it.
- The subject basis is recorded as `declared_row_type`.
- Byte-identical duplicate rows collapse to one, and the duplicates are counted in the report.
- Non-identical rows for the same event go to review.
- Orphaned rows are preserved and reported.
- An event whose supplied `eventCategory` is not `survey` is a conflict and goes to review.
- If `eventCategory` is empty, the core `event-category` issue applies. `survey`
  may be the recommended option there, but it is never written automatically.

**Occurrence core (`hum-row-occurrence-core`, always review).** The extension
declares an Event subject (`dc:subject='dwc:Event'`), but here it is attached to
occurrences. Attachment alone does not identify the surveyed event.

- A proposal is made only when every occurrence sharing a non-empty `eventID`
  carries a byte-identical Humboldt row. The proposal is then one survey on the
  grouped event.
- If the rows conflict, only some occurrences in the group have a row, or the
  occurrence has no `eventID` (a synthesised event), no automatic proposal is
  made. The rows are preserved, and partial coverage is shown with a warning.

**Nested events.** Each event that has its own Humboldt row gets its own survey.
Nothing is inherited or aggregated between parent and child surveys. The pinned
`survey` table has no parent-survey key, so the hierarchy is visible only through
`event.parentEvent_fk`.

**No derived records.** No Humboldt term creates any of the following:

- occurrences (present or absent);
- identifications, materials or agents;
- protocols, `survey-protocol`, bibliographic resources or `survey-reference`;
- `survey-agent-role`, `survey-identifier` or `survey-assertion` rows;
- `*_fk` / `*ByID` values.

`occurrence.surveyTarget_fk` is never inferred.

## Scope dimensions to survey targets

| Dimension | Target / excluded terms | Flag term | `surveyTargetType` | Attested in pinned examples |
| --- | --- | --- | --- | --- |
| Taxonomic | `targetTaxonomicScope` / `excludedTaxonomicScope` | `isTaxonomicScopeFullyReported` | `taxon` | yes |
| Habitat | `targetHabitatScope` / `excludedHabitatScope` | **none** | `habitat` | yes |
| LifeStage | `targetLifeStageScope` / `excludedLifeStageScope` | `isLifeStageScopeFullyReported` | `lifeStage` | yes |
| DegreeOfEstablishment | `target…` / `excluded…DegreeOfEstablishmentScope` | `isDegreeOfEstablishmentScopeFullyReported` | `degreeOfEstablishment` | **no** (examples list `establishmentMeans`, a different DwC term; not an alias) |
| GrowthForm | `targetGrowthFormScope` / `excludedGrowthFormScope` | `isGrowthFormScopeFullyReported` | `growthForm` | yes |

**`hum-scope-single-dimension` (conditional).** All of the following must hold:

- Exactly one dimension is populated, and it has at least one `target*` element.
- Its flag is present and is a valid boolean. `survey-target.isSurveyTargetFullyReported`
  is **required**, so Habitat can never pass.
- The type label is attested.
- Splitting on the exact separator ` | ` round-trips byte-for-byte, with no empty,
  padded or duplicate elements and no element in both the include and exclude lists.

The rule then emits:

- one `survey-target` with key `['survey-target-of', survey_pk, dimension]` and
  the flag copied verbatim;
- one `survey-survey-target` link;
- one `survey-target-descriptor` per element (`include` for `target*`, `exclude`
  for `excluded*`). The IRI, Source and Unit columns stay empty.

**`hum-scope-multi-dimension` (review).** Two or more dimensions are populated.
The reviewer chooses between:

- one combined target, supplying the single flag as a logged reviewer assertion; or
- one target per dimension, each needing its own valid flag.

A true taxonomic flag may seem to imply a fully reported "adult Lepidoptera"
target, but Humboldt does not say the flags were assessed independently, so this
is not applied automatically.

**`hum-scope-flag-only` (preserve).** A flag whose dimension has no values has no
pinned field. It is preserved and reported.

## Cross-field checks

| ID | Trigger | Action |
| --- | --- | --- |
| X1 | `isAbsenceReported` false and `absentTaxa` non-empty | withhold both, review |
| X2 | `hasNonTargetTaxa` false and `nonTargetTaxa` non-empty | withhold both, review |
| X3 | `isAbundanceCapReported` false and `abundanceCap` non-empty | withhold both, review |
| X4 | `hasVouchers` false and `voucherInstitutions` non-empty | withhold both, review |
| X5 | `hasMaterialSamples` false and `materialSampleTypes` non-empty | withhold both, review |
| X6 | `isSamplingEffortReported` false and `samplingEffortValue` non-empty | withhold both, review |
| X7 | any `*Value` populated with empty unit | withhold value, review |
| X8 | `totalAreaSampledValue` > `geospatialScopeAreaValue` with byte-identical units | withhold both, review (different units: no conversion, report only) |
| X9 | non-target terms populated with no taxonomic scope declared | report only |

## Where the existing catalogue is wrong or incomplete

These findings concern `docs/dwca-conversion/extension-catalogue.json`
(`hum-*` rules) and `extension-notes.md` § Humboldt.

1. **F1 (error): an empty flag fails a required field.** `hum-scope-single-dimension`
   says "for Habitat (no such term) leave empty" for `isSurveyTargetFullyReported`.
   That field is `required: true` in `survey-target.json`, so the package would be
   invalid. The rule also does not handle an empty or invalid flag in the other
   dimensions. Such targets must go to review, and the flag must never be
   defaulted.
2. **F2 (error): typed fields are copied without checks.** All 43 direct fields
   are marked `automatic`, but 17 of them are typed (integer, number or boolean)
   and the XML allows any string. These must be conditional, with the lexical
   checks above.
3. **F3 (incomplete): value/unit pairing is not enforced** (check X7).
4. **F4 (incomplete): Occurrence-core Humboldt is not covered.** The rows would
   fall silently into generic preservation. Add `hum-row-occurrence-core`.
5. **F5 (incomplete): repeated rows per event are not handled.** "One survey per
   Humboldt row" duplicates surveys for repeated identical rows and silently
   accepts conflicting ones.
6. **F6 (inconsistency): surveys are automatic in one document and opt-in in the
   other.** `core-notes.md` says "A survey is created only when the user opts in",
   while `hum-survey-row` creates surveys automatically. Neither document covers
   the required `event.eventCategory` for events with a Humboldt row.
7. **F7 (unverified value): `degreeOfEstablishment` is not an attested
   `surveyTargetType`.** It needs a recorded policy; `establishmentMeans` must not
   be used as an alias.
8. **F8 (incomplete): the scope split has no safeguards.** There are no checks
   for exclusion-only scopes, duplicate elements, target/exclude overlap, padding
   or the round trip.
9. **F9 (incomplete): flag-only dimensions have no disposition.**
10. **F10 (minor): incomplete source term list.** `hum-scope-multi-dimension`
    lists only the five `target*` terms; it should list all 14 scope terms.
11. **F11 (minor): the version notes miss some differences.** "No structural
    change" is right for IRIs, but the notes omit the 2026 definition changes,
    the byte-identical sandbox copy and the filename versus `dc:issued` mismatch.
12. **F12 (minor): `survey.surveyID` and the survey/target key components are
    unspecified.**
13. **F13 (incomplete): there are no cross-field contradiction checks.**
14. **F14 (confirmed): the following are correct:**
    - the 43 IRI pairs;
    - the 14 scope terms having no exact-IRI field;
    - never inferring `occurrence.surveyTarget_fk`;
    - not minting `*_fk` from text;
    - nested hierarchy only via `event.parentEvent_fk`.

## Fixtures

The fixtures are tab-separated archives with `meta.xml`, written with the
production 2025-07-10 term set. The expected outcomes are listed in
`humboldt.json` → `fixtures`. They have not been executed.

**`fixtures/humboldt/event-core/`** (Event core with Occurrence and Humboldt extensions):

- `BBS-2025-R12` (parent) and `BBS-2025-R12-P01` (child): each gets one survey
  and one taxon target.
  - R12 has descriptors Aves include and *Columba livia* exclude; P01 has Aves include.
  - The P01 occurrences keep `surveyTarget_fk` empty, including the excluded *Columba livia*.
  - `BBS-2025-R12-P02` has no Humboldt row, so it gets no survey.
- `MOTH-2025-L03`: the Taxonomic and LifeStage dimensions are both populated, with
  flags `true` and `false`. The survey fields are copied and the scope goes to review.
- `VEG-2025-Q17`: a habitat-only scope (`scrub | grassland`). The survey fields
  are copied and the target goes to review. X8 is not evaluated, because the units
  are m² and km².
- `POND-2025-07`: a survey is emitted, but the following go to review:
  - `siteCount` `3.0`;
  - `isAbundanceReported` `yes`;
  - `samplingEffortValue` `40` with no unit;
  - `isAbundanceCapReported` `false` with `abundanceCap` `300`;
  - the taxonomic scope, which has no flag.

  `absentTaxa` stays on `survey`, and no absent occurrence is created.
- `POND-2025-08`: two conflicting Humboldt rows, so the event goes to review and
  no survey is emitted.
- `BBS-2025-R99`: the coreid is orphaned. The row is preserved and reported.
- Totals before any review decision:
  - 5 `survey`, 2 `survey-target`, 3 `survey-target-descriptor` and 2
    `survey-survey-target` rows;
  - 8 review items and 1 orphan.

**`fixtures/humboldt/occurrence-core/`** (Occurrence core with a Humboldt extension keyed by `occurrenceID`):

- `PLOT-7`: both occurrences have identical rows, so a single-survey proposal goes to review.
- `PLOT-8`: the rows conflict on `samplingEffortValue` (20 vs 30). No proposal is made.
- `PLOT-9`: only one of two occurrences has a row. A proposal is shown with a
  coverage warning.
- `INC-2025-017`: there is no `eventID`, so the event is synthesised. No proposal
  is made.
- `PLOT-10`: no Humboldt row.
- Totals: no automatic surveys; all 6 Humboldt rows are preserved.

## Open questions and uncertainties

- **Union semantics within one type.** The pinned schema does not say that two
  `include` descriptors of the same type (e.g. Aves, Mammalia) form a union. This
  audit assumes they do, which matches Humboldt's list semantics. It keeps one
  target per dimension so that the per-dimension flag covers the whole list.
- **The GBIF boolean vocabulary is not supplied.** Synonym normalisation therefore
  stays a review action.
- **Overlap with core-catalogue routing.** `survey` also has exact-IRI fields for
  the Event-core terms `dwc:samplingProtocol`, `dwc:sampleSizeValue` and
  `dwc:sampleSizeUnit`, which the core catalogue sends elsewhere. When a survey
  exists, the root agent must decide where these go. This audit copies none of them.
- **Occurrence-core Humboldt.** Whether it should be offered for review at all,
  or only reported, is a product decision.
