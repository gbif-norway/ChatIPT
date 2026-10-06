# Tiered review: deterministic, AI reviewer, chat fallback

Status: implemented in rule version 10 (`api/dwca_review.py`, `api/dwca_preflight.py`). This document is the interface contract
between the deterministic plan (steps 1–3) and the AI reviewer / conversation
agent (steps 4–5). Steps 4–5 have their own design document,
[ai-review-and-chat.md](ai-review-and-chat.md), which must conform to this one.

## Principles

1. Deterministic rules resolve everything that does not change meaning.
2. An AI reviewer may settle *interpretations of supplied source data* when the
   policy allows it, with recorded evidence; every AI decision stays overridable.
3. Options that *assert a new fact* not present in the source (absence,
   completeness, survey classification, what media depicts, that an identifier
   denotes physical material, filling a missing category, splitting a repeated
   identity, a loose-file layout) are never applied by AI alone. They need a
   user answer, ideally a plain-language question in the conversation with the
   AI's recommendation and cited evidence.
4. `validate_decisions` remains the only gate for every decision source
   (form, AI reviewer, chat agent). Model output never executes transformations.
5. Every check is evaluated against the *effective decision set*
   (automatic defaults, group expansions, explicit choices), never against
   default mappings alone.

## Step 1 — issue kinds and per-option authority

Every entry in `plan.issues` and `plan.automatic_choices` gets a `kind` from a
closed set. Every option gets `assertion: true|false` from
`ISSUE_POLICY[kind]` in `dwca_conversion.py`; never inferred by the model.
`preserve` is never an assertion. The issue's `authority` is derived:
`"user-assertion"` when every non-preserve option is an assertion, otherwise
`"ai-reviewable"` (the AI may apply only non-assertion options).

| kind | ids | assertion options |
| --- | --- | --- |
| `layout` | `loose-links` (both cores) | `confirm` |
| `taxonomy-package` | `taxonomy-package` | none (single structural confirmation) |
| `event-grain` | `event-grain` | `per_row` when supplied eventIDs repeat (splits an identity); `by_id_depth` (splits an event by depth); `by_id` is not |
| `event-category` | `event-category` | all |
| `occurrence-status` | `status:t` | all (specimen records without any status get `present` as a changeable automatic `convention`, [review policy §6.1](review-policy.md#61-who-may-decide)) |
| `extension-role` | `table:t` | `media-occurrence`, `media-event` (what media depicts), `humboldt-*` survey roles are not assertions because `hum-category` carries that |
| `taxon-occurrences` | Taxon `table:t` | `convert` (asserts rows are actual occurrences) |
| `material-identity` | `material:t` | `per_row`, `by_id` |
| `column-mapping` | `column:t:c`, `country-label:t:c:sha`, `age-remark:t:c:sha` | none (routing the supplier's own label or remark is an interpretation) |
| `name-semantics` | `column:*` scientificName ambiguity | none (choice between copying and preserving supplied text) |
| `external-identifier` | Taxon `taxonID` | the copy option (asserts external meaning) |
| `row-handling` | `row:t:n`, `row-group:t:k` | none |
| `trait-link` | `trait-link:t` | none (exact identifier match) |
| `survey-classification` | `hum-category:t` | `confirm` (fills categories); `require` is not |
| `survey-completeness` | `hum-scope:t:n`, `hum-scope-group:t:k` | `reported-true`, `reported-false` |
| `occurrence-events` | `occurrence-events:t` (rule 11) | `per-row` (each occurrence row becomes its own child event) |

Loose files use the core identifier as their archive link; `meta.xml` archives have separate join keys. In either layout, a common missing-value token in an extension link is ambiguous even if the core contains the same token, so it is treated as unlinked. The user must correct the source or explicitly leave those extension rows in the originals before conversion.

Nested Taxon-core occurrence issues keep the inner issue's kind and option flags.

## Step 2 — preflight instead of convert-time surprises

### 2a. Option requirements evaluated against effective decisions

`build_plan` precomputes archive evidence once and records, for options whose
validity depends on the data, `requirements`: a closed list of plan-only
conditions evaluated by `option_status(plan, decisions)` without reloading the
archive:

- `{"type": "target_in" | "target_not_in", "column": id, "targets": [...], "prefixes": [...]}`
  — the column's effective target (a preserved table or material makes it
  `preserve`) is, or is not, one of these targets or prefixes.
- `{"type": "decision_in", "id": ..., "values": [...]}`
- `{"type": "any", "conditions": [...]}`
- `{"type": "unsatisfiable"}`. When no permitted choice can make an option valid,
  the option is moved to `unavailable_options` at plan time instead.

A requirement is `{conditions, reason, evidence?, when?}`: all conditions must hold
whenever every `when` condition holds (for example, conflicts that apply only
when a particular column is the material identifier). Requirements are stored
in `plan.requirements[decision_id][value]`.

Each requirement has `reason` and bounded `evidence` (counts plus up to five
examples with source rows). Typed values use the same filtering as `convert()`
(withheld invalid cells do not count as conflicts).

| decision / option | precomputed evidence |
| --- | --- |
| `event-grain=by_id` | Requires the core eventID column mapped to `event.eventID` (values are nonempty and not common missing-value tokens, otherwise unsatisfiable). Per core column with an `event.*` option: eventID groups whose copied values disagree. `by_id_depth` has the same evidence except that depth fields may differ, and requires a depth column mapped to an event depth field. |
| `material:t=by_id` | Requires the identifier column mapped to `material.materialEntityID`; missing or placeholder material identifiers are unsatisfiable; per column with a `material.*` option: identifier groups that disagree. Collection events are compared by their actual event keys: on an Occurrence core, identifier groups spanning several rows require `event-grain=by_id` (or `by_id_depth` when no group spans source depths) and groups spanning distinct eventIDs are unsatisfiable; on an Event-core Occurrence extension, groups spanning several core events are unsatisfiable (no `event-grain` decision exists). |
| `table:t=humboldt-merge` / `humboldt-grouped` | Non-identical source rows per event (unsatisfiable); grouped coverage gaps (unsatisfiable); `humboldt-grouped` requires `event-grain=by_id`. |
| Humboldt surveyID | For every Humboldt role: emitted surveys are computed per role (separate rows, merged per event, grouped per eventID) across all Humboldt tables; a supplied surveyID shared by more than one emitted survey requires the surveyID column preserved, or a merging role that makes it one survey. |
| `table:t=occurrence-assertion` (Occurrence core) | When any assertion supplies a usable occurrenceID: requires the core occurrenceID column mapped, and each supplied ID must identify exactly one converted occurrence in the whole archive (global uniqueness, as `convert()` requires) and that occurrence must be its own core row. Empty cells and common missing-value tokens use the archive core attachment without requiring a mapped occurrenceID. If real supplied IDs cannot resolve, choosing an event subject requires review. |
| `table:t=declared-assertions` (Event core) | Occurrence IDs come from converted Occurrence extensions: requires an Occurrence extension with role `occurrence` and its occurrenceID column mapped; each usable assertion occurrenceID must identify exactly one converted occurrence in the whole archive (global uniqueness, as `convert()` already requires), and that occurrence must belong to the attached event. A common missing-value token that also appears as a source occurrenceID leaves the subject ambiguous, so this option is withheld and event-level interpretation requires review. Preflight and conversion use this same rule. |
| `trait-link:t=exact` | Unmatched or duplicate trait IDs (unsatisfiable); requires the Trait table converted. |
| germplasm material / score-material | Requires `material:*` not preserved and a mapped material identifier; unmatched germplasmIDs. |
| `table:t=media-*` | Rows without any mappable media value. |
| `table:t=molecular` | Rows without a sequence (requires the sequence column mapped). |
| parent link / `humboldt-grouped` | Parent links require `event-grain` `by_id` or `by_id_depth` (links join the eventID events, never depth children); `humboldt-grouped` requires `by_id` (currently raised in `convert`). |
| NBN date columns | Must be approved or preserved together. |
| NBN/BMDE event context (`table:t=nbn-context` / `bmde-context`, its `derive` and `event.*` columns, core `event.*` columns, `event-grain`) | Each converted row's event values (derived vague dates, UTM coordinates, observation times, or directly mapped event fields) must equal any value the core supplies for the same field on that row's event, and a derived eventDate must contain a mapped core year. Extension rows reaching the same event must agree: rows of one core row always, rows of one eventID when `event-grain` combines events (`by_id`, `by_id_depth`). Each conflict is a requirement on every choice that applies it, conditioned on the others and on the rows' own row decisions. |

`validate_decisions` checks every *active effective* choice — explicit choices,
automatic defaults and group-expanded member choices — and ignores choices under a
preserved table or row. A failing requirement raises a structured error (below),
so an unsubmitted default can never fail later in `convert()`. The conversion
state returns `option_status: {decision_id: {value: {available, reasons}}}` for
issues, automatic choices and columns (parent links and NBN columns are columns).
The former `event_grouping_evidence` is replaced by the `event-grain`
requirement evidence.

### 2b. Plan-only decision checks

Two columns of one table effectively mapped to the same target field are rejected
when they supply different copied values in any row (the conversion rule; equal
or one-sided values are combined as before). This is precomputed as a pair of
`target_not_in` requirements. Group checks (event and material combining) compare
the *combined* value of every subset of columns that can map to a field, so sparse
complementary columns are not mistaken for conflicts. Duplicate term IRIs within
one table are already rejected on import, so `_source` duplicate-term conflicts
cannot arise from valid inputs.

Some combinations stay convert-time `conflict`s because their validity depends on
several tables at once: surveyIDs shared across Humboldt tables and assertion
occurrenceIDs found in more than one Occurrence extension.

### 2c. Failure categories

`ImportFailure` gains `category`, `decision_ids` and `evidence`
(`ConversionError` subclass; plain `ImportFailure` defaults to `source`).

| category | examples (current `dwca_conversion.py` lines) | handling |
| --- | --- | --- |
| `stale-plan` | 752 | 409; re-inspect. |
| `decision` | 716, 719, 932, 982, 1044, duplicate targets | Rejected by `validate_decisions` before queuing. |
| `conflict` | 989 (scope decisions differ within a merge), event patch conflicts 834/838/841 with row evidence, emitter conflicts in germplasm/legacy | Returned to review only when `decision_ids` contain a real remedy. |
| `source` | malformed inputs | Status `blocked`: the source must change; no retry offered. A `conflict` or `decision` failure without any remedying decision is also `blocked`. |
| `internal` | 831 and other invariants | Status `failed`, logged, not retryable (a code defect). |
| `transient` | storage/network | Status `failed` with retry. |

The job stores structured failures in `conversion.conflicts`:
`[{id, category, reason, decision_ids, evidence}]`.

## Step 3 — grouped row decisions

- Groups form among row issues of one kind in one table with identical
  option values+labels and identical reason. `hum-scope` rows additionally group
  only when their supplied scope-term values are identical, so one completeness
  assertion never covers different targets or flags.
- Group ids `row-group:t:k` / `hum-scope-group:t:k`; `k` numbers groups in order
  of their first source row. Computed only from source and rule data, never from
  decisions; included in the plan hash. Nested Taxon plans prefix them like other ids.
- A group has `members` (member issue ids), `rows` (1-based), `count`, up to five
  sample rows and, for scope, the shared scope values. Member issues are kept in
  `plan.row_issues` (with their reasons) so conversion and reports keep row-level
  detail. A single-member group is not wrapped.
- `effective_decisions(plan, decisions)`: automatic defaults → group choices
  expanded to members → explicit choices (explicit member choice wins). A group
  is resolved when every member has an effective choice. Member ids remain valid
  decision keys; the UI lists member exceptions under the group.
- Preserve-only groups become one automatic choice and one notice per group.

## Persistence and states

- `action: "save"` accepts any offered option (structural validation); requirement
  violations do not reject a save but appear in `unresolved` and `option_status`,
  so a user can change dependent choices in any order. `convert` rejects them.
  It persists decisions,
  clears `conflicts` whose `decision_ids` intersect the changed ids *after group
  expansion* (a group change affects all its members), and returns
  the state. No job is queued. The form saves on each change.
- Every state response includes `unresolved`, `option_status` and `conflicts`.
- Statuses: `queued`, `inspecting`, `review`, `reviewing`, `converting`,
  `complete`, `blocked` (source must change), `failed` (with `retryable` true for
  transient and false for internal failures). A transient failure after a plan
  exists (for example storage while saving output) returns to `review` with
  `retryable` true, since converting again is the retry.

## Contract for steps 4–5

Steps 1–3 provide: issue `kind`, option `assertion` flags and derived
`authority`; group `members`/`rows`/evidence; `option_status`; structured
`conflicts`; `effective_decisions`, `option_status` and `validate_decisions` as
pure functions of plan + decisions; the `save` action and statuses above.

Steps 4–5 must specify in their own document (agreed with Codex): evidence tool
inputs and bounded outputs, EML extraction, recommendation/abstention schema,
escalation rules derived from `authority`/`assertion`, a decision provenance log
(user / ai-reviewer / chat, with model, evidence and plan id), plan-id checks
when saving any AI or chat answer, retry/idempotency, invalidation of earlier
recommendations by later decisions or conflicts, the chat entry point and how it
connects to the conversion job queue (the publication `agent_turns` path
currently skips conversion datasets), user-answer persistence, and state
transitions.
