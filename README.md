# Welcome to ChatIPT

ChatIPT helps students and researchers publish biodiversity datasets to GBIF, especially when they publish only occasionally and do not want to learn the full technical workflow.

## What ChatIPT does

1. You upload one or more data files.
2. The chatbot helps clean and standardize the data through conversation.
3. It guides metadata creation.
4. It creates a relational Darwin Core Data Package (DwC-DP) as the authoritative dataset.
5. It derives a conservative Darwin Core Archive projection for publication on GBIF.

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

## DwC-DP schema snapshot

ChatIPT vendors the complete DwC-DP profile and table schemas under
`back-end/api/templates/dwc-dp`. The current snapshot is version 0.1, issued
2026-06-26 from GBIF's `gbif/dwc-dp` commit
`46bc94f5d7f7e44d4d3a116248c8bff3033e13a5`. Package descriptors record this
revision and a content hash so exported datasets remain reproducible even while
the specification is evolving.

When updating the snapshot, replace the profile, index, version, and complete
`table-schemas` directory together, then update `DWC_DP_SCHEMA_REVISION` in
`back-end/api/dwc_dp_specs.py` and run the backend tests in Docker.

## Deployment docs

Operational deployment instructions are maintained in [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Production deploy command

Use the repo script as the standard production deploy entrypoint:

```bash
cd /Users/rukayasj/Projects/chatipt
./scripts/deploy-prod.sh
```
