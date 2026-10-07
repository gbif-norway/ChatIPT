# Taxon-core conversion

Taxon cores are accepted from meta.xml archives, ZIPs, or loose `taxon.csv`,
`taxon.tsv` and `taxon.txt` files. Loose extensions join by supplied `taxonID`;
manifest extensions join by their declared `coreid`, even when it differs from
the persistent taxon identifier. Missing, duplicate or dangling archive join IDs
are rejected. Unknown loose tables without verified links remain independent
additional tables.

## Package format

The [TDWG guide](https://dwc.tdwg.org/dp/) permits additional resources alongside
standard DwC-DP tables. The pinned 79-table implementation has no standalone
taxon/name-usage table. Its `taxonID` fields refer to external records, rather
than establishing an internal checklist. The converter therefore uses two
explicit output formats:

| Source/review choice | Output |
| --- | --- |
| Checklist alone, or occurrence extraction declined | Frictionless Data Package (`profile: data-package`), labelled **taxonomy data package** |
| Actual Occurrence extension approved for extraction | DwC-DP containing standard occurrence/event tables plus additional taxonomy tables |

A standalone checklist is not labelled or validated as a standard DwC-DP.
Taxonomy conversion does not create fake occurrences for taxa, distribution
statements, vernacular names or type/specimen citations.

## Deterministic taxonomy tables

`taxonomy-taxon.csv` copies every core value as a string, including names,
authorship, taxon identifiers, ranks, parent links, synonym links and unknown
fields. `taxonomy-extension-N.csv` copies every extension row, preserving
multiplicity. Term IRIs identify source columns; field descriptors record those
IRIs. Original bytes, delimiters and headers remain in `source-originals.zip`;
an uploaded ZIP is also retained unchanged.

The core's `archive_join_id` remains distinct from `taxonID`. Extensions have a
generated `source_row_id` and, where verified, an `archive_taxon_id` foreign key.
All tables declare only the empty string as missing; literal `NA` and surrounding
spaces survive. Names are not matched, authorship is not split, synonyms are not
resolved, and taxonomy columns are not coerced to numeric types.

Parent, accepted-name and original-name fields get internal foreign keys only
when the supplied `taxonID` values are complete and unique and every populated
reference resolves exactly. Otherwise the original reference strings remain,
with unresolved/external and ambiguous-local counts in the report. No network
resolution or claim of hierarchy correctness is made.

## Actual occurrence extraction

Only declared `dwc:Occurrence` extensions with verified core attachments expose
this choice. Each extension uses the existing bounded review rules for status,
event grain, numeric fields, scientific names and material evidence. Separate
events preserve occurrence context; grouping requires consistent event values.

Empty classification fields can use the exact attached Taxon row after explicit
approval. If any overlapping classification value disagrees, the converter
retains the occurrence's own classification and fills no Taxon classification
values into that row. Differences are reported. Parent and accepted-name links
are not used to substitute a different taxon.

Local taxon IDs stay in the taxonomy tables. Copying an external IRI into a
standard DwC-DP `taxonID` field requires review of its external meaning; lexical
URI recognition does not prove that it resolves. `taxonomy-occurrence-links.csv`
records every original Taxon-to-Occurrence attachment and its generated output
key, so local identifiers do not need to be promoted to external identifiers.
Row crosswalks record source files/records and linked classification context.

## Validation and evidence

Both formats validate the serialized CSVs, schemas, primary/foreign keys and
original-file hashes offline. Canonical DwC-DP tables retain their existing
validation; additional taxonomy resources use their explicit Frictionless
schemas and cannot replace reserved tables. The report and download name expose
the actual output format.

Synthetic tests in `back-end/api/test_dwca_taxon.py` cover loose files and ZIPs,
synonyms, parent links, external/ambiguous references, extension multiplicity,
unknown fields, literal missing-value tokens, occurrence classification conflicts,
multiple occurrence extensions, reproducibility, broken serialized foreign keys,
queue/export/download behaviour and original ZIP preservation. No real Taxon-core
archive or live model recommendation quality has been benchmarked yet.

## Scientific-name check

Occurrence and Identification names are parsed with GBIF's name parser and
matched against Catalogue of Life XR through GBIF's v2 matcher
(`checklistKey`), in `back-end/api/conversion_names.py` with
`back-end/api/taxon_matching.py`. A match is a suggestion. Only names are
published. COL usage IDs are kept as provenance in the report and never become
`taxonID`. `verbatimIdentification` keeps the source text.

**How COL names are written.** COL names come from the v2 usage `name` with its
trailing authorship removed, not from `canonicalName`:

- **Rank markers are kept.** `canonicalName` drops "subsp."/"var."/"f." and turns
  "Betula pubescens subsp. czerepanovii" into a different botanical combination.
- **The authorship is not a plain suffix (some autonyms).** The canonical name is
  used instead.
- **There is no authorship.** Trailing words the canonical name lacks are dropped,
  such as an author left in a hybrid formula.
- **A parenthesised subgenus is kept only for a subgenus itself.** v2 writes
  "Acartia (Acartiura) longiremis", but the name is published as
  "Acartia longiremis", as the canonical name and most users write it.

The publication workflow's matcher shares these rules.

**Comparing names.** The asserted name is the parsed name, or the label without
its qualifier. It is compared with each COL usage by its parts: the genus or
uninomial, an infrageneric epithet with its marker, and the species and lower
epithets. Rank markers, hybrid signs, authorship and case are ignored.

- "Taraxacum sect. Ruderalia" and "Hieracium subg. Pilosella" keep their
  infrageneric epithet.
- A parenthesised subgenus counts only when no species epithet follows it:
  "Calanus (Calanus)" is the subgenus.
- "f. sp." is one marker.

The same name is never a change, whatever ranks the source supplies: "Larus sp."
accepted as the genus Larus keeps the user's assertion. Otherwise the change has
one of these kinds (`taxon_matching.name_change`):

| Kind | When | Example |
| --- | --- | --- |
| coarser | HIGHERRANK match, fewer parts, or a higher rank | "Calanus" → the phylum Arthropoda; "Taraxacum sect. Ruderalia" → Taraxacum |
| finer | more parts than the user asserted | "Carex nigra" → Carex nigra var. juncea |
| genus | another genus | "Trientalis europaea" → Lysimachia; "Calanus" → the plant genus Cajanus |
| epithet | another epithet in the same genus | "Parus major" → "Parus minor" |
| spelling | see below | Circium → Cirsium; Trema orientalis → Trema orientale |

A change is a **spelling** correction only when all of these hold:

- the match is VARIANT or FUZZY;
- the name has the same number of parts and the same rank;
- the epithets are identical, or differ only by a Latin gender ending
  (-us/-a/-um, -is/-e, -er/-ra/-rum);
- the genus is at most 2 edits away, or at most 1 edit for a genus of 5 letters
  or fewer;
- the usage's kingdom equals the source's kingdom hint, and its class and family
  equal any class and family hints.

A uninomial also needs a class or family hint to qualify.

The server assigns each checked label to a group and computes which bulk choices are safe. A group decision never overwrites a one-row user decision. Rows that cannot take a bulk choice remain available for per-row review.

| Group | Who is in it | Default | Bulk options | Per-row only |
| --- | --- | --- | --- | --- |
| Accepted automatically | Exact same-name COL matches without a qualifier or conflict | COL when authorship agrees; otherwise a lossless split, then keep | COL name, parsed split, keep | Alternatives, empty |
| Uncertain to genus or family | Resolvable `sp.`, `spp.` and `indet.` labels | Publish the exact genus or family stem | Stem, keep | Candidate choice, empty |
| Spelling differs from COL | `VARIANT`, `FUZZY` or `CANONICAL` matches with a usage, no qualifier or conflict | COL when any row is eligible; otherwise mine | COL, mine | Changes needing confirmation |
| Not confirmed by COL | Ambiguous names, higher-rank-only matches, no match, doubtful qualifiers and unresolvable stems | Mine | Mine where eligible | Choose a candidate, confirm a replacement, empty |
| Check against your data | Name, identifier, kingdom or phylum conflicts | No selection | COL, mine when eligible | Resolve each row where a bulk choice is unsafe |

`mine` uses the parsed split only when it is lossless and there is no qualifier. Otherwise it keeps the supplied text. `cf.`, `aff.`, `nr.` and other doubtful qualifiers are never accepted automatically or in bulk. The server checks eligibility again when it applies each choice.

**Automatic acceptance.** Same-name exact COL matches and resolvable uncertain stems are accepted automatically. `auto:exact` and `auto:uncertain` never write a coarser or different name, and never replace a user decision. Each automatic group has Undo all; a row can also be undone. Undo records `auto_declined`, so the label stays declined after a re-check.

For `sp.`, `spp.` and `indet.`, the published `scientificName` is the genus or family stem. The identification table receives a `taxonFormula` column when the DwC-DP table specification includes it; formula values such as `A sp.` fill blank cells only. DwC-DP has no `identificationQualifier` field. `verbatimIdentification` is unchanged.

A `stem` decision requires an exact COL match for the asserted stem. If homonyms disagree on authorship, or COL's authorship disagrees with any supplied authorship, the stem has no authorship. For an exact-name automatic decision, a differing supplied authorship is kept. COL authorship is written only when it agrees with the supplied authorship; when COL has no authorship, the supplied authorship survives.

**Check against your data.** A conflict is grouped by its signature. For example, in conversion 558, `kingdom Animalia` versus COL's `Plantae` groups the affected labels together; one decision can cover 81 names. `Sapotaceae sp` in that group is written as the stem. A bulk `mine` choice keeps or losslessly splits each name as above.

**Source IDs are a second matching step.** A consistent source `scientificNameID` or `taxonID` can disambiguate a non-exact first match. It never overrides an exact same-name match: `Parathemisto libellula` stays `Parathemisto libellula`. ID diagnostics remain available as issues. For example, Calanus and Chaetognatha without IDs are not confirmed; a matching WoRMS ID can confirm the same name. An ID that points elsewhere or is not found is shown as a conflict or reason.

Exact COL matches are accepted automatically only when every supplied authorship agrees with COL. The supplied authorship is from the label or its `scientificNameAuthorship` column. The spelling group uses the same rule. The comparison (`taxon_matching.authorships_agree`) works as follows:

- years must be equal when both give one;
- each author's surname must match exactly, by a curated abbreviation, or as a surname abbreviation of at least 4 letters;
- initials given on both sides must be equal; initials on one side only are accepted for the same full surname with the same year;
- punctuation, spacing, parentheses and square brackets around a year do not matter;
- `A in B` counts A, `A ex B` counts B, and `et al.` compares the first author only;
- when COL has no authorship, nothing is overwritten, so it agrees.

Bulk actions create a batch. Undo restores only labels that still carry that batch ID; rows changed since the batch are left alone. The groups and eligible counts are recalculated by the server for each request.

**Re-inspection carries name decisions** (`conversion_names.carry_decisions`).
A new rule version makes a plan stale, and the re-inspection used to drop every
name decision.
- **When decisions carry:** the user's own decisions carry over by label when
  the source is unchanged (same fingerprint). If the source changed but yields
  exactly the same labels, a decision carries only where that name's hints,
  supplied rank, supplied authorships and qualifier are unchanged.
- **Checking again:** when a COL decision is carried, the names are checked
  again even if name checks are switched off.
- **Notice:** the name section says how many decisions were kept.
- **What is not carried:**
  - bulk decisions, because the bulk actions are offered again under the
    current rules;
  - automatic decisions, because they are made again from the fresh matches.
  - name results, because the names are checked again.
- **Undo state:** an automatic decision declined with Undo all or per-row undo
  stays declined for labels carried into the re-inspection.
- **Re-checking:** a carried COL decision loses its `changeKind`, so it is
  checked again under the current rules. One that now replaces the name is held
  until confirmed; an explicit earlier confirmation stands.
- **State:** the counts are kept in `carried`.

Other plan choices are not carried: they belong to decision events tied to the
plan id, with AI provenance and evidence-basis hashes.

**Don't publish a name** (decision `empty`) is a secondary option. It leaves
`scientificName`, its authorship and its rank empty. The supplied text fills
`verbatimIdentification` wherever that would otherwise be empty.

The publication workflow's review (`TaxonReviewModal`) uses the same
confirmation. The serializer exposes `replacements` for the suggestion and for
each alternative. `decide` refuses such a usage without `confirm_coarser`. A name
the reviewer searched for and picked is itself an explicit choice.

**Name, rank and authorship are written together.** Whenever a decision writes
`scientificName`, it also writes the `taxonRank` and authorship that belong to
that name. Each frame row reads its supplied authorship and rank from its own
source row, so an occurrence and the identification made from it get identical
values.

| Decision | Rank | Authorship |
| --- | --- | --- |
| Parsed split | The parsed rank | The user's own authorship is kept and differences are counted; the parsed one fills a blank |
| COL name | COL's rank | COL's authorship; the supplied one survives only when COL has none for the same name |

Replaced rank values are counted per name (`ranksReplaced`, `ranks_replaced`).
Replaced authorships are counted as `authorshipReplaced`.

Regression fixtures are trimmed real v2 responses for conversions 560–570
(`back-end/api/testdata/col_v2_real_matches.json`).
