# Claude cloud workers — 2 October 2026

Three Claude Code cloud sessions were launched from a private, 11.7 MB source
snapshot containing converter code, the pinned 79-table schema, mapping proposals
and public registry XMLs. The snapshot has no GitHub remote. It contains no .env
files, credentials or uploaded user datasets. Workers were instructed not to push,
publish, deploy, contact anyone or create further workers.

| Task | Session | Completed deliverable |
| --- | --- | --- |
| Humboldt field audit | [Humboldt worker](https://claude.ai/code/session_013n7fXXVdR6HjnzbYPim7h6) | [57 terms, conditions and two fixture archives](cloud-audits/humboldt.md) |
| EOL/germplasm field audits | [Remaining field audits](https://claude.ai/code/session_01E537hc4M4GGJGeprxsdeEE) | [326 terms across six production and two sandbox families](cloud-audits/remaining-fields.md) |
| Identifier/reference implementation | [References worker](https://claude.ai/code/session_01SziS8B3ApfVhr3rQxso2Xd) | [Helpers, tests and integration notes](cloud-audits/references.md); integrated into converter |

All sessions finished. Deliverables were retrieved through the Claude CLI and
reviewed locally. The two audit archives were checksum-verified before extraction:

- Humboldt ZIP: 33,753 bytes, SHA-256 `fb2082ed7a7a3055f0b358133f0870e3efd53af8586de1b6e98a902931d2a2cd`.
- Remaining-field ZIP: 64,893 bytes, SHA-256 `25cd658d1aa0a6b0f9625acaea390faefb55a89e5ceff314cc0d999656b69e70`.

Local checks confirm complete XML term coverage and target annotations/types for
all 383 term entries. Four Humboldt source hashes also match the downloaded
registry. The reference helper's focused tests, root integration tests and media
implementation pass in the backend Compose container: **97 backend tests** across
conversion, canonical validation, job processing and source coverage.

Application tests could not run in the cloud snapshots because their Compose
configuration references an omitted `.env.dev`. The remaining-field generator
did run in a plain Python Docker container. Audit fixture expectations remain
proposals, not successful conversions. See [coordinator review](cloud-audits/coordinator-review.md)
for the runtime differences and verification command. The generator was adapted
to accept mounted registry/schema paths after importing it into this checkout.

Local snapshot: `/tmp/chatipt-cloud-audit-20261002`.

## Implementation workers

Three further sessions used a private 12.5 MB source snapshot to implement bounded
helpers and focused tests. The same exclusions and no-publication instructions
applied. All sessions finished, and their ZIP deliverables were retrieved with the
Claude CLI and checksum-verified before integration. Humboldt was implemented
locally alongside the workers.

| Task | Session | Deliverable |
| --- | --- | --- |
| EOL media/references | [EOL implementation](https://claude.ai/code/session_01F9JzoBkiErAjEB5EJXSnHq) | [Helper and tests](cloud-audits/eol-implementation.md) |
| Four germplasm families | [Germplasm implementation](https://claude.ai/code/session_01LMJVK3dubbSBDAw3s44vBs) | [Helper and tests](cloud-audits/germplasm-implementation.md) |
| BMDE/NBN | [Legacy implementation](https://claude.ai/code/session_01KBPJpttaoU35apx5NVmnLF) | [Helper and tests](cloud-audits/legacy-implementation.md) |

- EOL ZIP: 9,183 bytes, SHA-256 `87d76bbf557ac42c55099b5b7d7362674739c233ad7bb3724080d2936f532c45`.
- Germplasm ZIP: 10,849 bytes, SHA-256 `60a802e6261f5c22b89229fcb692f0ff51882f50baee8df5fb911d1d91310b8a`.
- Legacy ZIP: 11,623 bytes, SHA-256 `8ff0ec57fbbc65f2f52b14f717e8cf57374868a24ff409a7cc763843bfc2776f`.

Worker notes retain their original handoff context. Local integration adds closed
review choices, material identifier matching, unique trait protocol resolution,
event conflict checks, precise column dispositions and serialized download tests.
Score types use `verbatimAssertionType`, consistent with existing MoF/BMDE rules.
BMDE times with subminute precision are preserved rather than rounded. No live
OpenAI suggestion calls were made during verification.

Local snapshot: `/tmp/chatipt-cloud-implementation-20261002`.

Final local verification: **188 backend tests**, **12 frontend utility tests**,
frontend lint and the production build passed in Docker Compose. Serialized
Humboldt, Trial and combined occurrence-extension downloads validate against the
pinned schemas. Changes remain local; no production deployment was performed.
