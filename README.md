# Welcome to ChatIPT

ChatIPT helps students and researchers publish biodiversity datasets to GBIF, especially when they publish only occasionally and do not want to learn the full technical workflow.

## What ChatIPT does

1. You upload one or more data files.
2. The chatbot helps clean and standardize the data through conversation.
3. It guides metadata creation.
4. It creates a relational Darwin Core Data Package (DwC-DP) as the authoritative dataset.
5. It derives the most complete standards-compliant Darwin Core Archive projection possible for publication on GBIF.

The dashboard also has a separate **Convert a Darwin Core Archive** workflow.
Upload an existing Event, Occurrence or Taxon archive ZIP or loose files, review
ambiguous mappings, and download a validated package with the original files and
conversion report. A checklist alone produces a taxonomy Data Package; approved
actual occurrence records can also produce standard DwC-DP tables. Optional AI
suggestions run in successive batches and require approval. See the
[mapping audit and implementation notes](docs/dwca-conversion/README.md) for supported
extensions and current limits.

Supported source mappings and retention happen automatically. Choices that change
meaning still need input; parent/child scientific findings are reported without
blocking conversion or claiming that the source is scientifically consistent.

Scientific-name reviews in the chat and archive conversion share their grouping,
automatic defaults, choices, confirmation checks and column-value previews. Their
decision policy lives in `back-end/api/name_review.py`; both screens render
`front-end/app/components/ScientificNameReview.js`. Keep name-review changes in
these shared layers and verify both workflows with `api.test_name_review`.
In the chat, a review the agent requests (`Agent.open_taxon_review`) stays open
through chat replies until the user presses Done or Skip, and the agent cannot
complete its stage or publish meanwhile.

## Who it is for

- People new to biodiversity data publication.
- People who publish small/medium spreadsheet datasets infrequently.
- Users who want a guided workflow in a web browser.

## Typical files

- Spreadsheet/tabular files such as CSV, TSV, TXT, XLS, XLSX.
- Tree files can also be uploaded, but tree handling is currently limited.

## Current limitations

- For ad hoc spreadsheet publication workflows.
- Large or highly technical publication pipelines are better handled with dedicated tooling (for example, IPT + technical support).

## Need access or support?

Contact: `rukayasj@uio.no`

## Local development policy (Docker-only)

This project is run in Docker for **all** local work.

- Do not install or rely on local Python, Node, `pip`, `npm`, or virtualenv for normal ChatIPT development.
- Run backend and frontend through `docker compose`.
- If you need Django or frontend commands, run them inside the containers (for example with `docker exec`).

Quick start:

```bash
cd /Users/rukayasj/Projects/chatipt
docker compose up --build
```

Local URLs:

- Frontend: `http://localhost:3000`
- Backend API: `http://localhost:8000/api`

## OpenAI model routing

ChatIPT routes bounded work (including the suitability assessment, at `xhigh`
effort) to `gpt-6-luna`, general agentic work to
`gpt-6.1-sol`, and the independent pre-publication quality gate to `gpt-6.1-sol`
at `xhigh` reasoning effort. The tier defaults can be overridden with
`OPENAI_MODEL_EFFICIENT`, `OPENAI_MODEL_STANDARD`, and
`OPENAI_MODEL_CRITICAL` (for example `gpt-6-astra`), and the quality gate's
effort with `OPENAI_CRITICAL_REASONING_EFFORT`. The legacy `OPENAI_MODEL` setting remains a fallback
override for the standard tier.

Administrators can inspect per-dataset model usage through the existing
`openai-usage` API action. A cross-dataset rollout report is also available:

```bash
docker compose exec back-end python manage.py openai_model_report
docker compose exec back-end python manage.py openai_model_report --since 2026-09-24
```

The report groups calls by model, service tier, and workflow task and includes
completion proxies, retries, latency, token/cache usage, and estimated cost.

For GPT-5.6 and later, each agent keeps its opening prompt fixed and places an
explicit cache breakpoint after it. Up to three more breakpoints preserve the
previous and current compacted history boundaries and the latest eligible item.
This keeps an earlier cache endpoint available when recent tool output is
compacted. The changing per-turn workflow state follows all breakpoints. Each usage record
includes a hash of the opening cache prefix and
OpenAI's comparison diagnostics against the previous call with that prefix.
Compare `cached_input_tokens`, `cache_write_input_tokens`, and
`cache_diagnostics` across calls when evaluating a cost change.

GPT-6.1 Sol and GPT-6 Luna calls use Flex processing by default
(`OPENAI_SOL_SERVICE_TIER=flex` and `OPENAI_LUNA_SERVICE_TIER=flex`). Flex costs are included in the per-dataset ceiling.
The default cumulative ceiling is $7 per dataset, including later revisions;
set `OPENAI_DATASET_COST_LIMIT_USD` to override it.
Capacity-only Flex 429s get one retry before a Standard fallback. Set
`OPENAI_SOL_SERVICE_TIER=default` or `OPENAI_LUNA_SERVICE_TIER=default` to disable
Flex for the corresponding model family. Agent turns are queued in the
database and run by `run_agent_turns --watch` from the backend container, so
longer Flex calls do not hold a browser request open. Once a dataset upload or
user reply has been saved, the worker continues model and tool turns, advances
workflow stages, and schedules GBIF validator rechecks without an open tab.
It stops when the agent needs input, completes, or reports an error. Users can
request a one-time email for the next attention event; email delivery requires
`GMAIL_APP_EMAIL` and `GMAIL_APP_PASSWORD` on the backend. The default
Flex timeout is 900 seconds; a crashed worker's turn is recoverable after the
3600-second lease. Three workers run per backend container by default; set
`AGENT_TURN_WORKER_COUNT` to adjust concurrency or `RUN_AGENT_TURN_WORKER=0`
to disable them. One worker also reconciles recently active workflows left
without a queued job by the earlier browser-driven implementation.

## DwC-DP schema snapshot

ChatIPT vendors the complete DwC-DP profile and table schemas under
`back-end/api/templates/dwc-dp`. The current snapshot is the DwC-DP 1.0
prerelease (`1.0_DEV`) from TDWG's `rs.tdwg.org` `dwc` branch at commit
`76898192fd298c2aa170a7059e1bdadf3ee2a828`. Package descriptors reference
TDWG's deployed prerelease profile at
`https://dwc-prerelease.rs.tdwg.org/dwc-dp/1.0_DEV/dwc-dp-profile.json` and
record the immutable source revision and a content hash so exported datasets
remain reproducible while the deployed profile is a prerelease.

When updating the snapshot, replace the profile and complete `table-schemas`
directory together, update the local index/version metadata and
`DWC_DP_SCHEMA_REVISION` in `back-end/api/dwc_dp_specs.py`, then run the backend
tests in Docker. Once a final DwC-DP 1.0 profile is published and accessible,
update the snapshot and profile URL atomically from `1.0_DEV` to the final
versioned release. The [conversion fidelity audit](docs/dwca-conversion/fidelity-audit.md)
records the current schema status and value-level checks.

Exported packages are validated offline with checks ported from GBIF's
[dwc-dp-analyser](https://github.com/gbif/dwc-dp-analyser): the descriptor against
the DwC-DP profile (its base Frictionless Data Package v1 schema is vendored at
`back-end/api/templates/frictionless/data-package-v1.json`), each resource schema
against its canonical table schema, `eml.xml` against the EML 2.2.0 XSDs in
`back-end/api/templates/xsd/eml-2.2.0` (copied from dwc-dp-analyser commit
`d3dd96bc7c769724466f8495c331299ebd8c977f`), and every row with Frictionless
`validate()`.

The provisional GBIF check during refinement uses a disposable DwC-A projection.
It translates `detected`/`notDetected` to the current validator's `present`/`absent`
vocabulary and omits a redundant year beside an agreeing full ISO date when no
other date components are supplied. This avoids GBIF reducing the date precision;
conflicting years remain available to the validator. Authoritative DwC-DP tables
retain all source values, and their vocabulary checks accept both the current
detection terms and legacy presence terms.

## Deployment docs

Operational deployment instructions are maintained in [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Production deploy command

Use the repo script as the standard production deploy entrypoint:

```bash
cd /Users/rukayasj/Projects/chatipt
./scripts/deploy-prod.sh --tag sha-FULL_COMMIT_SHA
```

GitHub Actions builds both production images on pushes to `main` and publishes
`sha-<full-commit-sha>` tags to Docker Hub. Pull requests build without publishing.
Wait for the complete workflow to succeed, then use its summary's deploy command.
The deployment script only deploys existing images; it never builds or pushes them.
Repository Actions secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` are required.
