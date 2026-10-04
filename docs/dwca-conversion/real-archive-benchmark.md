# Local archive benchmark — 2 October 2026

Update, 3 October: the separate [Humboldt review and benchmark](humboldt-review.md)
adds a real Event-core publisher archive: 390 events, 389 verified parent links,
15,669 occurrences and 390 surveys. The original Occurrence-core scenarios and
results below retain their original scope and date.

This independent offline audit uses the pinned TDWG `1.0_DEV` snapshot at
`76898192fd298c2aa170a7059e1bdadf3ee2a828`. It first measured executable rule
version 3, verified the numeric-field repair in version 4, and repeats the final
checks against version 5. Review answers are explicit benchmark simulations,
not publisher approval or evidence of scientific equivalence.

The reproducible runner is
[`back-end/scripts/benchmark_dwca_conversion.py`](../../back-end/scripts/benchmark_dwca_conversion.py).
The final aggregate evidence is
[`real-archive-benchmark.json`](real-archive-benchmark.json).
It imports local files, rebuilds plans, repeats conversions, verifies direct cell
copies against source lineage, checks source joins and counts, and validates actual
serialized tar.gz packages. It makes no model/API calls and performs no database
writes. Full converted packages and their embedded source reports stay in `/tmp`;
this report contains aggregate findings and anonymized reproductions only.

## Archive coverage

All four source datasets below have **Occurrence cores**. The eight benchmark
scenarios reuse these sources with different upload or review choices; they are
not eight independently sourced archives. Event resources produced from these
Occurrence cores do not establish Event-core import coverage.

| Local source | Evidence | Parsed core rows | Review items, version 3 | Final review items | Outcome |
| --- | --- | ---: | ---: | ---: | --- |
| Akagera, five `377__*.csv` files | Real local source tables, loose layout | 10,365 | 31 | 31 | Valid serialized package |
| `gaynor_et_al_v3` | Real local meta.xml archive | 31 | 9 | 12 | Version 3 blocked; final package valid |
| `bigtree` | Additional local meta.xml archive; provenance not independently verified | 9,524 | 9 | 9 | Valid serialized package |
| `newick` | Synthetic fixture for anomaly handling | 9,833 | 6 | 7 | Valid serialized package; date/name anomalies require review |

Gaynor is also wrapped as a ZIP from the same local source bytes to exercise exact
upload-byte preservation. That wrapper is a test construction, not an independently
downloaded publisher archive. No other ZIP was found in the targeted local archive
locations. Bigtree was discovered through the local meta.xml inventory.

Core coverage across the converter's tests and this benchmark is:

| Source core | Application test coverage | Local archive benchmark coverage |
| --- | --- | --- |
| Occurrence | Synthetic parser, conversion, API and serialized-package tests | Akagera, Gaynor, Bigtree and synthetic Newick |
| Event | Synthetic conversion tests for Occurrence/eMoF joins, Humboldt surveys, identifiers/references, germplasm trials and conflict handling; survey and trial packages are serialized and validated | Dry-grassland publisher archive, verified separately on 3 October: [Humboldt benchmark](humboldt-review.md) |
| Taxon | Added on 3 October: synthetic checklist-package and reviewed Occurrence-extraction tests; see [Taxon-core conversion](taxon-conversion.md) | No real Taxon-core archive benchmarked |

Event-core examples are in
[`test_dwca_conversion.py`](../../back-end/api/test_dwca_conversion.py),
[`test_dwca_humboldt.py`](../../back-end/api/test_dwca_humboldt.py),
[`test_dwca_references.py`](../../back-end/api/test_dwca_references.py) and
[`test_dwca_extensions.py`](../../back-end/api/test_dwca_extensions.py).
Taxon fields on an occurrence's identification are a different case from a
Taxon-core checklist. The pinned target has no standalone taxon/name-usage table;
the [3 October implementation](taxon-conversion.md) preserves additional taxonomy
tables and can extract approved actual Occurrence extensions. The outcomes above
describe the 2 October archive benchmark, not a real Taxon-core benchmark.

For every successful case, the source-originals ZIP contains precisely the uploaded
members with identical bytes; every report file checksum matches the source; every
converted target row has a valid source-record crosswalk; repeated plans and CSV
resources agree; and the serialized archive passes descriptor, canonical schema,
resource, foreign-key, ancillary checksum and CSV value validation. Gaynor's ZIP
case additionally preserves the exact uploaded ZIP bytes. Both local EML documents
declare EML 2.1.1, so they correctly remain in originals rather than becoming
invalid EML 2.2.0 package metadata. No replacement author/title or phylogeny is
invented.

## Akagera evidence and review scenarios

CSV parsing yields 10,365 occurrence records from 10,520 physical lines including
the header. Physical line counts would overstate records because quoted remarks
contain newlines. The occurrence identifiers are unique and nonempty. DNA has the
same identifier set; all extension join identifiers resolve to the core.

| Source table | Parsed records | Important source checks |
| --- | ---: | --- |
| Occurrence | 10,365 | 790 event IDs and 790 material IDs; event-to-material cardinality is 1:1 |
| eMoF | 31,095 | Three types, each occurs 10,365 times; all 2,370 sample/type groups have consistent values |
| ResourceRelationship | 9,575 | Every endpoint resolves; every pair shares source event and material IDs |
| IdentificationHistory | 11,155 | Unique identification IDs; 10,365 DNA and 790 field rows |
| DNA-derived data | 10,365 | All 1,570 sample/marker denominators are constant and equal summed source read quantities |

Relationships cover 788 of the 790 samples. The remaining two samples each have a
single occurrence and need no relationship edge. No sample has multiple
relationship subjects. Of the related occurrences, 9,423 have kingdom Plantae and
152 Animalia. This is a source-predicate review signal; the converter preserves
generic relationships and never promotes them to observed interactions.

The all-default-mappings simulation explicitly approves physical material,
extension meanings and the reviewed mappings, while retaining separate event
contexts. It produces:

| Resource | Rows |
| --- | ---: |
| event | 10,365 |
| occurrence | 10,365 |
| material | 10,365 |
| identification | 21,520 |
| occurrence-assertion | 31,095 |
| resource-relationship | 9,575 |
| nucleotide-sequence | 997 |
| molecular-protocol | 14 |
| nucleotide-analysis | 10,365 |

The 124,380 crosswalk entries trace every output record. Independent exact-value
checks verify 684,999 mapped nonempty source cells, with no unmatched copies.
All 82 source columns have dispositions. Three nonempty unmapped columns retain
31,095 cells only in originals; one additional unmapped column is empty. Supplied
classifications plus all history rows remain separate; accepted-identification
flags and organism/interaction resources are not invented.

Source coordinate uncertainty disagrees within **788 of 790 events**. Choosing
event grouping with the original approved mappings correctly fails. Separate
contexts preserve every mapped uncertainty rather than selecting or concatenating
values.

Two additional explicit review scenarios clarify the modeling consequences:

- Preserving the three sample-environment fields instead of mapping them to
  `molecular-protocol.env_*` gives **two protocols**. The other twelve variants in
  the first scenario arise entirely from sample environments, not different source
  PCR/marker protocol descriptions. Correct placement as material-level environment
  assertions is a research proposal that remains unimplemented.
- Additionally preserving the entire conflicting coordinate-uncertainty column,
  then choosing event/material grouping by supplied IDs, gives **790 events,
  790 materials and two protocols**. Every uncertainty remains in originals;
  none is selected as the representative value. This deliberate withholding is
  required for that simulation and is not automatic scientific approval.

The grouped scenario uses permitted per-column decisions programmatically; this
benchmark does not establish that every automatic-column override is exposed by
the current review UI. All eight final scenarios pass serialized validation,
direct-copy checks, lineage/join checks and every reported copied-count check.

The converter does not yet implement read-count reinterpretation,
sample-level assertion deduplication or inferred material-to-analysis links. Those
research expectations must not be treated as failing runtime requirements.
`nucleotide-analysis.event_fk` is populated with the reviewed linked event context
for all 10,365 analyses, as declared by the molecular table option. The source
contains no distinct laboratory analysis event. Publisher review must decide
whether that contextual link is appropriate; the research policy proposes leaving
it empty. Structural validation cannot settle that semantic question.

## Concrete bugs and verified repairs

### Numeric null-like values blocked the real Gaynor archive

Version 3 maps `decimalLatitude` and `decimalLongitude` automatically despite one
record supplying literal `NA` in both columns. Numeric validation rejects the
output, while the issue-only review UI has no choices for those automatic columns.

Minimal anonymized loose input:

```csv
occurrenceID,decimalLatitude,decimalLongitude,occurrenceStatus
example,NA,NA,present
```

Expected: show a preservation/compatible-cell-copy decision before export and
retain original values; do not assume `NA` means null. Actual version 3: only
layout/grain issues are shown, then two numeric validation errors block export.
The cause was the review predicate in `build_plan`, which did not inspect target
types. Version 4 adds target lexical/type/bound checks and per-cell withholding in
`values`, with column counts and reasons in the conversion report. See
[`dwca_conversion.py`](../../back-end/api/dwca_conversion.py).

The repaired Gaynor run adds two coordinate reviews, retains all 31 occurrence and
event rows, copies **30 latitude and 30 longitude values**, withholds the two
literal `NA` cells with reasons, and passes actual tar.gz validation. Reported
30-copied/1-retained-per-column counts agree with independent source-lineage checks.
Original malformed study/BioProject IRIs and dynamic JSON stay in originals, and
string-valued `NA` tokens remain literal values unless explicitly preserved.

### Equivalent loose inputs had order-dependent identifiers

Version 4 reproducer: supply these two files, then reverse only their upload order.

```csv
# occurrence.csv
occurrenceID,occurrenceStatus
example,present

# identificationhistory.csv
occurrenceID,scientificName
example,Apus apus
```

Expected: content-equivalent uploads have the same plan and internal UUIDs, or the
identity contract explicitly records order. Actual: the archive fingerprint is
equal, but plan IDs and occurrence UUIDs differ. The fingerprint sorted files,
while `_loose_tables` preserved upload/ZIP entry order and `_key` encoded positional
table indices. See [`dwca_import.py`](../../back-end/api/dwca_import.py) and
[`dwca_conversion.py`](../../back-end/api/dwca_conversion.py).

Version 5 canonicalizes loose table order with the core first. The final reversed
upload-order probe confirms equal source fingerprints, plan IDs and occurrence
UUIDs. Byte-for-byte tar.gz identity is
outside this audit; deterministic plan IDs and CSV/UUID content are the contract
being checked.

### String dates could conceal source anomalies

The synthetic Newick table contains **3,756 float-shaped years** and **9,830 names
with repeated genus words**. Names already trigger a column review; dates passed
structural validation because the pinned `eventDate` field is a string. Version 5
adds an explicit date review for float-shaped years and supplied null-like tokens.
An approved date stays verbatim; preservation remains an explicit choice. No date
or name is repaired by inference. Gaynor's one literal `NA` date also gains that
review. This fixes review visibility, not the underlying synthetic source data.

## Review experience

Akagera needs 31 decisions: 24 column decisions, four extension meanings and three
layout/grain/material choices. Four items have one option. `scientificName`,
`identifiedBy` and `identificationRemarks` each occur twice as indistinguishable
titles for different tables. The UI already displays each source filename.
Environment review titles are numeric ontology suffixes (`0000012`, `0000013`,
`0000014`); add registry labels alongside these terms, and present
preservation-only items as an acknowledged summary rather than unexplained
repeated choices.

All audited real/local cases fit the first 40 AI issues. A separately labeled
synthetic NBN fixture with 50 sensitive rows generates **53 issues**, leaving
**13 outside the initial advice window**. The existing `suggest_mappings` selection
took the first 40 issues, including already resolved ones; the UI/API prevented
another request once suggestions existed. This made advice incomplete for larger
row-review archives. **Fixed on 3 October:** subsequent requests target unresolved,
previously unreviewed questions, retain earlier advice, and show progress. Completed
abstentions require manual review; failed requests can be retried. See
[`conversion_jobs.py`](../../back-end/api/conversion_jobs.py).

Ordinary columns still expose `nonempty` and disposition without explicit
`mapped_rows`/`retained_only_rows`; the repaired typed columns and specialized
extension rules expose those counts. The script independently verifies copied
cells and checks every count that is reported. The report/UI should show copied,
withheld and original-only counts consistently, and keep original-only data
visible when saying that conversion succeeded.

The prior uploaded DwC-DP tables are an **aggregate comparison only**:
790 events/materials, 11,155 identifications, 4,740 material assertions, two
protocols, 10,365 analyses/occurrences, 997 sequences and 9,575 relationships.
Their different identification grain, assertion subjects, protocol environments
and event uncertainty decisions explain structural differences. This audit does
not compare every prior cell, endorse prior inferred values, or treat those tables
as a verified expected answer.

## Reproduction and limits

Run from the repository root. The entrypoint override avoids starting Django web
services or background publication/model workers; this pure conversion audit does
not require a database service.

```sh
docker compose run --rm --no-deps \
  -v /tmp/dwca-real-archive-benchmark-final:/benchmark \
  --entrypoint python back-end \
  scripts/benchmark_dwca_conversion.py --output /benchmark
```

To rerun only Gaynor and its numeric-coordinate regression:

```sh
docker compose run --rm --no-deps \
  -v /tmp/dwca-real-archive-benchmark-gaynor:/benchmark \
  --entrypoint python back-end \
  scripts/benchmark_dwca_conversion.py --output /benchmark --cases gaynor
```

The default run writes aggregate JSON plus full successful tar.gz files under the
mounted `/tmp` directory. Earlier aggregate evidence is retained at
`/tmp/dwca-real-archive-benchmark/aggregate-rule3.json` and
`/tmp/dwca-real-archive-benchmark-rule4/aggregate.json` on the audit machine.

Coverage is strongest for real Occurrence cores, identification, eMoF,
relationships and DNA. No locally verified real Event/Humboldt, media/reference,
germplasm, BMDE or NBN archive was available; application integration fixtures are
synthetic evidence for those paths. The synthetic NBN test above quantifies review
burden, not family-wide semantic correctness. No live-model recommendation quality,
external GBIF validator, publication, deployment, account upload, or independent
publisher approval was tested. Existing research fixture expectations remain
proposals unless they match declared executable rules.
