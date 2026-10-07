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
- Names without identifiers in mapped `*By` fields link to one Agent per exact
  (whitespace-normalised) name, with a role row per mention. This is an
  automatic choice with a reason naming the mention count (`agent-names` for all
  names, `agent-share:` per repeated name), never a question; either can keep
  names as text only. The `agent-names` choice is also a notice with a link to
  change it; the notice goes once every name is kept as text. Placeholders and
  doubtful names (with `?`) never become Agents, and explicit IDs decide
  identity on their own (see fidelity-audit.md).
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
  `cf.` or `aff.` goes before the final epithet of a binomial or trinomial
  (`Tropidolaemus cf. subannulatus Gray, 1842`); other one-word qualifiers
  follow the name and precede any authorship (`Iguana sp. ?`,
  `Microcalanus spp.`). The authorship is the supplied
  `scientificNameAuthorship` when it ends the name, otherwise text after the
  name words that starts with `(` or a capital. A parenthesised subgenus right
  after the genus is part of the name (`Calanus (Calanus) cf. finmarchicus`).
  Names with author particles (`de Vries`), hybrid signs, groupings or
  life-stage words (`complex`, `agg.`, `larva`), designations after `sp.`
  (`Aus sp. A`) or a rank marker after an author are not split, and their
  qualifier stays in the originals. So does `sp.`, `spp.` or `indet.` beside
  a species epithet, which it would contradict (`Quercus robur L.` + `sp.`). A qualifier that names the part it qualifies goes before that part
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

## Fewer questions and plain language

October 2026 (next rule version, after 25). A review of production runs 560–572
found that most column questions had only one sensible answer, and that users could
not tell the options apart ("occurrence → recordedBy, event → eventConductedBy,
material → collectedBy all seemed fine to me"). The converter now asks less, explains
what it decided, and words the remaining questions for non-experts.

- **Specimen details follow the specimen answer.** A column whose only Data Package
  field is on the specimen (material) record — catalogNumber, institutionCode,
  collectionCode, preparations, disposition, recordNumber, modified, … — is no longer
  asked about. It is stored on the specimen records when specimen records are created
  and stays in the original files when they are not. The column carries
  `follows: 'material:<t>'`; the specimen question (or automatic choice) lists these
  columns as `followers` and names them in its explanation.
- **Conditional defaults.** A column may carry `default_when`: branches of
  `{value, when, reason}` whose conditions use the requirement language. The first
  branch that holds is the column's default; an explicit choice always wins.
  `dwca_review.conditional_defaults` resolves them (by fixed point, since one column
  can follow another), `effective_decisions` includes them, and the conversion state
  returns them as `conditional_defaults` with the dataset-specific reason. Option
  availability for a decision that conditions refer to (the specimen answer) is
  evaluated as if that option were chosen. Nested Taxon-core occurrence plans resolve
  their own conditions.
- **Collectors.** In an Occurrence core (or an Occurrence extension of an Event core)
  with possible specimen records, recordedBy goes to `material.collectedBy` when
  specimen records are created and every specimen links to exactly one occurrenceID
  that no other specimen uses; otherwise it stays on `occurrence.recordedBy`. GBIF reads
  a specimen's collectors only through that unambiguous `evidenceForOccurrenceID`
  link, and the converter writes the link only in that case. recordedByID follows the
  names (`material.collectedByID`, `event.eventConductedByID` or
  `occurrence.recordedByID`); a requirement refuses any choice that would put names and
  identifiers on different records. Recording who carried out the fieldwork
  (`event.eventConductedBy`) remains an option.
- **Type status.** typeStatus and typeDesignationType go to the specimen record when
  specimen records are created (combined specimens only when their values agree), and
  to the identification otherwise. Neither is asked any more.
- **Media.** The standard media-vocabulary matches (dcterms:format, created, creator and
  dc:type to the media/provenance fields) are automatic choices instead of confirmations.
- **scientificName** no longer asks which table; in the occurrence context the options
  are the occurrence's scientificName or leaving it empty (the name check settles that).
- **Questions an earlier answer settles.** An issue may carry `ask_when`; while its
  conditions do not hold it is not unresolved and its default applies (for example a
  core materialEntityID question while no specimen records are created). The AI
  reviewer's dependencies include `ask_when` and `default_when` references, so it waits
  for, and is invalidated by, the earlier answer.
- **Visible automatic choices.** These choices are automatic choice entries with a
  `family` (people, type status, media, …) and `glance: true` when worth a look. The
  interface lists them in a "What we decided for you" panel above the questions, with
  the reason for this dataset and a Change button; routine choices are collapsed into a
  count, and the specimen details form one line. Notices that repeat an automatic
  choice are not listed twice.
- **Plain-language glossary.** `api/dwca_glossary.py` holds every explanation of tables,
  fields and options in one reviewed file, pinned to the DwC-DP snapshot. Options carry
  a plain `label` and a `technical` name; `plan['glossary']` carries, for the targets the
  plan can show, a gloss, a consequence, when to choose it and the official definition.
  Column questions with several targets use family wording (people, identification,
  record details, type status, specimen identifiers). Evidence packets add the plain
  labels and glosses, and the AI reviewer writes its `user_question` with them; the
  question card shows that `user_question` first when there is one.

The DwC-A projection used for GBIF validation fills an empty recordedBy from the
collectors of the one specimen linked to the occurrence, then from ordered collector
agent roles, as GBIF's own DwC-DP ingestion does; an identifier comes along only
with its names.

On production sources the questions drop from 18 to 3 (568), 5 to 1 (562) and
15 to 13 (572; its 12 remaining questions are per-value remarks handled by value
tidying); 560 and 563 are unchanged at 3.

## Decisions that remain

Missing occurrence status (except the specimen convention above), missing event category, loose-file roles/joins,
merging repeated event identities, physical material identity, media subjects,
molecular/legacy interpretations and subject-changing mappings with several real
options still require input. Zero quantities are highlighted in the missing-status question and do not
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
The interface keeps notices separate from required choices, lists automatic choices
in the "What we decided for you" panel and keeps per-column mappings in an optional
section.

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
