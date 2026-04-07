# ChatIPT Deployment (Sigma2 NIRD)

This document describes the **current** ChatIPT deployment workflow.

- Target cluster context: `nird-lmd`
- Target namespace: `gbif-no-ns8095k`
- CI/CD model: manual, command-driven
- Not used for routine deployment: Jenkins, Argo CD

## Repositories

- App code: `chatipt` (this repo)
- Kubernetes/Helm config: `../gitops/apps/chatipt`

## Environments

- Production
  - Frontend: `https://chatipt.svc.gbif.no`
  - Backend API: `https://api.chatipt.svc.gbif.no`
  - Helm release: `chatipt`
  - Values file: `../gitops/apps/chatipt/values-prod.yaml`
- Staging
  - Frontend: `https://staging.chatipt.svc.gbif.no`
  - Backend API: `https://staging-api.chatipt.svc.gbif.no`
  - Helm release: `chatipt-staging`
  - Values file: `../gitops/apps/chatipt/values-staging.yaml`

## Critical guardrails

- `NEXT_PUBLIC_BASE_API_URL` must be the API origin only (no trailing `/api`).
  - Correct prod value: `https://api.chatipt.svc.gbif.no`
  - Correct staging value: `https://staging-api.chatipt.svc.gbif.no`
  - Wrong value example: `https://api.chatipt.svc.gbif.no/api` (causes frontend calls like `/api/api/auth/status/`)
- Keep image tags mapped to the correct service:
  - `backEnd.image.tag` -> `gbifnorway/chatipt-back-end`
  - `frontEnd.image.tag` -> `gbifnorway/chatipt-front-end`
- For frontend-only hotfixes, change only `frontEnd.image.tag` in the target values file.

## Prerequisites

- `docker` + `docker buildx`
- `kubectl` configured with context `nird-lmd`
- `helm`
- Push access to Docker Hub repos:
  - `gbifnorway/chatipt-back-end`
  - `gbifnorway/chatipt-front-end`
- Write access to `../gitops`

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

## 1) Build and push images

Use a unique tag per release.

```bash
cd /Users/rukayasj/Projects/chatipt

SHA=$(git rev-parse --short HEAD)
STAMP=$(date -u +%Y%m%d-%H%M%S)
TAG="${SHA}-${STAMP}"
```

### Fast path (recommended)

Main bottleneck is usually frontend build/push (large image), not Helm or `kubectl`.

- Build backend once and retag for staging (same backend image for both envs).
- Use buildx registry cache to speed repeated builds.
- Skip rebuilding unchanged component(s) when possible.

```bash
cd /Users/rukayasj/Projects/chatipt

SHA=$(git rev-parse --short HEAD)
STAMP=$(date -u +%Y%m%d-%H%M%S)
TAG="${SHA}-${STAMP}"
PROD_TAG="2.0.0-${TAG}"
STAGING_TAG="staging-${TAG}"

# Build backend once (prod tag)
docker buildx build --platform linux/amd64 \
  -f back-end/Dockerfile \
  -t gbifnorway/chatipt-back-end:${PROD_TAG} \
  --cache-from=type=registry,ref=gbifnorway/chatipt-back-end:buildcache \
  --cache-to=type=registry,ref=gbifnorway/chatipt-back-end:buildcache,mode=max \
  --push back-end

# Reuse exact same backend image for staging tag (no second backend build)
docker buildx imagetools create \
  -t gbifnorway/chatipt-back-end:${STAGING_TAG} \
  gbifnorway/chatipt-back-end:${PROD_TAG}

# Frontend prod (build bakes NEXT_PUBLIC_BASE_API_URL into bundle)
docker buildx build --platform linux/amd64 \
  -f front-end/Dockerfile \
  --build-arg NEXT_PUBLIC_BASE_API_URL=https://api.chatipt.svc.gbif.no \
  -t gbifnorway/chatipt-front-end:${PROD_TAG} \
  --cache-from=type=registry,ref=gbifnorway/chatipt-front-end:buildcache \
  --cache-to=type=registry,ref=gbifnorway/chatipt-front-end:buildcache,mode=max \
  --push front-end

# Frontend staging (separate build because API URL differs)
docker buildx build --platform linux/amd64 \
  -f front-end/Dockerfile \
  --build-arg NEXT_PUBLIC_BASE_API_URL=https://staging-api.chatipt.svc.gbif.no \
  -t gbifnorway/chatipt-front-end:${STAGING_TAG} \
  --cache-from=type=registry,ref=gbifnorway/chatipt-front-end:buildcache \
  --cache-to=type=registry,ref=gbifnorway/chatipt-front-end:buildcache,mode=max \
  --push front-end
```

### Production-style tags

```bash
PROD_TAG="2.0.0-${TAG}"

docker buildx build --platform linux/amd64 \
  -f back-end/Dockerfile \
  -t gbifnorway/chatipt-back-end:${PROD_TAG} \
  --push back-end

docker buildx build --platform linux/amd64 \
  -f front-end/Dockerfile \
  --build-arg NEXT_PUBLIC_BASE_API_URL=https://api.chatipt.svc.gbif.no \
  -t gbifnorway/chatipt-front-end:${PROD_TAG} \
  --push front-end
```

### Staging-style tags

```bash
STAGING_TAG="staging-${TAG}"

docker buildx build --platform linux/amd64 \
  -f back-end/Dockerfile \
  -t gbifnorway/chatipt-back-end:${STAGING_TAG} \
  --push back-end

docker buildx build --platform linux/amd64 \
  -f front-end/Dockerfile \
  --build-arg NEXT_PUBLIC_BASE_API_URL=https://staging-api.chatipt.svc.gbif.no \
  -t gbifnorway/chatipt-front-end:${STAGING_TAG} \
  --push front-end
```

## 2) Update image tags in gitops

Edit one or both values files:

- `../gitops/apps/chatipt/values-prod.yaml`
- `../gitops/apps/chatipt/values-staging.yaml`

Set both:

- `backEnd.image.tag`
- `frontEnd.image.tag`

to the new image tag you pushed.

Commit and push those changes in `../gitops`.

Quick check before commit/push (prevents backend/frontend tag mix-ups):

```bash
cd /Users/rukayasj/Projects/gitops
rg -n "^backEnd:|^frontEnd:|^  image:|^    repository:|^    tag:" apps/chatipt/values-prod.yaml
rg -n "^backEnd:|^frontEnd:|^  image:|^    repository:|^    tag:" apps/chatipt/values-staging.yaml
```

## 3) Deploy with Helm

```bash
cd /Users/rukayasj/Projects/gitops

helm upgrade --install chatipt apps/chatipt \
  --context nird-lmd \
  -n gbif-no-ns8095k \
  -f apps/chatipt/values-prod.yaml

helm upgrade --install chatipt-staging apps/chatipt \
  --context nird-lmd \
  -n gbif-no-ns8095k \
  -f apps/chatipt/values-staging.yaml
```

Deploy only the environment you changed.

## 4) Verify rollout

```bash
kubectl --context nird-lmd -n gbif-no-ns8095k get deploy chatipt-backend chatipt-frontend
kubectl --context nird-lmd -n gbif-no-ns8095k get deploy chatipt-staging-backend chatipt-staging-frontend
kubectl --context nird-lmd -n gbif-no-ns8095k get ingress | grep chatipt
kubectl --context nird-lmd -n gbif-no-ns8095k rollout status deploy/chatipt-backend
kubectl --context nird-lmd -n gbif-no-ns8095k rollout status deploy/chatipt-frontend
kubectl --context nird-lmd -n gbif-no-ns8095k rollout status deploy/chatipt-staging-backend
kubectl --context nird-lmd -n gbif-no-ns8095k rollout status deploy/chatipt-staging-frontend

# Confirm deployed images are mapped correctly
kubectl --context nird-lmd -n gbif-no-ns8095k get deploy chatipt-backend chatipt-frontend \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
kubectl --context nird-lmd -n gbif-no-ns8095k get deploy chatipt-staging-backend chatipt-staging-frontend \
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

helm --kube-context nird-lmd -n gbif-no-ns8095k history chatipt-staging
helm --kube-context nird-lmd -n gbif-no-ns8095k rollback chatipt-staging <REVISION>
```

## Runtime assumptions

- Backend secret names expected by the chart:
  - prod: `chatipt-backend`
  - staging: `chatipt-backend-staging`
- Discord bug-report env vars should be set in those backend secrets:
  - `DISCORD_WEBHOOK` (required for Discord notifications)
  - `DISCORD_DEVELOPER_USER_ID` (recommended for reliable user mention ping)
  - `DISCORD_DEVELOPER_HANDLE` (optional fallback, defaults to `@_rkian`)
  - Backward compatibility: `DISCORD_RUKAYA_USER_ID` is still accepted if already present.
- Persistent storage is currently configured via existing shared PVC:
  - claim: `573890b9-3346-4027-ab0c-22eec6dfd665`
  - subPath:
    - prod: `chatipt-prod-user-files`
    - staging: `chatipt-staging-user-files`
