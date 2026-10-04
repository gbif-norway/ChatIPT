# Humboldt coverage — 3 October 2026

The converter covers the registered 57-field Humboldt DwC-A extension, but this
is narrower than the current Humboldt vocabulary and its hierarchical survey
model. Full vocabulary coverage and semantic equivalence are not established.

> **Update (rule version 6):** the converter now resolves `dwc:parentEventID` to
> `event.parentEvent_fk` against persistent `dwc:eventID` values. It also imports
> `eco:surveyID` and audits the seven newer literal and 28 `ecoiri:` properties.
> See the [implementation note](humboldt-implementation.md),
> [vocabulary audit](humboldt-vocabulary-audit.json), and
> [independent review and real archive benchmark](humboldt-review.md).

## Sources

- [TDWG quick reference](https://eco.tdwg.org/terms/): definitions, usage notes and
  examples, including Survey and SurveyTarget terms.
- [TDWG vocabulary list](https://eco.tdwg.org/list/): current version dated
  2026-05-26; term IRIs and definitions are normative.
- [TDWG hc repository](https://github.com/tdwg/hc): vocabulary history,
  documentation and implementation material. The local reference checkout is
  `/Users/rukayasj/Projects/humboldt-core`, on `main` at
  `05ad2bb6e960b1028a314feaf686444b7b8373c7`. Fetching origin confirmed no commits
  ahead or behind on 3 October. The untracked local user guide was left untouched.
- [Archive extension audit](cloud-audits/humboldt.md) and
  [machine-readable catalogue](cloud-audits/humboldt.json): all 57 fields of four
  registered XML versions. The original cloud worker's unexecuted fixture
  expectations remain distinct from the subsequently implemented local tests;
  see [coordinator review](cloud-audits/coordinator-review.md).

## Implemented scope

The 57 registered fields consist of 55 `eco:` properties and two reused Darwin
Core properties (`identifiedBy` and `identificationReferences`). All appear in
the audit. The reused Darwin Core properties are not obsolete merely because
they are absent from the Humboldt namespace's own vocabulary file.

Of these fields, 43 have direct survey targets. Values are copied when type,
bound, unit and implemented consistency checks pass. The other 14 describe
scopes, reconstructed into survey targets and descriptors. Missing completeness
flags, habitat, combined dimensions and degree of establishment require explicit
review. Original values and withheld values remain available in source files and
the conversion report. Matching a field IRI does not itself prove the source
row's subject or completeness assertion.

The original twelve Humboldt application tests pass unchanged. They cover direct
copies, invalid values, unit/flag contradictions, scope reconstruction, review
requirements, duplicate/conflicting survey rows, Occurrence-core event grouping,
and serialization/validation of a survey package. Thirty hierarchy/vocabulary
tests from Claude and one coordinator regression test also pass. The complete
focused matrix passes 225 tests in Docker. A real nested Humboldt Event-core
archive is now verified separately; live-model recommendations remain unevaluated.

## Gaps found against the current vocabulary

Reading the latest recommended versions in `vocabulary/term_versions.csv`
finds 62 literal `eco:` properties and 28 `ecoiri:` properties. The 57-field XML
audit is not a coverage claim for all these properties.

Seven literal properties are outside the registered XML audit:

- `surveyID`
- `surveyTargetID`
- `surveyTargetType`
- `surveyTargetValue`
- `surveyTargetUnit`
- `includeOrExclude`
- `isSurveyTargetFullyReported`

The converter imports `eco:surveyID` on confirmed survey extension rows, checking
that one identifier does not describe separate emitted surveys. The six other
literal properties require a SurveyTarget source table model and remain in
originals. Ten IRI scope properties and `ecoiri:samplingPerformedBy` have reviewed
import paths; seventeen other IRI properties remain in originals with explicit
reasons. Literal/IRI scopes in the same original row are never paired or silently
reinterpreted by preserving a column. The checked-in supplemental catalogue covers
all 35 properties and matches the local TDWG vocabulary and executable audit.

Nested events now resolve supplied `dwc:parentEventID` against persistent source
`dwc:eventID`, never against unrelated archive row keys. Forward references work;
missing or ambiguous parents, cycles, self-links and conflicting grouped parent
values withhold the whole hierarchy column. Occurrence cores require explicit
grouping by eventID before linking parents. The user can preserve the column.

The real dry-grassland archive benchmark verifies 390 events, 389 parent links,
15,669 occurrence-to-event links and 390 survey-to-event links, exact survey cell
copies, reproducibility, original-file preservation and serialized validity.
Review answers are explicit benchmark simulations, not publisher approval.

String copying also does not implement every controlled-vocabulary or
parent/child consistency condition described in TDWG's documentation. These
checks need explicit policies rather than inferred completeness or invented
parent records.

## Next verification work

1. Add spatial/temporal containment, controlled-vocabulary and hierarchy consistency review criteria where
   they can be evaluated deterministically, keeping publisher assertions explicit.
2. Design a SurveyTarget source table model and resolve how literal and IRI
   descriptions should be paired before importing the preserved target properties.
3. Evaluate more real publisher scope/completeness statements with domain reviewers.
   Successful linkage and structural validation alone do not establish scientific
   equivalence or justify inherited completeness.
