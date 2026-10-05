# Humboldt nested events and vocabulary gaps — implementation note

3 October 2026. Executable rule version **6** (was 5). Targets DwC-DP snapshot
`76898192fd298c2aa170a7059e1bdadf3ee2a828` and the TDWG hc vocabulary at
`05ad2bb6e960b1028a314feaf686444b7b8373c7` (term list dated 2026-05-26). This
work does not change publication, model routing, queues, schema snapshot,
models, migrations or the frontend.

Coordinator update: the [independent review](humboldt-review.md) records a
corrected mixed-scope edge case, 225 passing local tests, and the completed real
publisher benchmark. Sections 3–4 below describe the cloud worker's own run and
its environment limits; they are not the final local verification status.

The later [scientific consistency follow-up](humboldt-scientific-consistency.md)
implements rule-version-7 temporal, bounded spatial and parent/child survey
auditing. The unresolved list below records the earlier cloud implementation.

## 1. Nested Event structure

`api/dwca_hierarchy.py` is a new pure helper. It resolves supplied
`dwc:parentEventID` values against supplied `dwc:eventID` values. The converter
emits `event.parentEvent_fk` only after every core event exists, so forward
references resolve and row order does not matter.

**Identity.** A parent value is matched exactly to the persistent `dwc:eventID`
of a source Event identity. Values are not trimmed or case-folded. A DwC-A
`meta.xml` core `id` is an archive join key, so it is never a parent target. A
parent value that matches only an archive key is reported as `missing`, with a
note explaining why. If the core declares no `dwc:eventID` field, links are
unsupported and the column stays in the originals.

**Event cores** (loose `event.csv`, `meta.xml`, or ZIP, with Occurrence and
Humboldt extensions) use one identity per source row. When every supplied
relationship resolves, the plan's `parentEventID` column defaults to
`parent-link`, which needs no review. Selecting `preserve` keeps the column in
the originals, and no foreign key is emitted.

**Occurrence cores** default to `preserve` and require review. A link is
possible only when occurrence rows are combined into events by supplied
`eventID` (`event-grain = by_id`). Both the child and the parent must be such
combined events, and every row in a group must supply the same parent value.
If `parent-link` is chosen with per-row events, conversion fails with a clear
message. A `parentEventID` in an Occurrence extension describes an event, not
the occurrence, so it is offered for preservation only.

**Invalid relationships.** The helper detects these cases:

- `missing`: no event has the parent's eventID.
- `ambiguous`: duplicate persistent eventIDs; no candidate is selected.
- `self`: an event names itself as its parent.
- `cycle`: found iteratively; each cycle is reported once.
- `inconsistent`: one combined event has different parent values.
- `no-event-id`: a child row has no eventID.

A problem withholds only its own link. Cyclic edges are all withheld, while
independent links can still be emitted after review. The report lists every
supplied parent value and whether it was linked. `NA`, `N/A`, `null`, and `none`
are treated as empty references only when no source event actually has that ID;
the original text is retained. When no supplied links resolve, the plan offers
only preservation and an API caller cannot force `parent-link`.
Traversal and depth counting are iterative. Tests cover a 20,000-level chain and
a 20,000-member cycle.

**No inference.** The converter does not create parent events. It does not
infer or roll up observations, absences, scopes, completeness or categories.
Humboldt survey confirmation changes only the categories of events that have
Humboldt rows, never their parents.

**Provenance.** Plans include `event_hierarchy`, with counts, maximum depth,
problem kinds and a sample of up to 50 problems. The plan id covers it. The
report's `event_hierarchy` lists every nonempty source parent value with:

- source table and row
- file and data record
- archive join id
- eventID and parentEventID
- child event key
- linked parent key and parent source row
- status: `linked` or `retained in originals`

The column disposition is `derived` with `mapped_rows`, or `retained-unmapped`.

## 2. Current Humboldt vocabulary

The machine-readable catalogue is
[`humboldt-vocabulary-audit.json`](humboldt-vocabulary-audit.json). It is
generated from `api.dwca_humboldt.AUDIT`, and tests compare that constant with
the executable targets. It covers the seven literal properties and all 28
`ecoiri:` properties. Each entry gives a target, prerequisites, a disposition
and a reason. The totals are 1 imported, 11 reviewed-import and 23 preserved.
All of these rules apply only to Humboldt `eco:Event` extension rows. A column
is recognized only by its exact term IRI, from `meta.xml` or a full-IRI loose
header. A bare `surveyID` header stays `header:surveyID` and is preserved.

| Property | Disposition | Behaviour |
|---|---|---|
| `eco:surveyID` | imported | Copied to `survey.surveyID`. If the same value appears on several rows, the column needs review. Conversion fails if one value would identify several separate surveys; merging identical rows or preserving the column resolves this. |
| `ecoiri:` target and excluded scopes (10) | reviewed-import | Become `survey-target-descriptor.surveyTargetValueIRI` with the dimension label, but only when the original row has **no literal scope**, each cell holds one absolute IRI, and the existing completeness rules pass. Preserving a literal column does not change that original-row check. Mixed literal/IRI rows keep the existing literal conversion; the IRI is withheld and reported as "not paired". IRI lists and literals in IRI columns are not split; only preservation is offered. |
| `ecoiri:samplingPerformedBy` | reviewed-import | Copied to `survey.samplingPerformedByID`, never to the literal name field. Cells that are not a single IRI are withheld per row. |
| `eco:surveyTargetID`, `surveyTargetType`, `surveyTargetValue`, `surveyTargetUnit`, `includeOrExclude`, `isSurveyTargetFullyReported`; `ecoiri:surveyTarget{Type,Value,Unit}` | preserved | SurveyTarget records have their own grain: several descriptor rows per `surveyTargetID`, linked to surveys. They need a SurveyTarget source table model, which is neither registered nor implemented. Targets are never synthesized from them, and flags are never copied or inferred. |
| 14 other `ecoiri:` properties (units, protocols, taxa lists, types, completeness) | preserved | The pinned survey table has only literal fields for these. IRIs are not written to literal fields. An IRI unit does not satisfy the literal value/unit check, so the paired literal value is still withheld. |

The registered 57-field behaviour is unchanged: the 43 direct and 14 scope
terms, checks and review options are the same. Existing Humboldt tests pass
without modification. Supported core row types are unchanged (Event and
Occurrence only). There is no `eco:Survey` or Taxon core.

## 3. Real archive verification

**Not performed here.** No real-publisher benchmark is claimed. All tests use
synthetic fixtures.

- **NEON ticks** (GBIF `12315bb8-8ab3-446a-b5a4-2be93aade242`, DOI
  10.15468/b52b9z): the coordinator reported HTTP 403 from
  `https://biorepo.neonscience.org/portal/content/dwca/NEON-TICC-H_DwC-A.zip`.
  It was not retried here.
- **Dry grasslands of Bulgaria and Romanian Dobrudzha** (GBIF
  `c670c564-8366-4007-8d91-6f7fb6c31c2f`, DOI 10.15468/pkx4tg, publisher URL
  `https://cloud.gbif.org/eca/archive.do?r=dry_grasslands_palpurina_phdthesis`):
  the session's egress proxy denied the CONNECT to `cloud.gbif.org`
  (organization policy).
  - The coordinator holds a local copy: 333,844 bytes, SHA-256
    `d45be8017a7f87234ce3b9680f13b39a45aba3725613aff808a985f1050abb64`.
  - The coordinator reports 390 Event rows, 389 parent values, 209 distinct
    parents, 15,669 Occurrence rows, 390 Humboldt rows and 181 Releve rows.
    The Releve rows are unsupported and preserved.
  - This is the recommended first benchmark. See the reproduction steps below.
- **Zwin Nature Park birds**: the existing importer rejects it because 166
  Humboldt attachment IDs are absent from the core. The join checks were not
  relaxed, and no parents were invented.

## 4. Tests actually run

The tests ran in the repository's Compose `back-end` service, with the Compose
`db` (Postgres) service. Three workarounds were local only and were not
committed or included in the deliverable:

- **Image.** The Dockerfile's `apt-get` step is blocked by the egress proxy
  (HTTP 403 from `deb.debian.org`). I therefore built a local image with the
  same `python:3.12-slim` base and pinned `requirements.txt`, but without
  `netcat`, and tagged it `gbifnorway/chatipt-back-end:latest`.
- **Configuration.** A dummy `back-end/.env.dev` set a test-only secret key and
  disabled the background workers.
- **Website stub.** The snapshot omits the `website` app, so a stub was mounted
  through `PYTHONPATH`.

Results:

- Before the changes, `api.test_dwca_humboldt` and `api.test_dwca_conversion`:
  44 tests, OK.
- After the changes, the new `api.test_dwca_hierarchy` (16 tests) and
  `api.test_dwca_humboldt_vocabulary` (14 tests) pass, as does the unchanged
  `api.test_dwca_humboldt` (12 tests).
- The coordinator's focused matrix plus the new modules ran 224 tests, with 10
  failures or errors. All 10 also fail on the untouched snapshot: 5 for the
  missing `templates/eml.xml`, 3 in source-coverage setup for the missing
  system-message fixtures, and 2 in agent-turn tests. Every `test_dwca_*`
  module passed.
- Whole-project discovery gave the same 117 failures or errors before and after
  the changes. The only difference was the 30 added passing tests.

## 5. Reproduction

```sh
docker compose run --rm --entrypoint python back-end manage.py test \
  api.test_dwca_hierarchy api.test_dwca_humboldt_vocabulary api.test_dwca_humboldt \
  api.test_dwca_conversion api.test_dwca_media api.test_dwca_references api.test_dwca_eol \
  api.test_dwca_germplasm api.test_dwca_legacy api.test_dwca_extensions \
  api.test_dwc_dp_validation api.test_agent_turns api.test_source_coverage
```

Real archive check, with the ZIP outside the repository (paths illustrative).
The output is aggregate only; no records are printed:

```sh
docker compose run --rm --no-deps -v /path/to/archives:/archives:ro --entrypoint python back-end manage.py shell -c "
from api.dwca_import import read_inputs; from api.dwca_conversion import build_plan
a = read_inputs([('dry.zip', open('/archives/dry.zip','rb').read())]); p = build_plan(a)
print(p['event_hierarchy']); print([(c['term'], c['default']) for c in p['columns'] if 'parentEventID' in c['term']])"
```

Then review and convert the archive in the application. Compare
`report['event_hierarchy']` with the publisher's structure, not only with
validation status.

## 6. Unresolved

- Partial linking is not offered when some parents are external. Such an archive
  keeps its whole hierarchy in the originals. Offering it would need a reviewed
  policy that also excludes accidental matches against archive keys.
- Parent links are not checked for spatial or temporal containment, which TDWG
  requires. Rules for child/parent Humboldt consistency are also not
  implemented, because evaluating them needs reviewed policies.
- A SurveyTarget source table model is not implemented. The policy for pairing
  literal and IRI scopes is unresolved.
- The 14 `ecoiri:` properties without an IRI target field remain preservation-only.
- The cloud worker could not run the real benchmark; the coordinator subsequently
  completed it locally (see [independent review](humboldt-review.md)).
