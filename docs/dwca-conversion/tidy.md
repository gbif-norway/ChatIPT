# Automatic value tidy-up

Before the review questions are built, the converter tidies obviously messy cell values and tells the
user what it did. Each change can be undone, and suggestions that need a person can be applied with one
click. The aim is fewer, smarter questions: spelling variants of one life stage, or a country name typed
into `countryCode`, should not each become a question.

The tidy-up never touches the uploaded bytes. It rewrites a *view* of the parsed archive
(`api/dwca_tidy.py:tidy_archive`), so `source-originals.zip` and `uploaded-archive.zip` stay byte-exact,
and every change is listed in `conversion-report.json`. It never creates a blocking question.

## Where it runs

`conversion_jobs.load_sources()` reads the upload and returns the tidied view
(`conversion_tidy.tidied()`). Inspection, the AI reviewer's evidence, the conversation, name checks and
conversion all read this same view, so the plan built at inspection and the plan rebuilt at conversion
have the same id. The plan records `plan['tidy'] = {version, sha256}`; the digest covers every applied
change, so an undo or a new rule changes the plan id.

Columns the tidy-up adds (for example `country` filled from names that were in `countryCode`, or
`lifeStage` filled from remarks) are appended after the table's own columns, so existing
`column:t:c` ids stay stable. Their plan column carries `tidy_added`.

With `CONVERSION_TIDY_ENABLED=0` (or when `build_plan` is given an untidied archive) the earlier
per-value questions return as a fallback: `country-label:` (a label in `countryCode` that is not an
ISO code) and `age-remark:` (an event remark that starts with a life-stage word).

## Rules (deterministic, `TIDY_VERSION` 1)

Rules work on the distinct values of a column and run in this order; the first that applies wins.
Leading/trailing spaces and runs of spaces are removed on their own when nothing else applies.

| Rule | Example | Tier |
|---|---|---|
| GBIF vocabulary terms (lifeStage, sex, establishmentMeans, degreeOfEstablishment, pathway, occurrenceStatus, basisOfRecord) | `f` → `female`, `Pullus` → `nestling`, `Female + Male` → `female \| male`, `Preserved specimen` → `PreservedSpecimen` | auto |
| Placeholders left empty | `NA`, `n/a`, `Unknown`, `-`, `?`, `Unknown or unrecorded` → empty | auto |
| Country names in `countryCode` | `Norway` → `NO`, the name moves to `country`; `Great Britain` → `GB` | auto |
| ISO codes from `country` names | `country` `Sweden` fills an empty `countryCode` with `SE` | auto |
| Code forms | `se` → `SE`, `NOR` → `NO` | auto |
| Seas and oceans in country columns | `Mediterranean Sea` → `waterBody` | auto |
| Life stages in remarks of occurrence rows | `eventRemarks` `juv.` → `lifeStage` `juvenile` | auto |
| Decimal commas in numbers | `1000,0` → `1000.0` | auto |
| All-zero elevation and depth | every row 0 in an elevation and a depth column → empty | auto |
| Spelling variants | `2 cy.` → `2 cy` when `2 cy` is more common | auto |
| Comma that may be a thousands separator | `1,000` → `1.000` | suggestion |
| Trailing separator | `1,` → `1` | suggestion |
| Characters damaged by an encoding problem | `B\x99SINGEN` → `BÖSINGEN` | suggestion |

Details that matter:

- Vocabulary output uses the GBIF concept (`tidy-vocabularies.json`, snapshot of the GBIF vocabulary
  server with curated aliases). GBIF's hidden labels are not used because some are wrong (Female lists
  `juv`). Sex has no "unknown" concept, so `Unknown` sex is left empty; life stage `Unknown` becomes the
  GBIF concept `unknown`.
- `countryCode` `NA` is Namibia, never a placeholder. Country names and aliases come from ISO 3166-1
  (pycountry) plus a curated alias list (`tidy-countries.json`): Great Britain, England, Scotland and Wales
  are GB; Svalbard and Jan Mayen is SJ; Norge, Sverige and other Nordic names are included.
- A value moves out of its column (sea names, remark life stages) only when the destination cell is
  empty or already says the same; otherwise the row is left as written and counted as a conflict.
  Fills (`country`, `countryCode`) never overwrite a different supplied value.
- All-zero elevation and depth are cleared only when at least one elevation column *and* one depth
  column are zero on every row (a placeholder pattern seen in 572); real zero depths alone stay.
- Protected columns are never changed: identifiers and anything ending in `ID`, catalogue and record
  numbers, scientific names and other taxon terms, identification qualifiers, dates and times,
  coordinates, `verbatim*` fields, measurement terms other than remarks, relationship terms other than
  remarks, `dynamicProperties` and all terms outside the Darwin Core namespace.

## Seeing and undoing changes

`conversion.tidy` (JSON) holds the overrides for the current source and a bounded summary of the last
result. The API state has `tidy: {enabled, pending, groups, overrides, counts}`. Each group is one rule
applied to one column with a plain-language title, for example *"countryCode held country names in
70,951 rows. The names now go to country and countryCode gets ISO codes: NO, SJ, SE, SH."*

The review page shows these in **Here's what we tidied** (`ConversionTidySummary.js`): each group with
examples and **Undo**, and suggestions with **Apply**. Undo/Apply posts
`{action: 'tidy', plan_id, changes: {id: 'undo'|'apply'|null}}`; ids are group ids
(`tidy:t:c:rule`) or value ids. Overrides survive a re-inspection of the same source.

An Undo or Apply queues a `replan` job. It rebuilds the plan from the tidied view and carries
the user's state to the new plan id: decisions whose id, column term and option still exist
(AI-reviewer decisions only when their question is unchanged), their provenance events (copied with
`transcript.carried_from_plan`), AI review records for unchanged questions, the name review (names
are never tidied) and the conversation. If the replan fails, the previous plan and overrides stay.

## Report

`conversion-report.json` has a `tidy` section: version, digest, overrides and every group with every
distinct value, its rows, output fields, whether it was applied, and changed/conflict/agree row counts.
The value-disposition ledger adds `tidied_values` (cells rewritten), `tidy_cleared_values`
(placeholders cleared or values moved out) and `source_nonempty_values` per source column, and marks
added columns with `tidy_added`.

## Production archives (566–572, deterministic rules only)

| Archive | Questions before → after | Notable tidy-up |
|---|---|---|
| 566 | 1 → 1 | 7,529 `NA` measurementRemarks cleared |
| 567 | 2 → 2 | copepod stages left for the model layer |
| 568 | 18 → 18 | sex/lifeStage terms, 34 decimal-comma elevations, `Female + Male` → `female \| male`, umlaut repairs suggested |
| 569 | 3 → 3 | countryCode NO filled for 14,776 rows |
| 570 | 17 → 2 | all 15 country-label questions gone; ISO codes for 70,951 rows; 3 sea names → waterBody; sex `Unknown` ×63,677 empty; Pullus → nestling |
| 571 | 2 → 2 | countryCode UG filled |
| 572 | 14 → 3 | all 11 life-stage remark questions gone (1,525 rows → lifeStage); f/m → female/male; all-zero elevation/depth cleared |

Every package validated.
