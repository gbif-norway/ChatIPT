# Conversion choices and notices

4 October 2026. Mapping rule version **8**. This policy governs the implemented
converter and supersedes blanket review requirements in the original design
[review policy](review-policy.md).

Conversion asks a question when it needs an interpretation or a new assertion.
The AI reviewer may answer interpretations of the supplier's own text, such as
a country name in `countryCode` or a life stage in `eventRemarks`; only the
user may answer new assertions ([review policy §6.1](review-policy.md#61-who-may-decide)).
Faithful copies, deterministic supported mappings and retention of unsupported
data proceed automatically. Notices explain incomplete or questionable source
claims without asking users to certify scientific validity.

## Automatic behavior

- Exact registered IRIs map to the field on the declared subject when available.
  For example, occurrence `identifiedBy`, identification remarks, quantity/type
  and organism identifiers retain their supplied values. No organism or accepted
  identification is inferred. Event context fields remain attached to their row;
  combining repeated event IDs still requires a decision and consistency checks.
- Declared Occurrence extensions on Event cores, Identification extensions on
  Occurrence cores, generic ResourceRelationship, Identifier and Reference
  extensions, and MeasurementOrFact assertions use their declared core subject.
  Supplied assertion occurrence IDs must resolve uniquely within their attached
  event. Audited relationship/measurement renames and reference identifier aliases
  proceed automatically. Generic reference creator/date aliases still need review.
- Event-core Humboldt rows create separate surveys. Already supplied survey
  categories require no confirmation. A missing category is filled only by the
  explicit event-category choice; the Humboldt step merely requires that category.
  Survey merging remains an optional explicit choice.
  A supplied non-survey category makes the attached Humboldt table unavailable
  for conversion; it is retained automatically until the source is corrected.
- Unique, fully supplied occurrence event IDs establish one context event per
  source row and permit supplied parent links, without merging. Repeated IDs
  require an event-grain decision. Missing IDs use separate context events;
  impossible grouped-survey and parent-link mappings stay unavailable.
  When missing IDs occur alongside repeated IDs, splitting the repeated identity
  remains an explicit context decision; users can retain eventID in originals.
- Preserve-only columns, rows and tables are retained automatically, with reasons.
  Questions about the meaning of an unconvertible table are suppressed.
- Direct numeric/boolean copies omit incompatible cells from mapped tables and
  record their original values, source rows and reasons. They never repair them.
  A valid half-coordinate remains supplied data; notices distinguish an omitted
  opposite coordinate from an invalid value withheld by conversion. No point is
  constructed from it.
- Suspicious `eventDate` text remains unchanged, with a notice that its meaning
  is unverified. Null-like tokens and float-shaped years are never interpreted
  as dates or silently recoded as empty. The converter's limited comparison
  parser is not a comprehensive ISO 8601 validator.
- Scientific parent/child findings remain visible before and after conversion,
  including original assertions retained without mapping. They are nonblocking;
  retaining a publisher's links does not certify those assertions. Structurally
  unresolved or cyclic links remain unavailable.
- A strictly valid `ecoiri:samplingPerformedBy` goes to the agent identifier
  field, without requiring a second confirmation or creating an agent record.
- A nonnegative integer `individualCount` becomes `organismQuantity` with
  `organismQuantityType=individuals` on every converted occurrence, from an
  Occurrence core or an Occurrence extension, when the row supplies no quantity
  of its own. Beside a different supplied quantity (a density in `ind/m3`, say),
  which keeps the quantity pair, the count becomes an `occurrence-assertion`
  with `assertionType` `individualCount` and `assertionUnit` `individuals`.
- eMoF `measurementTypeID`, `measurementValueID` and `measurementUnitID` go to
  `assertionTypeIRI`, `assertionValueIRI` and `assertionUnitIRI`. Missing-value
  tokens (`NA`, `n/a`, `null`, `none`) count as empty cells, reported once per
  column as `empty_placeholder`. Other text that is not an absolute IRI is
  withheld and listed by source row.
- The pinned DwC-DP has no `identificationQualifier` field. Wherever
  `verbatimIdentification` is filled from the supplied name, a one-word
  qualifier follows the name text (`Iguana sp. ?`, `Microcalanus spp.`). A
  qualifier that names the part it qualifies goes before that part
  (`aff. agrifolia var. oxyadenia` gives `Quercus aff. agrifolia var. oxyadenia
  (Torr.) J.T. Howell`); if that part is not in the name, nothing is built and
  the qualifier stays in the originals. Name text that already contains the
  qualifier (`Pachyporidae?`) is kept as is. `scientificName` never receives
  the qualifier, and a supplied `verbatimIdentification` is never rewritten.
- When occurrence rows keep separate events, an eventID that several rows share
  goes to one parent event holding only that eventID and an eventCategory. The
  row events link to it; no row details are combined. Placeholder eventIDs
  such as `NA` are left empty on separate row events (the originals keep them),
  so no eventID repeats.
- Specimen records without any occurrence status are `present` by convention
  (see [review policy §6.1](review-policy.md#61-who-may-decide)). This is a
  visible automatic choice with a notice and can be changed to `absent`.

## Decisions that remain

Missing occurrence status (except the specimen convention above), missing event category, loose-file roles/joins,
merging repeated event identities, physical material identity, media subjects,
molecular/legacy interpretations and subject-changing mappings still require
input. Zero quantities are highlighted in the missing-status question and do not
establish either presence or absence.

Reconstructing a combined survey target or supplying its completeness still
requires an explicit assertion. Literal and IRI scopes are not paired by
assumption. Unsupported/invalid scope constructions remain in originals.

The pinned DwC-DP scientific-name definition excludes authorship and qualifiers,
unlike the broader source definition. Recognised ranks, subgenera, hybrid
components, Unicode epithets and cultivar epithets no longer trigger questions
merely because of their shape. Other forms, authorship suffixes and qualifier
patterns still require a decision. Every hybrid component is checked separately;
no name text is removed or repaired. This is a review heuristic, not nomenclature
validation; unusual names can still require input.

Unpaired quantity/type values are retained with a notice and no abundance
interpretation. Humboldt numeric measurements without required units remain
withheld because their supported target requires that supplied unit. Neither
behavior invents a unit, count or quantity type.

## Audit and verification

Plans separate required `issues`, overrideable `automatic_choices` and `warnings`.
All options and defaults are included in the plan hash and validated. Reports
distinguish submitted user decisions from effective automatic choices, retain
notices and full scientific findings, and account for every source column and row.
The interface keeps notices separate from required choices and exposes automatic
mappings in an optional section.

Codex implemented this policy in the reviewed `5581` worktree. Claude independently
audited the triggers and challenged quantities, half-pairs, names, dates and
subject choices in the [existing collaboration session](https://claude.ai/code/session_01CpHnX64RXSgz5a6kB5Qtsa).
Original files and source claims remain the evidence; neither schema validation
nor accepting a conversion option establishes scientific equivalence.

The Docker backend matrix passes **275 tests**. Frontend lint and the production
build pass. The pinned publisher benchmark drops from **34 required choices to
2** (missing event categories and scientific-name semantics), retaining all
389 parent links, 6,235 direct survey cells, row links and original bytes. The
serialized package validates, and repeated plans/CSV outputs remain identical.
[Aggregate evidence](humboldt-benchmark.json) uses simulated answers to the two
remaining questions; it is not publisher approval or proof of scientific consistency.
