# Welcome to ChatIPT

ChatIPT helps students and researchers publish biodiversity datasets to GBIF, especially when they publish only occasionally and do not want to learn the full technical workflow.

## What ChatIPT does

1. You upload one or more data files.
2. The chatbot helps clean and standardize the data through conversation.
3. It guides metadata creation.
4. It publishes the result as a Darwin Core Archive on GBIF.

## Who it is for

- People new to biodiversity data publication.
- People who publish small/medium spreadsheet datasets infrequently.
- Users who want a guided workflow in a web browser.

## Typical files

- Spreadsheet/tabular files such as CSV, TSV, TXT, XLS, XLSX.
- Tree files can also be uploaded, but tree handling is currently limited.

## Current limitations

- Best for ad hoc spreadsheet publication workflows.
- Not intended for direct publication from operational databases.
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

## Deployment docs

Operational deployment instructions are maintained in [`DEPLOYMENT.md`](DEPLOYMENT.md).
