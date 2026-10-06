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

**Names keep their rank marker.** COL names come from the v2 usage `name` with
its trailing authorship removed. They do not come from `canonicalName`, which
drops "subsp."/"var."/"f." and turns "Betula pubescens subsp. czerepanovii"
into a different botanical combination. When the authorship is not a plain
suffix, as in some autonyms, the canonical name is used. The publication
workflow's matcher shares this behaviour.

**A coarser or different COL name never replaces the user's name by
default.** Matching compares the asserted name (the parsed name, or the label
without its qualifier) with each COL usage. Markers, authorship and case are
ignored. A usage *replaces* the asserted name when it differs from it and any
of these is true:

- it is a HIGHERRANK match;
- it has fewer name parts or a higher rank;
- it is in another genus.

Real cases include "Calanus" → the phylum Arthropoda, "Trientalis europaea" →
the genus Lysimachia, and "AmphibiaReptilia sp." → the class Amphibia (an
"exact" match steered there by the class hint). Genus spelling corrections such
as Pelecopis → Pelecopsis also count, because a near spelling can also be a
plant genus ("Calanus" → "Cajanus"). The same name is never a replacement,
whatever ranks the source supplies: "Larus sp." accepted as the genus Larus
keeps the user's assertion.

The server enforces these rules:

- A replacing usage is never accepted in bulk.
- A single choice of a replacing usage needs `confirm_coarser: true`.
- For such names, the suggested action is **Keep my name**.
- A COL decision saved before this check, which would replace the name and was
  never confirmed, is not applied at conversion. The review marks it to be made
  again, and the report lists it under `not_applied`.

The review lists same-name COL usages (homonyms) inline, with their
kingdom › phylum › class. It names what a replacing option would do, for
example "replaces your genus with a phylum", and asks for a second click.
**Don't publish a name** (decision `empty`) is a secondary option. It leaves
`scientificName`, its authorship and its rank empty. The supplied text fills
`verbatimIdentification` wherever that would otherwise be empty.

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
