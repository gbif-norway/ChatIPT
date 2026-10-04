# Parent and child survey consistency

Updated 4 October 2026. Rule version **8**, targeting the existing DwC-DP snapshot
`76898192fd298c2aa170a7059e1bdadf3ee2a828`. Codex and
[Claude](https://claude.ai/code/session_01CpHnX64RXSgz5a6kB5Qtsa) implemented and
reviewed this work in the reviewed `5581` worktree. No commits, pushes or deployment.

## Scientific policy

[TDWG's normative hierarchy guidance](https://eco.tdwg.org/hierarchy/), sections
3.2–3.3, requires spatial and temporal containment and explicit applicable scope
and completeness statements. It forbids implicit inheritance and retrospective
scope construction. The [term definitions](https://eco.tdwg.org/list/) do not make
different protocols, voucher flags, site counts or effort values automatic
parent/child contradictions: they may describe different levels or directly linked
observations. Neither observations nor effort are summed.

`api/dwca_scientific.py` audits original core Event assertions and Humboldt rows
before mapping filters. Preserving a date or scope column cannot hide an original
source inconsistency. Findings describe source claims, including unmapped fields.

| Outcome | Meaning |
| --- | --- |
| `contradiction` | Supported source bounds cannot satisfy containment. |
| `conflict-signal` | Supplied claims need interpretation; no definite contradiction is asserted. |
| `reporting-gap` | An ancestor declares an inference-related property absent from the child; applicability needs review. |
| `not-demonstrated` | Bounds permit several interpretations; containment remains unverified. |
| `incomparable` | Information is missing, invalid, ambiguous or outside supported rules. |
| `compatible` | That particular comparison found containment or no area conflict; other properties remain unverified. |

Contradictions, signals, reporting gaps and ambiguous event/survey grain produce
nonblocking notices. The converter retains supported supplied links with findings
recorded; users can optionally keep the hierarchy only in source originals.
Retention does not certify consistency or publisher approval. Structurally invalid
hierarchies remain unlinkable; a user choice cannot override them. See the
[streamlined review policy](streamlined-review.md) for decisions that still change
interpretation and require input.

## Supported checks

**Time.** ISO years, months, days, supported timestamps and closed intervals use
conservative start/end envelopes. Reduced precision means uncertainty: child
`2019` under parent `2019-06` is inconclusive. Known offsets use UTC; local dates
allow UTC+14 through UTC−12. Fractional seconds use an enclosing whole-second
range. Abbreviated calendar ends such as `2007-11-13/15` are supported. Reversed,
invalid and unsupported dates are incomparable, never repaired. Separate year,
day-of-year and eventTime fields do not construct intervals.

Every link compares the nearest supplied ancestor date. An iterative accumulation
of the strongest bounds also detects conflicts with more distant ancestors, even
behind a reduced-precision or invalid intermediate date. Invalid dates supply no
bound and retain their own incomparable outcome. Traversal stays linear through
20,000 levels. Only comparison constraints travel; emitted values do not. Evidence
identifies the actual ancestor. A publisher may have recorded only a campaign's
start as eventDate; findings flag that uncertainty without expanding the source interval.

**Space.** Contradictions require valid positive point-radius uncertainties on
both events, supported WGS84 aliases and disjoint enclosing circles. A child
representative coordinate outside a parent circle is insufficient: it need not
lie within the actual Location. Haversine comparisons allow 1% of distance plus
5 metres, plus conservative rounding allowances from supplied decimal precision
and any larger valid coordinatePrecision. Only comparison tolerance changes.
Overlapping circles, even one within another, do not prove containment.

Footprints, generalized/withheld locations, invalid datum/radius/precision and
conflicting grouped location assertions make comparisons incomparable. Common
coordinates and conflicting source values remain visible. Polygon, elevation,
depth and country-boundary checks are not implemented. Only the nearest ancestor
with supplied coordinates is examined for spatial separation.

**Surveys.** The nearest ancestor with Humboldt rows is compared. Identical rows
compare once with all row references; distinct survey rows at either event are
incomparable and remain reported without blocking conversion. Exact child target tokens excluded by an ancestor
are signals, not taxonomic proofs. Different names or protocols do not trigger
inferred taxonomy or equivalence.

Missing ancestor-declared scope/completeness properties produce reporting gaps.
A literal/IRI counterpart is an incomparable representation rather than an absent
scope. They are never paired by assumption. Stronger ancestor completeness and
weaker child completeness are signals only when the whole explicit scope matches
as exact token sets. Empty scopes do not establish a shared scope. Parent
incomplete / child complete is permitted.

Valid child scope or sampled area exceeding ancestor geospatial scope area in the
same exact unit is a signal. Numeric values must meet pinned types/bounds. Units
are not converted, sampled areas are not compared with each other, and counts,
effort, durations and boolean properties are not rolled up.

## Review, provenance and limits

Plans contain bounded counts and up to ten finding samples in scientific_hierarchy,
included in their deterministic ID. Changed sources/rules invalidate decisions.
Reports contain all checks in event_hierarchy.scientific_consistency, with exact
evidence, child/direct-parent/actual-ancestor identities, survey table/row references
and the parent-link disposition. The events ledger stores every node's file/record lineage
once; checks reference its indices to avoid repeating large occurrence groups.
The interface separates scientific results from package validation.

Other extension-supplied event dates/locations (e.g. Germplasm Trial), derived
values, taxonomic reasoning, unsupported dates/geometries, occurrence-based
absence checks and full scientific equivalence remain outside the audit. Unknown
outcomes are explicit. No absence, inherited scope or inferred completeness is
created.

## Verification and collaboration

The rule-version-8 Docker backend matrix passes **275 tests**, including source filtering,
retained hierarchy provenance, scope flags, ambiguous grouping,
invalid values, remote ancestors, spatial rounding/tolerance, 20,000-level
traversal, deterministic output and serialized package checks. Frontend lint and
the production build pass in Compose.

The pinned public dry-grasslands benchmark still emits 390 events, 390 surveys and
15,669 occurrences/identifications. All 389 parent links and 6,235 mapped survey
cells are independently verified; original bytes and repeated CSV outputs remain
identical. The serialized package validates without errors or warnings.
[Aggregate evidence](humboldt-benchmark.json) records simulated review choices,
not publisher approval. Its 1,556 scientific outcomes are 416 compatible,
736 incomparable and 404 not-demonstrated, with no conflict findings. Those unknown
results do not establish scientific consistency.

Claude researched the policy and delivered the helper and its unit tests in a
9,034-byte ZIP, SHA-256
`e36cb2206a137c0c6a469cbfb23e6fdebc9ab10b05866c11b9da356a3280af46`.
Codex verified the handoff, integrated review/report/UI behavior, corrected invalid
offsets, empty scopes, numeric bounds and row ordering, and added ancestry,
tolerance and evidence fixes after Claude's independent integration review.
Local verification uses the real Compose image and an isolated Postgres test
database, without the cloud worker's fixture stubs.
Claude also audited the rule-version-8 review triggers, challenging quantity,
date, partial-coordinate, name and subject assumptions. The publisher benchmark
now needs two simulated choices instead of 34, with the same verified source
values and relationships.
