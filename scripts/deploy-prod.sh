#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GITOPS_DIR="${GITOPS_DIR:-$ROOT_DIR/../gitops}"
VALUES_FILE="${VALUES_FILE:-$GITOPS_DIR/apps/chatipt/values-prod.yaml}"

KUBE_CONTEXT="${KUBE_CONTEXT:-nird-lmd}"
KUBE_NAMESPACE="${KUBE_NAMESPACE:-gbif-no-ns8095k}"
HELM_RELEASE="${HELM_RELEASE:-chatipt}"
HELM_CHART_PATH="${HELM_CHART_PATH:-$GITOPS_DIR/apps/chatipt}"

BACKEND_IMAGE_REPO="${BACKEND_IMAGE_REPO:-gbifnorway/chatipt-back-end}"
FRONTEND_IMAGE_REPO="${FRONTEND_IMAGE_REPO:-gbifnorway/chatipt-front-end}"
FRONTEND_API_URL="${FRONTEND_API_URL:-https://api.chatipt.svc.gbif.no}"
BUILD_PLATFORM="${BUILD_PLATFORM:-linux/amd64}"
BUILD_BUILDER_PRIMARY="${BUILD_BUILDER_PRIMARY:-chatipt-builder}"
BUILD_BUILDER_FALLBACK="${BUILD_BUILDER_FALLBACK:-desktop-linux}"

usage() {
  cat <<'EOF'
Usage: ./scripts/deploy-prod.sh [--tag TAG] [--skip-gitops-commit]

Options:
  --tag TAG              Use an explicit image tag (default: auto-generated 2.0.0-<sha>-<utcstamp>)
  --skip-gitops-commit   Update values file but skip git commit/push in ../gitops
  -h, --help             Show this help

Environment overrides:
  GITOPS_DIR, VALUES_FILE, KUBE_CONTEXT, KUBE_NAMESPACE, HELM_RELEASE, HELM_CHART_PATH
  BACKEND_IMAGE_REPO, FRONTEND_IMAGE_REPO, FRONTEND_API_URL
  BUILD_PLATFORM, BUILD_BUILDER_PRIMARY, BUILD_BUILDER_FALLBACK
EOF
}

log() {
  printf '\n[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

docker_is_running() {
  docker info >/dev/null 2>&1
}

build_frontend_with_fallback() {
  local tag="$1"
  local primary_ok=0

  set +e
  docker buildx build \
    --builder "$BUILD_BUILDER_PRIMARY" \
    --platform "$BUILD_PLATFORM" \
    -f "$ROOT_DIR/front-end/Dockerfile" \
    --build-arg "NEXT_PUBLIC_BASE_API_URL=$FRONTEND_API_URL" \
    --build-arg NEXT_BUILD_CACHE_SCOPE=prod \
    -t "${FRONTEND_IMAGE_REPO}:${tag}" \
    --provenance=false --sbom=false \
    --push "$ROOT_DIR/front-end"
  primary_ok=$?
  set -e

  if [ "$primary_ok" -eq 0 ]; then
    return 0
  fi

  log "Frontend build failed with builder '$BUILD_BUILDER_PRIMARY'. Retrying with '$BUILD_BUILDER_FALLBACK'."
  docker buildx build \
    --builder "$BUILD_BUILDER_FALLBACK" \
    --platform "$BUILD_PLATFORM" \
    -f "$ROOT_DIR/front-end/Dockerfile" \
    --build-arg "NEXT_PUBLIC_BASE_API_URL=$FRONTEND_API_URL" \
    --build-arg NEXT_BUILD_CACHE_SCOPE=prod \
    -t "${FRONTEND_IMAGE_REPO}:${tag}" \
    --provenance=false --sbom=false \
    --push "$ROOT_DIR/front-end"
}

main() {
  local explicit_tag=""
  local skip_gitops_commit="false"

  while [ $# -gt 0 ]; do
    case "$1" in
      --tag)
        [ $# -ge 2 ] || { echo "--tag requires a value" >&2; exit 1; }
        explicit_tag="$2"
        shift 2
        ;;
      --skip-gitops-commit)
        skip_gitops_commit="true"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        usage
        exit 1
        ;;
    esac
  done

  require_cmd git
  require_cmd docker
  require_cmd kubectl
  require_cmd helm
  require_cmd perl

  if ! docker_is_running; then
    echo "Docker is not running. Start Docker Desktop and rerun." >&2
    exit 1
  fi

  [ -d "$GITOPS_DIR" ] || { echo "GitOps repo not found: $GITOPS_DIR" >&2; exit 1; }
  [ -f "$VALUES_FILE" ] || { echo "Prod values file not found: $VALUES_FILE" >&2; exit 1; }

  local image_tag
  if [ -n "$explicit_tag" ]; then
    image_tag="$explicit_tag"
  else
    local sha stamp
    sha="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    image_tag="2.0.0-${sha}-${stamp}"
  fi

  log "Deploying production with image tag: $image_tag"

  log "Building and pushing backend image"
  docker buildx build \
    --builder "$BUILD_BUILDER_PRIMARY" \
    --platform "$BUILD_PLATFORM" \
    -f "$ROOT_DIR/back-end/Dockerfile" \
    -t "${BACKEND_IMAGE_REPO}:${image_tag}" \
    --provenance=false --sbom=false \
    --push "$ROOT_DIR/back-end"

  log "Building and pushing frontend image"
  build_frontend_with_fallback "$image_tag"

  log "Updating prod values file"
  perl -0777 -i -pe \
    "s|(repository:\\s*${BACKEND_IMAGE_REPO}\\s*\\n\\s*tag:\\s*).*$|\${1}${image_tag}|mg; s|(repository:\\s*${FRONTEND_IMAGE_REPO}\\s*\\n\\s*tag:\\s*).*$|\${1}${image_tag}|mg" \
    "$VALUES_FILE"

  log "Previewing updated values"
  rg -n "^backEnd:|^frontEnd:|^  image:|^    repository:|^    tag:" "$VALUES_FILE"

  if [ "$skip_gitops_commit" = "false" ]; then
    log "Committing and pushing gitops values"
    git -C "$GITOPS_DIR" add "$VALUES_FILE"
    git -C "$GITOPS_DIR" commit -m "chatipt: deploy prod images ${image_tag}"
    git -C "$GITOPS_DIR" push
  else
    log "Skipping gitops commit/push (--skip-gitops-commit)"
  fi

  log "Deploying with Helm"
  helm upgrade --install "$HELM_RELEASE" "$HELM_CHART_PATH" \
    --kube-context "$KUBE_CONTEXT" \
    -n "$KUBE_NAMESPACE" \
    -f "$VALUES_FILE" \
    --wait

  log "Verifying rollout"
  kubectl --context "$KUBE_CONTEXT" -n "$KUBE_NAMESPACE" rollout status deploy/chatipt-backend --timeout=300s
  kubectl --context "$KUBE_CONTEXT" -n "$KUBE_NAMESPACE" rollout status deploy/chatipt-frontend --timeout=300s

  log "Verifying deployed image tags"
  kubectl --context "$KUBE_CONTEXT" -n "$KUBE_NAMESPACE" get deploy chatipt-backend chatipt-frontend \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'

  log "Production deploy complete."
}

main "$@"
