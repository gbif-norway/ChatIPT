# Verification — 2 October 2026

The catalogue audit found 79 core rules and 89 extension rules. All target table/field
pairs exist in the pinned 79-table TDWG snapshot. All 38 latest production registry
identifiers are covered; there are 58 distinct families including sandbox entries.
These checks validate references and coverage, not the scientific correctness of
every proposed mapping.

The initial Docker checks passed 97 backend tests; extension integration increased
that suite to 188 before the real-archive audit. They cover conversion, media and reference
subject joins, serialized DwC-DP validation, source coverage and publication-worker
regressions. Earlier checks also passed 12 frontend utility tests, frontend lint
and production build; the later media/reference work changed no frontend code.
A Chromium browser check exercised the
separate dashboard entry, multi-file upload control, mapping review, preservation
of draft choices during AI review, explicit acceptance of a suggestion, and the
download screen. Browser API responses were controlled fixtures; backend API,
storage, ownership and queue tests used an isolated PostgreSQL test database and
local test storage. No live model or publication calls were made by these tests.

The [cloud audits](cloud-workers.md) add 383 term entries: 57 Humboldt terms and
326 terms across six production and two sandbox families. Local evidence checks
verify complete XML term coverage, source definitions/required flags, target
fields/types/annotations, and four Humboldt source hashes. The imported generator
also runs in the backend Compose container. These audits do not establish that
their proposed transformations or fixture outputs are implemented; differences
are recorded in [coordinator review](cloud-audits/coordinator-review.md).

The local Akagera loose-file benchmark was read from dataset 58's five source files.
The benchmark used explicit simulated review choices, not a claim of scientific
approval. Every converted archive passed the profile, canonical table, value,
key, relationship and serialized CSV/Frictionless checks.

| Resource | Rows |
| --- | ---: |
| event | 10,365 |
| occurrence | 10,365 |
| material | 10,365 |
| identification | 21,520 |
| resource-relationship | 9,575 |
| occurrence-assertion | 31,095 |
| nucleotide-sequence | 997 |
| molecular-protocol | 14 |
| nucleotide-analysis | 10,365 |

The 21,520 identification rows include the core classification and all 11,155
history rows. They are not merged without evidence of identity. Protocol records
share keys only for identical complete mapped descriptions. The 14 descriptions
include sample environments approved as protocol properties. The independent
archive audit confirms that preserving the three environment fields produces two
protocol descriptions: all remaining procedure fields are constant within each
marker. Both scenarios require explicit review; the two-protocol comparison is
not inherently a regression.

Combining events by supplied `eventID` failed the consistency check: for example,
`ANP_24_FECA_1` has both `30` and `20001` for coordinate uncertainty. Separate
context events keep both values and their supplied event identifiers. The final
archive therefore reports repeated weak event/material identifiers. It also
reports generic biological relationship records as candidates for future
`organism-interaction` review. None is silently promoted to an observed interaction.

The previously uploaded dataset 68 DwC-DP tables are a comparison, not a verified
expected answer. Live model recommendation quality, vocabulary conventions in the
prerelease, and the unimplemented catalogue families remain follow-up work before
a production rollout.

The independent real-archive audit adds a reproducible Compose benchmark and
aggregate evidence in [real-archive-benchmark.md](real-archive-benchmark.md).
Runtime version 5 adds numeric/boolean type and bound review, per-cell withholding,
review of float-shaped/unknown date text, and canonical loose-file ordering.
After these fixes the full backend suite passes **194 tests**. No live model calls
were used. That run exposed the 40-item, single-request AI advice limit; manual
review could still resolve all choices.

On **3 October**, successive advice requests were implemented. Each request
selects up to 40 unresolved, previously unreviewed questions, retains prior advice,
and records completed abstentions. Queue/API tests exercise more than 40 questions,
manual decisions, preservation, exhausted batches, retries, and inspection resets.
Provider responses are mocked; recommendations remain subject to user approval.

Runtime version **6** also adds [Taxon-core conversion](taxon-conversion.md).
Synthetic tests cover generic checklist packages and reviewed occurrence extraction
into DwC-DP, with additional taxonomy resources validated through Frictionless.
No real Taxon-core archive has been benchmarked. The updated backend regression
suite passes **210 tests**; **14 frontend utility tests**, frontend lint and the
production build pass. Migration state is consistent with the saved migration.
All eight existing archive scenarios were rerun under version 6 and pass again;
aggregate evidence is at `/tmp/dwca-real-archive-benchmark-rule6/aggregate.json`.
These remain Occurrence-core scenarios and do not add real Taxon/Event coverage.

On **4 October**, the reviewed Humboldt worktree and the main checkout were
combined on `main` under rule version **9**. This retains Taxon-core conversion
and successive AI advice batches alongside the streamlined review policy and
scientific hierarchy audit. Nested occurrence plans expose their automatic
choices and retain their notices and full reports with original source-table
indices. A regression fixture confirms that contradictory parent/child dates
remain reported without blocking conversion, including when the user preserves
the parent link instead of mapping it.

The full backend suite passes **676 tests**. All **16 frontend utility tests**,
frontend lint, the production build and the migration generation check pass in
Docker Compose. The real dry-grassland archive was rerun under rule 9: **2**
required choices, **389** parent links, **390** surveys and **15,669** occurrences;
all **6,235** direct survey cells and the original archive bytes are retained.
The serialized package validates without errors or warnings. Current evidence is
in [humboldt-benchmark.json](humboldt-benchmark.json). Taxon-core validation still
uses synthetic serialized fixtures; no real Taxon-core benchmark is claimed.
