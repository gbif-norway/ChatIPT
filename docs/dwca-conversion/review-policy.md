# Semantic review policy for DwC-A to DwC-DP conversion

Implementation update, 4 October 2026: rule versions 8–9 use
[streamlined review](streamlined-review.md). Source copies, supported declared
roles and preserve-only outcomes proceed automatically with notices. This
document retains the original broader design; its blanket confirmation language
does not override the implemented distinction between interpretation and retention.
Who may make each choice (interpretation, conventional default, new fact) is set out in §6.1.

Target: ChatIPT's vendored TDWG DwC-DP `1.0_DEV` profile and 79 table schemas at
`76898192fd298c2aa170a7059e1bdadf3ee2a828` (`back-end/api/templates/dwc-dp`).
Reference sources are pinned in
`/Users/rukayasj/Projects/sandbox/dwc-dp-guides/PROVENANCE.md`. Mappings that only
work with the newer GBIF `master` schemas (for example `recordedBy` on `event`)
are out of scope. Do not mix schema generations.

Benchmark assertions are in [benchmark-cases.json](benchmark-cases.json).

## 1. Principles

1. **Originals are evidence.** Store the uploaded archive, `meta.xml`, `eml.xml` and every
   data file byte for byte with SHA-256. Do not overwrite them.
2. **Every source cell gets an outcome.** Each source column ends in exactly one disposition:
   `mapped`, `mapped+retained`, `derived` (consumed to build keys or links), or
   `retained-unmapped` with a reason. A conversion that cannot account for a column fails.
3. **The model chooses from options; code does the work.** The model may pick among
   options the rule registry generated and the validator accepts, or it may return
   `unresolved`/`ask_user`. It never writes target values, IDs, or rows.
4. **Evidence, not confidence.** Decisions are discharged by the deterministic checks in §4,
   which pass or fail. There are no numeric confidence scores or thresholds. A model's stated
   certainty is not evidence.
5. **Ambiguity is preserved, not resolved by default.** If no check discharges an ambiguity
   and the user has not answered, the value stays in its most literal supported place, or
   remains `retained-unmapped`. The report lists it.
6. **Structural validity is not semantic equivalence** (§8). A package can pass Frictionless
   and still say something the source did not.

## 2. Workflow

```
import → profile → mapping plan → semantic review → deterministic conversion → validation/report
                         ↑                │
                         └── user answers ┘   (plan revised; conversion re-run from scratch)
```

### 2.1 Import

- Parse `meta.xml` if present. Record the core `rowType`, the extensions, each `<field>`'s
  `index`/`term`/`default`, the delimiter, `fieldsEnclosedBy`, encoding, and
  `ignoreHeaderLines`. Without `meta.xml` the input is a set of **loose tables**, not an
  archive. Record that, and take terms from the headers only.
- Count rows with a real CSV parser, using the declared dialect. Never count lines. The
  Akagera `377__occurrence.csv` has quoted multi-line `occurrenceRemarks`, so physical lines
  outnumber records. A line-oriented grep for empty `materialSampleID` gives false positives
  on those continuation lines.
- Distinguish `default=` constants in `meta.xml` from data values. Gaynor's
  `basisOfRecord=MaterialCitation` exists only as a default.
- Flag malformed or non-DwC term URIs without dropping them. Gaynor declares
  `http://https://www.ebi.ac.uk/terms/study` and `.../bioproject_accession`.
- Record the null tokens observed (`""`, `NA`, `unknown`). Only `""` is treated as null
  automatically. `NA` and `unknown` are reviewed (§6).

### 2.2 Profile (deterministic, whole-file, no model)

For every table and column, compute: non-null count, distinct count, top values with
counts, regex shape classes (ISO date, float-as-year `2015.0`, URI, ENVO `label [ENVO:n]`,
DNA alphabet), numeric range, and the following:

- **Key candidates:** uniqueness of `occurrenceID`, `eventID`, `materialSampleID`,
  `identificationID`, `resourceRelationshipID`.
- **Join coverage:** for every extension, the share of foreign key values found in the
  core, the share of core rows with ≥1 extension row, and the cardinality distribution.
- **Functional dependencies:** for each candidate grouping key (`eventID`,
  `materialSampleID`, `(materialSampleID, target_gene)`, `target_gene`), which columns are
  constant within every group. This is the main evidence for moving columns out of
  occurrence (§4 E3).
- **Cross-file consistency:** whether repeated columns agree where they appear in several
  files, such as `verbatimIdentification`/`scientificName` between occurrence and
  identification history.

The profile is stored as JSON with a hash. All later stages cite profile entries rather
than raw rows.

### 2.3 Mapping plan

A declarative, versioned plan (JSON), proposed by deterministic rules:

- `column → (target_table, target_field, transform_id)` or a non-mapped disposition with a
  reason.
- `entity rules`: how target rows are formed. For example, "one `material` per distinct
  `materialSampleID`" or "one `molecular-protocol` per distinct tuple of protocol columns".
- `link rules`: how foreign keys are filled, such as `nucleotide-analysis.materialEntity_fk`
  from `materialSampleID`.
- `id rules` (§9).
- `review items`: each open decision, its trigger (§5), and the options.

Exact source/target term-IRI matches within the declared subject context are
candidates for deterministic copying. Field types, bounds, value shape and
cross-field prerequisites still apply. Technical keys and relationship fields
are excluded; a shared annotation on several fields is not enough to choose a
target. Explicit aliases require review under the implemented rules.
A match is never an approval, because the same term may exist in several tables. For
example, `verbatimIdentification` and `scientificName` exist on `occurrence`, `material`
and `identification`, so the placement is reviewed.

**Supported targets** are only (table, field) pairs present in the pinned schemas, plus
transforms in a closed registry (copy, trim, split-on-validated-pattern, ENVO label/IRI
split, sequence normalisation, group-by-dedupe, deterministic ID minting). Any other
target is rejected by the validator.

### 2.4 Semantic review

Only for items in the review queue. Items are batched per decision, not per row (§7).
Each decision is resolved by one of:

1. an evidence check in §4 that passes, which is auto-discharged and recorded;
2. a model recommendation that selects a validated option and cites the check IDs and
   profile entries it relies on. This is applied only where the decision is not in §11;
3. a user answer (§6);
4. `unresolved`: the conservative default applies and the item appears in the report.

### 2.5 Deterministic conversion

- Pure function of `(originals, approved plan, rule registry version, schema revision)`.
  Re-running gives byte-identical CSVs.
- No model calls. No network calls. Taxon re-matching is not part of conversion. Existing
  `scientificNameID` values are copied, and any COL review happens in ChatIPT's existing
  taxon review step (see `api/taxon_matching.py`).
- Each output row carries crosswalk entries (§9).

### 2.6 Validation and report

Run structural, preservation and semantic layers (§8). The report contains:

- each layer's results;
- per-column dispositions;
- the list of retained-unmapped items with counts;
- unresolved decisions;
- applied user answers;
- minted identifiers;
- a DP → DwC-A projection diff against the original (§8.3).

Publication is blocked while any structural or preservation check fails. Unresolved
semantic items do not block, but they must be acknowledged.

## 3. Conversion artefacts

| Artefact | Content | Mutable? |
| --- | --- | --- |
| `originals/` | uploaded bytes and SHA-256 | never |
| `profile.json` | §2.2 output | regenerated only if originals change |
| `plan.json` | §2.3, with review decisions embedded | versioned. Each edit makes a new version |
| `decisions.jsonl` | one line per decision: item id, options, evidence ids, decided by (`check`/`model`/`user`), model call id and packet hash, timestamp | append-only |
| `crosswalk.csv` | source file, parser record index, source key, target table, target pk, rule id | regenerated per run |
| `residuals.csv` | retained-unmapped cells (source file, record index, column, value, reason) | regenerated per run |
| `report.json`/`.md` | §2.6 | regenerated per run |

## 4. Evidence checks that discharge ambiguity

Each check is computed over the whole source and returns pass/fail with counts and
counter-examples. If a check fails, its decision becomes a review item. Counter-examples are
included in the packet.

| ID | Check | Discharges |
| --- | --- | --- |
| E1 | Every extension `occurrenceID` exists in the core. Every core row has the expected number of rows per extension. | Join correctness. Orphan and duplicate handling. |
| E2 | `occurrenceID` is unique and non-empty. `resourceRelationshipID` and `identificationID` are unique. | Use as `*ID` and pk seeds |
| E3 | Column C is constant within every group of key K | Moving C from occurrence to the entity identified by K. For example, `faecal texture` moves to `material-assertion` keyed by `materialSampleID`. |
| E4 | `eventID` ↔ `materialSampleID` cardinality (1:1, 1:n, n:1) | Material ↔ collection event linkage |
| E5 | Within `(materialSampleID, target_gene)`, `sampleSizeValue` is constant and equals the sum of `organismQuantity`, with `organismQuantityType = sampleSizeUnit = "DNA sequence reads"`. | Treating the value as a per-sample, per-assay total read count (`processedTotalReadCount` candidate) |
| E6 | Identical `DNA_sequence` ⇔ identical ESV suffix in `occurrenceID`, if the suffix pattern holds for every row | Sequence dedupe, and whether ESV labels can be kept as sequence remarks |
| E7 | Protocol columns are constant within `target_gene` | One `molecular-protocol` per marker |
| E8 | For each occurrence with an identification history, exactly one history row equals the core `scientificName` | Which identification the core reflects (evidence for, not proof of, `isAcceptedIdentification`) |
| E9 | Relationship endpoints exist as `occurrenceID`s and share `materialSampleID` and `eventID`. Each sample has exactly one subject. | Relationship scope (within-sample) and host role |
| E10 | `year` equals `eventDate[0:4]` | `year` can be dropped as derived, with no information loss |
| E11 | Event-level columns (coordinates, uncertainty, `dataGeneralizations`, `locationRemarks`, `habitat`) are constant within `eventID` | Lossless placement on `event`. If this fails, the event model is a user decision. |
| E12 | A value matches `^(.+) \[ENVO:(\d{8})\]$` for every non-null row | Splitting into value plus `...IRI`. The verbatim value is retained. |
| E13 | Occurrence attributes (`sex`, `lifeStage`, `verbatimIdentification`) on non-subject rows within a sample equal the subject row's values | Detecting host attributes copied onto prey or diet rows |
| E14 | DP → DwC-A projection reproduces each mapped source cell (§8.3) | Preservation of mapped values |

Checks are reusable and named. A model recommendation must cite the check IDs it relies on.
If a cited check failed, the validator rejects the recommendation.

## 5. Review triggers

A review item is created when any of the following holds:

- A source column has no exact term match in the pinned schema. This includes MIxS-style
  `dnaderiveddata` columns, non-DwC URIs and dynamicProperties keys.
- A term matches fields in more than one table.
- A move or aggregation requires an E-check that failed (E3, E5, E7, E11).
- One source column would fill two targets, or two columns would fill one target, for
  example `organismQuantity` and `readCount`.
- A value would be transformed beyond the closed registry, for example parsing `pcr_cond`
  into `annealingTemp`.
- Free text in remarks contradicts structured values. For example, the remark "faecal sample
  represents one host individual" sits next to `organismQuantity` holding a read count, and a
  "placeholder (pending project review)" note sits next to `coordinateUncertaintyInMeters`.
- An identification conflict: E8 fails, a field ID disagrees with a DNA ID, or a non-name
  string appears in `scientificName`.
- A relationship predicate may not fit its endpoints, for example `preys on` between an
  herbivore occurrence and a plant occurrence.
- A required target field has no source, for example `organism-interaction.event_fk`.
- Null tokens `NA` or `unknown`, numeric artefacts such as `2015.0`, or malformed name
  strings such as a repeated genus.
- Taxon/location plausibility flags from the existing taxon review. These are surfaced, not
  fixed, during conversion.

## 6. When to ask the user

Ask when the decision is listed in §11, or when it changes what the data asserts about the
world and no check discharges it. Do not ask about anything a check can settle.

Question rules:

- One question per decision, never per row. Each question includes the affected row count,
  two or three representative rows (IDs plus the relevant cells only), and the options
  produced by the registry. The conservative default is always listed first.
- Use plain language. For example: "Should 'preys on' be published as written, or does it
  mean 'eats' for plant detections?"
- Record the answer, who gave it, and the plan version it produced. Answers apply only to the
  dataset and plan version they were given for.
- If the user does not know, the item stays `unresolved` and the default applies.

### 6.1 Who may decide

Implemented 6 October 2026. Every choice belongs to one of three classes. The class decides
who may make it (`ISSUE_POLICY` and the per-issue `assertion_values` in the converter; see
`api/dwca_review.py`).

| class | examples | who decides |
| --- | --- | --- |
| **Interpretation of supplied text** | `country-label:` routes ("Norway" or "Great Britain" in `countryCode` → `event.country`, "Norwegian Sea" → `event.waterBody`); `age-remark:` routes ("juv", "ad." in `eventRemarks` → `occurrence.lifeStage` or `occurrenceRemarks`); column targets | Not assertions. The AI reviewer may apply them under the usual evidence and confidence rules; the user can change them. The text is always copied unchanged. |
| **Conventional default** | `occurrenceStatus` = `present` for specimen records (see below) | Applied automatically as a visible automatic choice with a notice and a `reason`. It is marked `convention` and can be changed like any other automatic choice. Never asked. |
| **New fact** | absence, survey completeness, survey or event category, splitting a repeated identity (`event-grain` `per_row`, `by_id_depth`, `occurrence-events` `per-row`), material identity, what media depicts, agent identity | Assertions. Only the user may choose them; an AI recommendation is shown for confirmation. |

The specimen-presence convention applies to one Occurrence table when every row declares a
`basisOfRecord` of `PreservedSpecimen`, `FossilSpecimen`, `MaterialSample`, `LivingSpecimen`
or `MaterialCitation`, no row supplies `occurrenceStatus`, no `individualCount` or
`organismQuantity` is zero, and no cell uses absence wording ("absent", "not found",
"ikke funnet", …). GBIF interprets such records as present. Any exception keeps the
present/absent question, which remains a user-only assertion.

Column targets that follow from an earlier answer are interpretations applied automatically
([streamlined review](streamlined-review.md#fewer-questions-and-plain-language)): specimen
details follow the material answer, collectors (recordedBy, with recordedByID beside them)
go to the specimen record when specimen records exist and each links to exactly one
occurrence, and type status follows the specimen answer. They are visible automatic choices
with a dataset-specific reason and can be changed. The material answer itself stays a new
fact for the user.

## 7. Model context and avoiding per-row work

A review packet (target ≤ a few thousand tokens) contains only:

- the decision id, trigger and options. Each option is a validated plan fragment;
- the relevant profile entries for the columns involved: distinct counts, top values,
  functional-dependency results, E-check results with ≤5 counter-examples;
- definitions of the candidate target fields, copied verbatim from the pinned schema
  (`description`, `comments`, `examples`);
- ≤5 representative rows, projected to the involved columns plus keys;
- the relevant EML fragments (methods, sampling) by section, not the whole document;
- prior user answers on the same dataset.

It never contains the full data files, all rows, or other datasets.

Per-row work is avoided by:

- **pattern-level decisions:** decide on a column, a grouping key or a value class, then
  apply the result deterministically;
- **value-level decisions only for small distinct sets:** for example, the three
  `measurementType` values, two `target_gene` values, and a handful of
  `relationshipOfResource` values. A decision on a distinct value applies to every row with
  that value;
- **batching:** one call can resolve several independent decisions about the same table, but
  each decision is validated separately.

Model output contract (JSON):

```
{decision_id, choice: option_id | "unresolved" | "ask_user", evidence: [check/profile ids], rationale}
```

The validator rejects output if:

- the option id is unknown;
- cited evidence is missing or failed;
- the decision is in §11 and has no user answer;
- the choice would leave a column without a disposition.

## 8. Validation layers

### 8.1 Structural (necessary, not sufficient)

- Descriptor against the DwC-DP profile.
- Each resource schema against its canonical table schema.
- Frictionless `validate()` per row.
- EML against the 2.2.0 XSD.

All of these exist in `api/dwc_dp_specs.py`: `validate_dwc_dp_resources`,
`validate_datapackage_descriptor`, `validate_dwc_dp_archive`. Also check:

- primary key uniqueness, and foreign key resolution across tables;
- required fields, for example `nucleotide-analysis.molecularProtocol_fk` and
  `nucleotideSequence_fk`, `resource-relationship.subjectResourceID`, and
  `organism-interaction.event_fk`/`subjectOccurrence_fk`.

### 8.2 Preservation (accounting)

- Every source record maps to ≥1 crosswalk entry, or to a residual with a reason.
- Every source column has a disposition.
- Row count reconciliation per entity rule. Expected aggregation ratios come from E3/E4. For
  example, `material` rows equal distinct `materialSampleID`s, and `material-assertion` rows
  equal distinct samples × assertion types, not occurrences × types.
- No value is silently altered. Transformed values keep their verbatim source, either in a
  `verbatim*` field where the schema has one (for example `verbatimAssertionType`) or in the
  crosswalk.
- Remarks are carried, not concatenated across different sources. Prior upload counter-example:
  `event.dataGeneralizations` joined host and diet statements with `" | "`.

### 8.3 Semantic equivalence (evidence, never proof)

- **Projection diff (E14):** project the DP back to DwC-A terms with ChatIPT's DwC-A
  derivation, then compare cell by cell with the original for mapped columns. Differences are
  either explained by a rule (dedupe, derived `year`) or flagged.
- **Assertion checks:** run the benchmark checks in `benchmark-cases.json`.
- **Statement review:** for each changed predicate, moved attribute or minted entity
  (organism, protocol, analysis event), the report states what the package asserts that the
  source did not state literally. Each must trace to a check or a user answer.
- Passing 8.1 and 8.2 shows only that the package is well formed and nothing was lost. Only
  8.3, together with the decisions log, supports any claim that it means the same as the
  source.

## 9. Identifiers, provenance and audit

- **Preserve source identifiers verbatim** in the corresponding `*ID` field: `occurrenceID`,
  `eventID`, `materialSampleID` → `material.materialEntityID`, `identificationID`, and
  `resourceRelationshipID`. Never rewrite them, re-case them, or parse new meaning out of them
  without E6-style proof.
- **Primary keys (`*_pk`):** use deterministic UUIDv5 under a per-dataset namespace UUID
  stored in the plan. The name is `table + "\x1f" + source key tuple`. The result is stable
  across reruns and independent of row order.
- **Minted identifiers** cover entities that have no source ID: `molecularProtocolID`,
  `nucleotideSequenceID`, `nucleotideAnalysisID`, `assertionID`, and any organism or
  analysis event. Mint them deterministically from content and record the recipe. For
  example, `nucleotideSequenceID = "seq:sha256:" + sha256(upper(sequence))`, and
  `nucleotideAnalysisID = occurrenceID + ":analysis"`. Flag them as minted in the report.
  Never use model-generated identifiers.
- **Crosswalk:** each target row lists its contributing source records. Aggregated rows list
  all of them.
- **Package provenance:** the descriptor records schema revision `76898…a828` and the profile
  SHA-256, as the export already does. Also record the conversion plan hash, the rule
  registry version, and the originals' hashes.
- **Audit:** `decisions.jsonl` is append-only. Model calls are logged with the existing
  per-dataset usage records plus the packet hash. User answers are stored with the plan
  version.

## 10. Risks shown by the sources

### 10.1 Akagera loose tables (`back-end/user_files/377__*.csv`, dataset58)

Profiled 2026-10-02. Counts come from line-based reads and must be recomputed with a CSV
parser.

**Structure and joins**

- The occurrence file contains quoted multi-line remarks (for example `S079636_ESV_110941`).
  Row counts and grep-based checks are unreliable.
- Every extension is keyed by `occurrenceID`. `dnaderiveddata` has 10,365 rows (942
  `12S rRNA` and 9,423 `trnL intron`). eMoF has 31,095 rows: exactly three types × 10,365.
  Identification history has 10,365 `_dna` and 790 `_field` rows. There are 9,575
  relationships.
- The prior DP upload has 790 events and materials, so samples ≈ 790. The arithmetic
  10,365 − 790 = 9,575 suggests exactly one subject (host) occurrence per sample, related to
  every other detection in that sample. E9 must confirm this before host roles are used for
  anything.
- `occurrenceID = materialSampleID + "_ESV_" + n`. The ESV suffix is shared across samples
  (for example `ESV_015820` for Loxodonta). The mapping must not parse this unless E6 passes
  and the user confirms (§11).

**Quantities and units**

- `organismQuantity`/`sampleSizeValue` are DNA read counts, not organisms. The source remark
  says so.
- In sample S081760, the 12S rows give 21,663 (Panthera pardus) + 127 (Tragelaphus
  scriptus) = 21,790 = `sampleSizeValue` on both rows. The trnL rows use 23,768. The
  denominator is therefore per sample and per assay, not per sample. Host-only 12S samples
  show `organismQuantity == sampleSizeValue` (for example 13,162 for S079328).
- `sampleSizeValue` exists only on `survey` in the pinned schema. The candidate target is
  `nucleotide-analysis.processedTotalReadCount`, gated by E5. `readCount` takes
  `organismQuantity`.
- Whether `occurrence.organismQuantity` also keeps the read count is a §11 decision. Keeping
  it repeats a misleading abundance semantic. Dropping it changes the DwC-A projection.
- `coordinateUncertaintyInMeters` differs within one event. Host rows have 7,901, and diet
  rows in the same sample have 21,504, because a "20 km placeholder for host daily
  displacement (pending project review)" is added. DwC-DP puts location on `event`, so E11
  fails.
- The prior upload picked one value per event: `ANP_24_DALU_47` has 30 m, while its remark
  claims the 20 km placeholder. It also concatenated both remarks. Both are silent semantic
  losses. The source itself marks the placeholder as unreviewed.
- eMoF has no `measurementUnit`. Values are categorical (`<24hr`, `3-7_days`, `observed`,
  `soft`, `hard`, `diarrhea`, `yes`, `no`). `observed` inside `faecal sample age` is a
  different kind of value. Do not convert these to durations or invent units.
- `pcr_cond` contains units (`94C 3min; 45 cycles…`). Do not parse it into `annealingTemp`
  or `annealingTempUnit`. Copy it to `molecular-protocol.pcr_cond` verbatim.

**PCR and molecular metadata**

- There are two markers with different primers and cycling: 12S (45 cycles, 52 °C) and trnL
  (40 cycles, 55 °C). E7 should yield exactly two protocols.
- `env_broad_scale`, `env_local_scale` and `env_medium` vary per sample (for example
  `agriculture field`, `grassland area` or `woodland area` for `env_local_scale`).
  `molecular-protocol` has `env_*` fields, but placing them there would split protocols per
  environment combination. Target `material-assertion`, gated by E3 on `materialSampleID`,
  with an E12 IRI split. This is what the prior upload did, and it is acceptable only with
  E3 evidence.
- `molecular-protocol` also has a `DNA_sequence` field. Sequences must go to
  `nucleotide-sequence.sequence` (997 distinct in the prior upload), not into the protocol.
- `otu_seq_comp_appr` reads "UNOISE3 via vsearch; minimum identity 95%". Denoising and
  identity clustering are different approaches. Surface this and copy it verbatim; do not
  correct it.
- `sop` is empty. `associatedSequences` is a BioProject URL repeated on every row. It belongs
  on `material.associatedSequences` or in project metadata, not in any sequence accession.
- No analysis event exists. The prior upload reused the collection event as
  `nucleotide-analysis.event_fk`, which asserts that the lab analysis happened at collection.
  Leave `event_fk` empty unless the user supplies an analysis event (§11).

**Identification multiplicity**

- Host occurrences have two identifications: `_dna` (Jonah Ventures, no date) and `_field`
  (field collector, dated, vernacular codes such as `impala`, `buffalo_cape` or
  `hyaena_spotted`).
- The source puts these codes in the `scientificName` column of the history. They are not
  scientific names. Send them to `verbatimIdentification` only.
- Conflicts are real and must be kept, not reconciled: `S079316` has DNA *Damaliscus
  lunatus* but field `impala`. `S081760` has DNA *Panthera pardus* but field
  `hyaena_spotted`.
- The source has no accepted flag. E8 (core `scientificName` equals the `_dna` row) is
  evidence about which ID the core used. Setting `isAcceptedIdentification` is a §11
  decision. The prior upload set it without asking.
- `identificationType`: `nucleotideAnalysis` is supported for `_dna` rows by the linked
  analysis. For `_field` rows, `features` is an interpretation of "field identification" and
  needs confirmation.
- Field IDs identify the depositing animal. The prior upload linked them only to
  `materialEntity_fk`, with no `occurrence_fk`. Linking them to the host occurrence, the
  material, or a minted organism is a §11 decision.
- `dateIdentified` stays empty for DNA rows. Never back-fill it from `eventDate`.

**Material and organism identity**

- One faecal `material` per `materialSampleID` contains DNA of host, prey and plants. The
  material is not the organism.
- Occurrence-level `verbatimIdentification` repeats the host field code on every diet row
  (`sheep` on a Fabaceae row, `hyaena_spotted` on a Jasminum row). Host `sex`/`lifeStage`
  leak onto 12S prey rows: Tragelaphus scriptus is tagged `unknown`/`adult`, while
  Elephant-sample plant rows are blank. Under E13, these become sample/host attributes and
  are not copied as prey attributes.
- "A faecal sample represents one host individual" supports at most one host organism per
  sample. It says nothing about the same individual across samples. Do not merge organisms
  across samples. Diet detections (family- and genus-level reads) are not organisms; do not
  mint organism rows for them.
- Source text records uncertainty about the host ("might also be from the goat…"). Retain it
  in `occurrenceRemarks`.

**Relationship interpretations**

- Every relationship is `preys on` (`RO_0002439`), with the host as subject. That fits
  leopard → bushbuck. It does not fit elephant → Fabaceae (herbivory) or leopard → Jasminum,
  where plant DNA in carnivore scat may be secondary ingestion or contamination.
- Possible publications:
  - (a) as given in `resource-relationship`, with `relationshipType` and `relationshipTypeIRI`
    verbatim and the subject/related resource type `Occurrence`. This is the conservative
    default.
  - (b) with a corrected predicate (for example "eats").
  - (c) as `organism-interaction` rows, which assert an observed organism interaction and
    need an `event_fk`.
- The prior upload's remark "Inferred feeding link from metabarcoding detections" is
  reasonable context, but it was authored by the converter, not the source. Mark such text as
  converter-authored, or ask the user.
- `relationshipAccordingTo` is a name plus ORCID in one string. Splitting it into
  `relationshipAccordingToID` requires the E12-style pattern check over all rows.

**Taxa and coordinates**

- The neotropical *Lacmellea panamensis* is detected in Rwanda. Surface it through the
  existing taxon review; do not change it.
- Coordinates are generalized for sensitive hosts, and every detection in that sample
  inherits the generalization. Keep `dataGeneralizations`, and never restore precision.

### 10.2 Prior DP-shaped upload (dataset68)

Use it as a comparison, not a gold standard. Its useful patterns:

- one material and one event per sample;
- two protocols;
- deduplicated sequences;
- `env_*` placed as `material-assertion`.

Its defects, which are regression assertions:

- collapsed per-occurrence uncertainty;
- concatenated remarks;
- unflagged `isAcceptedIdentification` and `identificationType`;
- collection event reused as analysis event;
- reads kept in `organismQuantity`;
- predicate copied without review;
- converter-authored relationship remarks.

### 10.3 Simpler examples

- **`gaynor_et_al_v3`** (DwC-A):
  - the file named `.csv` is tab-separated and `fieldsEnclosedBy=""`;
  - two malformed non-DwC term URIs;
  - `NA` used as null in `locality`, `associatedSequences` and the EBI columns;
  - `basisOfRecord` comes only from a `meta.xml` default;
  - `dynamicProperties` JSON references phylogeny tip labels in `above50_genes.nex`, and the
    pinned DwC-DP has no tree table, so retain it;
  - `materialSampleID` holds NCBI BioSample accessions (SAMN…), a candidate for
    `material-identifier` with an identifier type, not proof of physical material identity;
  - `associatedSequences` holds SRA run URLs;
  - `catalogNumber` formatting differs (`CAS 1089812` vs `CAS1199187`) and must not be
    normalised;
  - `collectionCode` embeds the institution name.
- **`newick`** (loose `occurrence.txt`, no `meta.xml`):
  - not an archive;
  - `scientificName` repeats the genus (`Pleocarphus Pleocarphus revolutus`);
  - `eventDate` contains a float artefact `2015.0` and blanks;
  - `preparations` mixes `Herbarium`, which is not a preparation, with `Silica dried`;
  - coordinates have about 14 decimals, with taxon/location combinations that look synthetic;
  - `phylo.newick` is unsupported. Surface all of these without fixing them.

## 11. Decisions that must not be guessed

These need a user answer. A passing check can narrow the options but cannot choose. The
default in brackets applies while a decision is unresolved.

1. Whether read counts remain in `occurrence.organismQuantity` as well as
   `nucleotide-analysis.readCount` [keep as in the source, and flag it].
2. Whether `sampleSizeValue` means `processedTotalReadCount` [map only if E5 passes; otherwise
   it is a residual].
3. How to model per-occurrence location uncertainty that differs within an event [host-row
   event location. Diet uncertainty and remarks become residuals and are reported. Never pick
   silently].
4. Relationship predicate correction, and `resource-relationship` versus
   `organism-interaction` [verbatim `resource-relationship`].
5. `isAcceptedIdentification` values [empty].
6. `identificationType` for field identifications [empty; verbatim remark kept].
7. Which entity field identifications attach to (host occurrence, material, organism)
   [`occurrence_fk` of the E9 subject plus `materialEntity_fk`, flagged].
8. Minting `organism` rows, and their scope [none].
9. Whether host attributes on non-subject rows (E13) are prey attributes [treated as residual
   sample attributes, not prey attributes].
10. Target of sample-level eMoF and `env_*` values when E3 fails [`occurrence-assertion`].
11. An analysis `event_fk` for `nucleotide-analysis` [empty].
12. Parsing identifiers, such as ESV labels from `occurrenceID` or ORCID from
    `relationshipAccordingTo` [no parsing].
13. Interpretation of null-like tokens (`NA`, `unknown`) [kept verbatim].
14. Repairing names, dates, coordinates or `catalogNumber` formatting [no repair].
15. Whether a repository accession (BioSample, BioProject, SRA run) identifies the physical
    material [`material-identifier`/`associatedSequences` only, with a typed identifier].
16. Anything that changes taxon names or IDs. That belongs to the taxon review workflow, not
    conversion.

## 12. Critical implementation recommendations

1. **Build the profile and E-checks first, as plain code with tests.** They are the
   foundation. Without them, model review becomes guessing. Use a CSV parser that honours
   `meta.xml`, and add a test with embedded newlines.
2. **Keep a closed transform registry and a plan validator** that checks every target against
   `load_dwc_dp_table_specs()`. Model output is data that goes into the validator, never code.
3. **Make preservation accounting a hard gate.** Every source column needs a disposition, and
   `residuals.csv` must be complete. This is what prevents the dataset68 failure modes.
4. **Never aggregate onto `event` or `material` without a passing E3/E11.** When a check
   fails, raise a review item; do not pick the first value or concatenate.
5. **Use deterministic UUIDv5 primary keys under a stored per-dataset namespace, and
   content-hash minted IDs.** Preserve all source `*ID`s verbatim.
6. **Ask about §11 items as grouped, row-count-annotated questions** with the conservative
   default first, and record answers per plan version.
7. **Report structural, preservation and semantic results separately.** Never present a
   Frictionless pass as proof of equivalence. Include the DP → DwC-A projection diff.
8. **Run `benchmark-cases.json` in CI inside the back-end container**, using the Akagera and
   simple examples. Add the dataset68 defects as negative assertions so regressions fail
   loudly.
9. **Keep schema-generation discipline.** Pin the revision in the plan. When the snapshot
   moves (1.0 ratification, or GBIF changes such as `recordedBy` on event), re-validate
   plans; do not migrate them silently.
10. **Treat converter-authored text as provenance, not data.** Any remark the converter writes
    must be labelled as such in the crosswalk, or replaced by a user-approved statement.
