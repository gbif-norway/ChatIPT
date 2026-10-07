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

The earlier per-value questions remain as a fallback for what the tidy-up leaves: `country-label:` (a
label in `countryCode` that is still not an ISO code) and `age-remark:` (an event remark of an occurrence
row that still starts with a life-stage word, such as `1 juv.`). Values the tidy-up settled no longer
qualify, and a value it offers as a suggestion is not asked about as well; undoing a change brings the
question back for its values. With `CONVERSION_TIDY_ENABLED=0` every such value is asked about.

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
| Seas and oceans in country columns (curated names) | `Mediterranean Sea`, `North Atlantic Ocean (other parts)` → `waterBody` | auto |
| Other country-column text with a water word | `United Kingdom (English Channel)` → `waterBody`? | suggestion |
| Life stages in remarks of occurrence rows | `eventRemarks` `juv.` → `lifeStage` `juvenile` | auto |
| Decimal commas in numbers | `1000,0` → `1000.0` | auto |
| All-zero elevation and depth | every row 0 in all four elevation/depth columns → empty | auto (two or three such columns: suggestion) |
| Spelling variants | `2 cy.` → `2 cy` when `2 cy` is more common | auto |
| Comma that may be a thousands separator | `1,000` → `1.000` | suggestion |
| Trailing separator | `1,` → `1` | suggestion |
| Characters damaged by an encoding problem | `B\x99SINGEN` → `BÖSINGEN` | suggestion |

Details that matter:

- Vocabulary output uses the GBIF concept (`tidy-vocabularies.json`, snapshot of the GBIF vocabulary
  server with curated aliases). GBIF's hidden labels are not used because some are wrong (Female lists
  `juv`). Sex has no "unknown" concept, so `Unknown` sex is left empty; life stage `Unknown` becomes the
  GBIF concept `unknown`.
- `countryCode` `NA` is Namibia, never a placeholder (lower-case `na` is a placeholder); a bare `NA` in `country`
  is ambiguous and stays as written. Country names and aliases come from ISO 3166-1
  (pycountry) plus a curated alias list (`tidy-countries.json`): Great Britain, England, Scotland and Wales
  are GB; Svalbard and Jan Mayen is SJ; Norge, Sverige and other Nordic names are included.
- A change applies to a row as a whole: its own rewrite and every value it moves or fills happen only when
  each destination cell is empty or already says the same. Otherwise the whole row is left as written and
  counted as a conflict (for example `countryCode` `Norway` beside `country` `Sweden` keeps both). Fills
  never overwrite a different supplied value. Changes whose fills all agree already change nothing and are
  not listed.
- All-zero elevation and depth are cleared automatically only when all four elevation/depth columns are
  zero on every row (the placeholder pattern of 572), with a visible Undo. Two or three such columns could
  be a real shoreline or surface survey and are only suggested; zero depths alone stay.
- Protected columns are never changed: identifiers and anything ending in `ID`, catalogue and record
  numbers, scientific names and other taxon terms, identification qualifiers, dates and times,
  coordinates, `verbatim*` fields, measurement terms other than remarks, relationship terms other than
  remarks, `dynamicProperties` and all terms outside the Darwin Core namespace.

## Model layer (one call per dataset)

Values the rules cannot settle go to the model once per dataset (`conversion_tidy.py`): `eventRemarks`
`fad` (adult female in an arachnid archive), `1 juv.`, copepod life stages `AF`/`CV`, a stateProvince
`M�re og Romsdal` with a lost character, a sea name in `county`.

- **When**: after inspection a `tidy` job runs when AI is available (`CONVERSION_AI_REVIEW_ENABLED` and an
  API key) and some value has no stored answer. The page shows *Reading through your values to tidy them
  up…*; the job then rebuilds the plan like a replan and hands over to the AI review and name checks.
  Any failure, timeout, missing key or cost refusal keeps the deterministic plan and never blocks.
- **What is sent**: per column, distinct values that no rule changed and that are not already valid —
  value fields (lifeStage, sex, establishment vocabularies, behavior, preparations, organismQuantityType,
  …), remarks of occurrence rows, and place names only when they look damaged or misplaced (a sea or a
  country in county/stateProvince). Columns with more than 300 such values (free text such as
  localities) are never sent, nor are protected columns or columns the tidy-up added. At most 1,500
  values per call, each clipped to 200 characters, with counts and the most frequent values of up to six
  neighbouring columns (sex, counts, places, remarks), plus the dataset title and description.
- **Call**: `gpt-6-sol` at medium effort on Flex (`OPENAI_CONVERSION_TIDY_MODEL`,
  `OPENAI_CONVERSION_TIDY_EFFORT`), reserved and recorded under the dataset cost limit with task
  *DwC-A conversion tidy-up*. The answer is strict JSON: per value, `fields` from a closed set,
  `residue` (what the fields do not capture), `confidence` and a short `note`. Values are untrusted data.
- **Validation**: an answer may only fill fields a value of that column can plausibly state (a remark:
  life stage, sex, count and other organism fields; a country column: countryCode, country, waterBody;
  …). sex must be GBIF Sex concepts, establishment vocabularies GBIF concepts, individualCount a whole
  number, countryCode an ISO code. Protected fields are never written.
- **Exact words are kept**: a remark about the organism moves its exact text to `occurrenceRemarks`
  (an `eventRemarks` value leaves the event); a value whose reading leaves something over (`Female?`)
  keeps its exact text in `occurrenceRemarks` beside the interpreted field.
- **Tiers**: high confidence applies automatically; medium only when another column of the same row
  already agrees (`fad` with sex `f`); a conflict with the row (`1 juv.` where individualCount is 2)
  makes a suggestion. Agreement is counted against the source after the rules only, so answers never
  corroborate each other. Clearing a value, rewording free text that is not damaged (`ind/m3`), and a
  non-GBIF life stage read from another field are always suggestions. Low confidence changes nothing.
- **Cache**: answers are stored in `conversion.tidy.model` with the source fingerprint and prompt
  version, so inspect, replan and convert produce the same view and plan id without calling again.
  A byte-identical re-upload by the same owner reuses them (571/572 were re-uploads of 558/559).

## Seeing and undoing changes

`conversion.tidy` (JSON) holds the overrides for the current source and a bounded summary of the last
result. The API state has `tidy: {enabled, pending, model, groups, overrides, counts}`. Each group is one rule
applied to one column with a plain-language title, for example *"countryCode held country names in
70,951 rows. The names now go to country and countryCode gets ISO codes: NO, SJ, SE, SH."*

The review page shows these in **Here's what we tidied** (`ConversionTidySummary.js`): each group with
examples and **Undo**, and suggestions with **Apply** (one value) or **Apply all** (every value of the
group, including any beyond the 30 listed). Undo/Apply posts
`{action: 'tidy', plan_id, changes: {id: 'undo'|'apply'|null}}`; ids are group ids
(`tidy:t:c:rule`) or value ids. Overrides survive a re-inspection of the same source.

A group action replaces the choices made for its single values. An Undo or Apply queues a `replan` job,
which never calls the model (stored answers are reused). It rebuilds the plan from the tidied view and carries
the user's state to the new plan id: decisions whose id, column term and option still exist
(AI-reviewer decisions only when their question is unchanged), their provenance events (copied with
`transcript.carried_from_plan`), AI review records for unchanged questions and the name review (names
are never tidied). The conversation moves to the new plan only when every question and proposal in it
is unchanged; otherwise it stays with the previous plan and a fresh one starts. If the replan fails,
the previous plan and overrides stay.

## Report

`conversion-report.json` has a `tidy` section: version, digest, overrides and every group with every
distinct value (space-only groups list their first 500), its rows, output fields, whether it was applied, and changed/conflict/agree row counts.
The value-disposition ledger adds `tidied_values` (cells rewritten), `tidy_cleared_values`
(placeholders cleared or values moved out; both from group totals, so they also cover values a report
group does not list) and `source_nonempty_values` per source column, and marks
added columns with `tidy_added`.

## Production archives (566–572)

Questions: production at the time → current rules without the tidy-up → with the tidy-up (rules and one
`gpt-6-sol` Flex call; prod decisions where still valid, otherwise first options).

| Archive | Questions | Notable tidy-up |
|---|---|---|
| 566 | 1 → 1 → 1 | 7,529 `NA` measurementRemarks cleared; the model left the remarks as written |
| 567 | 2 → 2 → 2 | model: copepod `AF`/`AM` → adult + female/male, `CI`–`CV` → copepodite I–V; two larva readings suggested |
| 568 | 19 → 18 → 18 | sex/lifeStage terms, 34 decimal-comma elevations, `Female + Male` → `female \| male`; `Female?`/`Male?` and umlaut repairs suggested |
| 569 | 3 → 3 → 3 | countryCode NO filled for 14,776 rows; model repaired `M�re og Romsdal` → `Møre og Romsdal` |
| 570 | 17 → 17 → 2 | all 15 country-label questions gone; ISO codes for 70,951 rows; sea names → waterBody (model: `North Sea` from county); sex `Unknown` ×63,677 empty; Pullus → nestling |
| 571 | 2 → 2 → 2 | countryCode UG filled; nothing for the model |
| 572 | 16 → 14 → 3 | all life-stage remark questions gone: 1,525 rows by rule, `fad` and `ad + egg` by the model (exact text kept in occurrenceRemarks), `1 juv.` and `ad.m.egg` suggested; f/m → female/male; all-zero elevation/depth cleared |

Every package validated. The six model calls cost $0.027 in total ($0.001–0.007 each, 2–9 s on Flex).
