# ChatIPT Deployment (Sigma2 NIRD)

This document describes the **current** ChatIPT deployment workflow.

- Target cluster context: `nird-lmd`
- Target namespace: `gbif-no-ns8095k`
- CI/CD model: GitHub Actions builds/publishes; local Helm deploys
- Not used for routine deployment: Jenkins, Argo CD

## Default production path

For routine production deploys from this repo, use:

```bash
cd /Users/rukayasj/Projects/chatipt
./scripts/deploy-prod.sh --tag sha-FULL_COMMIT_SHA
```

After both GitHub Actions image jobs succeed, copy the exact command from the
workflow summary (replace `FULL_COMMIT_SHA` above with the full commit hash).
Pushing source or publishing images does not deploy production.

The script runs the deployment flow:
- verify both tagged images exist in Docker Hub
- update `../gitops/apps/chatipt/values-prod.yaml`
- commit/push gitops change
- `helm upgrade --install` for `chatipt`
- rollout and image-tag verification

Use the manual sections below for troubleshooting or partial/custom deploy scenarios.

## Repositories

- App code: `chatipt` (this repo)
- Kubernetes/Helm config: `../gitops/apps/chatipt`

## Environments

- Production
  - Frontend: `https://chatipt.svc.gbif.no`
  - Backend API: `https://api.chatipt.svc.gbif.no`
  - Helm release: `chatipt`
  - Values file: `../gitops/apps/chatipt/values-prod.yaml`

There is no staging environment. It was decommissioned on 2026-10-26 (release, secrets,
database, user files and MinIO buckets removed).

## Critical guardrails

- `NEXT_PUBLIC_BASE_API_URL` must be the API origin only (no trailing `/api`).
  - Correct prod value: `https://api.chatipt.svc.gbif.no`
  - Wrong value example: `https://api.chatipt.svc.gbif.no/api` (causes frontend calls like `/api/api/auth/status/`)
- Keep image tags mapped to the correct service:
  - `backEnd.image.tag` -> `gbifnorway/chatipt-back-end`
  - `frontEnd.image.tag` -> `gbifnorway/chatipt-front-end`
- For frontend-only hotfixes, change only `frontEnd.image.tag` in the target values file.

## Prerequisites

- `docker` + `docker buildx`
- `kubectl` configured with context `nird-lmd`
- `helm`, `git`, `perl`, and `rg`
- Read access to the published Docker Hub images (local deployment does not require push access):
  - `gbifnorway/chatipt-back-end`
  - `gbifnorway/chatipt-front-end`
- Write access to `../gitops`

Important local workflow policy:

- Local app development and local deployment are Docker-only.
- Do not rely on a project Python virtualenv (`.venv`) for normal local run/test/deploy steps.
- Use `docker compose` for local backend/frontend startup and local verification.

## Local Development For Agent Sessions

Use this when an agent needs to run and debug the app locally (especially frontend/UI work).
All local development should be done in Docker.

### Start local stack

From repo root:

```bash
cd /Users/rukayasj/Projects/chatipt
docker compose up --build
```

Expected local URLs:

- Frontend: `http://localhost:3000`
- Backend API: `http://localhost:8000/api`

Notes:

- Backend startup uses `back-end/start.sh` as entrypoint in Docker.
- In local compose, `SKIP_COLLECTSTATIC=1` is set to speed up dev startup.
- Frontend runs via `npm run dev` inside compose.

### Let Codex inspect the live browser UI

For high-quality frontend debugging, expose Chrome DevTools to Codex via MCP.

One-time install/config:

```bash
brew install chrome-devtools-mcp
codex mcp add chrome-devtools -- chrome-devtools-mcp --autoConnect --channel stable --no-usage-statistics
```

Checks:

```bash
codex mcp list
curl -i http://127.0.0.1:9222/json/version
```

Expected:

- `codex mcp list` shows `chrome-devtools`.
- Chrome debug endpoint returns `200` with JSON (not `404`).

If `9222` is not available, enable Chrome remote debugging (for example via
`chrome://inspect/#remote-debugging`) and restart Chrome if needed.

Important:

- Existing Codex sessions do not always hot-load newly added MCP servers.
- After adding/enabling Chrome DevTools MCP, start a new Codex session before asking it to inspect UI.

### One-time buildx setup for cache support

Use a `docker-container` buildx builder. The default `docker` driver often fails with:
`Cache export is not supported for the docker driver.`

```bash
# Create once (or switch to it if it already exists)
docker buildx create --name chatipt-builder --driver docker-container --use desktop-linux \
  || docker buildx use chatipt-builder

# Start/verify builder
docker buildx inspect --bootstrap
docker buildx ls
```

### First-time cluster auth on fresh contexts

On a fresh machine/session, the first `kubectl`/`helm` call against `nird-lmd` may launch an in-browser SSO login window.

- Complete the browser auth flow manually (UiO institutional login) and click through the prompts.
- Until this is done, CLI commands can block with no output for a long time.
- This is especially important for agent-driven deployments: the agent may appear "stuck" or idle until a human completes browser auth.
- After successful login, rerun the command that was waiting.

Quick auth check:

```bash
kubectl --context nird-lmd -n gbif-no-ns8095k get deploy
```

## 0) Refresh Darwin Core quick reference (as needed)

The quick reference files are vendored in this repo and can be refreshed from TDWG.

- Source: `https://raw.githubusercontent.com/tdwg/dwc/master/docs/terms/index.md`
- Local files:
  - `back-end/api/templates/dwc-quick-reference-guide.md.txt`
  - `back-end/api/templates/dwc-quick-reference-guide.yaml`
- Generator: `back-end/api/helpers/generate_dwc_yaml.py`

From repo root:

```bash
cd /Users/rukayasj/Projects/chatipt

curl -fsSL https://raw.githubusercontent.com/tdwg/dwc/master/docs/terms/index.md \
  -o back-end/api/templates/dwc-quick-reference-guide.md.txt

python3 back-end/api/helpers/generate_dwc_yaml.py
```

Optional quick check:

```bash
rg -n "^MaterialCitation:|basisOfRecord:" back-end/api/templates/dwc-quick-reference-guide.yaml
```

## 1) Build and publish with GitHub Actions

The workflow `.github/workflows/publish-images.yml` builds both existing Dockerfiles
on native Linux amd64 runners, in parallel, with separate persistent layer caches.
Pushes to `main` publish both images as `sha-<full-commit-sha>` to the existing
`gbifnorway/chatipt-back-end` and `gbifnorway/chatipt-front-end` Docker Hub repos.
Pull requests build both images without publishing. The Actions page also supports
manual runs; publishing is restricted to `main`. No cluster credentials are needed
in GitHub. Application runtime secrets stay in Kubernetes.

One-time setup in this GitHub repository's Settings → Secrets and variables → Actions:

- `DOCKERHUB_USERNAME`: account with push access to both image repositories.
- `DOCKERHUB_TOKEN`: Docker Hub access token with Read & Write permission; delete access is unnecessary. For a personal token, the account must have push access to both repositories.

Wait for the entire workflow (including both image jobs) to succeed. The workflow
summary prints the exact deployment command:

```bash
./scripts/deploy-prod.sh --tag sha-FULL_COMMIT_SHA
```

This command verifies the images exist before changing GitOps values, commits and
pushes the values, deploys with Helm, and verifies the rollout. It never builds or
pushes images. `--tag` is required; existing older release tags are also accepted.
The manual steps below are for troubleshooting; the script normally handles them.

## 2) Update image tags in gitops

In `../gitops/apps/chatipt/values-prod.yaml`, set both:

- `backEnd.image.tag`
- `frontEnd.image.tag`

to the new image tag you pushed.

Commit and push those changes in `../gitops`.

Quick check before commit/push (prevents backend/frontend tag mix-ups):

```bash
cd /Users/rukayasj/Projects/gitops
rg -n "^backEnd:|^frontEnd:|^  image:|^    repository:|^    tag:" apps/chatipt/values-prod.yaml
```

## 3) Deploy with Helm

```bash
cd /Users/rukayasj/Projects/gitops

helm upgrade --install chatipt apps/chatipt \
  --kube-context nird-lmd \
  -n gbif-no-ns8095k \
  -f apps/chatipt/values-prod.yaml
```

## 4) Verify rollout

```bash
kubectl --context nird-lmd -n gbif-no-ns8095k get deploy chatipt-backend chatipt-frontend
kubectl --context nird-lmd -n gbif-no-ns8095k get ingress | grep chatipt
kubectl --context nird-lmd -n gbif-no-ns8095k rollout status deploy/chatipt-backend
kubectl --context nird-lmd -n gbif-no-ns8095k rollout status deploy/chatipt-frontend

# Confirm deployed images are mapped correctly
kubectl --context nird-lmd -n gbif-no-ns8095k get deploy chatipt-backend chatipt-frontend \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
```

Auth URL smoke check (frontend):

```bash
curl -sS https://chatipt.svc.gbif.no/_next/static/chunks/4282-ee375a392ac48521.js \
  | rg 'baseUrl:"https://api.chatipt.svc.gbif.no"|/api/auth/status/|/api/api/auth/status'

# Expected:
# - contains baseUrl:"https://api.chatipt.svc.gbif.no"
# - contains /api/auth/status/
# - does NOT contain /api/api/auth/status
```

## 5) Rollback

```bash
helm --kube-context nird-lmd -n gbif-no-ns8095k history chatipt
helm --kube-context nird-lmd -n gbif-no-ns8095k rollback chatipt <REVISION>
```

## Runtime assumptions

- Backend secret name expected by the chart: `chatipt-backend`
- Discord bug-report env vars should be set in that backend secret:
  - `DISCORD_WEBHOOK` (required for Discord notifications)
  - `DISCORD_DEVELOPER_USER_ID` (recommended for reliable user mention ping)
  - `DISCORD_DEVELOPER_HANDLE` (optional fallback, defaults to `@_rkian`)
  - Backward compatibility: `DISCORD_RUKAYA_USER_ID` is still accepted if already present.
- Persistent storage is currently configured via existing shared PVC:
  - claim: `573890b9-3346-4027-ab0c-22eec6dfd665`
  - subPath: `chatipt-prod-user-files`
