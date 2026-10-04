# Humboldt implementation review — 3 October 2026

Follow-up: [parent and child scientific consistency](humboldt-scientific-consistency.md)
adds auditing and source evidence. Rule version 8 makes scientific findings
nonblocking and removes redundant mapping prompts; see
[streamlined review](streamlined-review.md). The sections
below record the earlier rule-version-6 integration; the benchmark JSON has been
rerun with the scientific audit.

The Claude cloud implementation has been reviewed, corrected and integrated in
this worktree. The focused backend matrix passes **225 tests**, including the
original 12 Humboldt tests, Claude's 30 hierarchy/vocabulary tests and one new
coordinator regression test. A genuine publisher Event-core archive now passes
independent relationship, cell-copy, provenance and serialized-package checks.
These results establish structural and value preservation for the tested choices;
they do not establish scientific equivalence or publisher approval.

## Implementation and review

The [Claude cloud session](https://claude.ai/code/session_01CpHnX64RXSgz5a6kB5Qtsa)
returned eight allowed files in a checksum-verified ZIP:
`5d54f94fa623b575306763750113b4753d8cfffc795287dc87b9a093a268fa50`.
The review checked parent identity resolution, forward references, conservative
failure handling, grouping prerequisites, source lineage, scope semantics and
the supplemental vocabulary catalogue. No additional worker was launched, and
no code was committed, pushed or deployed.

One issue was corrected: filtering out a preserved literal scope column could
make an originally mixed literal/IRI row appear IRI-only to the emitter. The
emitter now checks literal-scope presence in the original source row. A regression
test verifies that preserving the literal column leaves the IRI scope withheld
with the explicit pairing reason and creates no survey target.

The executable supplemental audit and checked-in JSON were independently compared
with the latest recommended property versions in the TDWG hc checkout at
`05ad2bb6e960b1028a314feaf686444b7b8373c7`. All 62 literal properties and 28 IRI
properties are accounted for: the supplemental catalogue covers exactly the seven
literal properties outside the XML and all 28 IRI properties. Its dispositions
are **1 imported, 11 reviewed imports and 23 preserved**. Targets remain pinned
to DwC-DP schema revision `76898192fd298c2aa170a7059e1bdadf3ee2a828`, rule version 6.

Claude's cloud snapshot lacked application fixtures and could not download the
publisher archive through its proxy. Its ten broader-matrix failures are recorded
in the [worker note](humboldt-implementation.md). The complete local matrix passes
in the actual worktree without its website stub or other snapshot workarounds.
Execution used the Compose backend and an isolated local Postgres test database.

## Real Event-core benchmark

Source: **Vegetation plots collected in dry grasslands throughout Bulgaria and
Romanian Dobrudzha**, [DOI 10.15468/pkx4tg](https://doi.org/10.15468/pkx4tg),
[GBIF dataset](https://www.gbif.org/dataset/c670c564-8366-4007-8d91-6f7fb6c31c2f),
[publisher archive](https://cloud.gbif.org/eca/archive.do?r=dry_grasslands_palpurina_phdthesis).
Downloaded on 3 October 2026: 333,844 bytes, SHA-256
`d45be8017a7f87234ce3b9680f13b39a45aba3725613aff808a985f1050abb64`.

| Source table | Rows | Converted outcome |
| --- | ---: | --- |
| Event core | 390 | 390 events; all 389 parent links verified against persistent source eventIDs |
| Occurrence extension | 15,669 | 15,669 occurrences and identifications; every occurrence event link checked against its source attachment |
| Humboldt extension | 390 | 390 surveys; every event link and 6,235 mapped nonempty survey cells independently checked |
| Releve extension | 181 | Unsupported rows preserved in source originals |

All source extension attachment identifiers resolve. No parent events are
fabricated. The benchmark explicitly simulates answers to 34 review issues by
selecting each issue's first allowed option. There are 24 populated columns
retained without a DwC-DP mapping; validation is not a claim that every source
field converted. No model sees archive records, and no model/API call is made.

The emitted resources are event, occurrence, identification and survey. The
serialized tar.gz passes canonical schema, resource, foreign-key, ancillary
checksum and CSV validation with no errors or warnings. Every original member
matches its source bytes, and `uploaded-archive.zip` matches the exact downloaded
ZIP. Rebuilding the plan and repeating conversion produces identical plan IDs
and CSV resources. The evidence is [humboldt-benchmark.json](humboldt-benchmark.json).
Full source and converted archives remain outside the repository.

Reproduce with the checked-in runner in the backend Compose service:

```sh
docker compose run --rm --no-deps -v /path/to/archives:/archives \
  --entrypoint python back-end scripts/benchmark_humboldt_conversion.py \
  --source-zip /archives/vegetation.zip --output-dir /archives/humboldt-results
```

The runner refuses a different source checksum. Its review decisions are recorded
in the aggregate JSON. The focused test command is in the
[conversion README](README.md).

An additional public archive, [Zwin Nature Park birds](https://doi.org/10.15468/saesvn),
was downloaded and examined. Its 980 Humboldt rows include 166 attachment IDs
absent from its 814-row Event core. The existing importer rejects that source
before conversion; join checks were not relaxed to make it pass. NEON's tick
archive endpoint returned HTTP 403 and was not benchmarked.

## Remaining limits

Rule version 7 checks supported temporal bounds, spatial circle separation and
parent/child Humboldt claims, with explicit unknown outcomes and review. Full
spatial containment and scientific equivalence remain unverified; see the
[scientific follow-up](humboldt-scientific-consistency.md). Partial hierarchies with unresolved external parents are
preserved as a whole. SurveyTarget source tables, literal/IRI pairing and 14 IRI
properties without corresponding pinned fields still need a source model or
explicit scientific policy. Scope reconstruction and supplemental IRI imports
have synthetic test coverage; this publisher benchmark does not exercise every
scope or completeness case. Live AI recommendation quality remains unevaluated.
