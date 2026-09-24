# Welcome to ChatIPT

ChatIPT helps students and researchers publish biodiversity datasets to GBIF, especially when they publish only occasionally and do not want to learn the full technical workflow.

## What ChatIPT does

1. You upload one or more data files.
2. The chatbot helps clean and standardize the data through conversation.
3. It guides metadata creation.
4. It creates a relational Darwin Core Data Package (DwC-DP) as the authoritative dataset.
5. It derives the most complete standards-compliant Darwin Core Archive projection possible for publication on GBIF.

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
`gpt-6-sol`, and the independent pre-publication quality gate to `gpt-6-sol`
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

The report groups calls by model and workflow task and includes completion
proxies, retries, latency, token/cache usage, and estimated standard-tier cost.

## DwC-DP schema snapshot

ChatIPT vendors the complete DwC-DP profile and table schemas under
`back-end/api/templates/dwc-dp`. The current snapshot is the DwC-DP 1.0
prerelease (`1.0_DEV`) from TDWG's `rs.tdwg.org` `dwc` branch at commit
`76898192fd298c2aa170a7059e1bdadf3ee2a828`. Package descriptors reference
TDWG's deployed prerelease profile at
`https://dwc-prerelease.rs.tdwg.org/dwc-dp/1.0_DEV/dwc-dp-profile.json` and
record the immutable source revision and a content hash so exported datasets
remain reproducible while the schemas are under public review.

When updating the snapshot, replace the profile and complete `table-schemas`
directory together, update the local index/version metadata and
`DWC_DP_SCHEMA_REVISION` in `back-end/api/dwc_dp_specs.py`, then run the backend
tests in Docker. Once DwC-DP 1.0 is ratified, update the snapshot and profile URL
atomically from `1.0_DEV` to the final versioned release.

## Deployment docs

Operational deployment instructions are maintained in [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Production deploy command

Use the repo script as the standard production deploy entrypoint:

```bash
cd /Users/rukayasj/Projects/chatipt
./scripts/deploy-prod.sh
```
