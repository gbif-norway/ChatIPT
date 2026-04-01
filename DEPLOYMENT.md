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

## 1) Build and push images

Use a unique tag per release.

```bash
cd /Users/rukayasj/Projects/chatipt

SHA=$(git rev-parse --short HEAD)
STAMP=$(date -u +%Y%m%d-%H%M%S)
TAG="${SHA}-${STAMP}"
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
- Persistent storage is currently configured via existing shared PVC:
  - claim: `573890b9-3346-4027-ab0c-22eec6dfd665`
  - subPath:
    - prod: `chatipt-prod-user-files`
    - staging: `chatipt-staging-user-files`
