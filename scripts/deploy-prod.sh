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

usage() {
  cat <<'EOF'
Usage: ./scripts/deploy-prod.sh --tag TAG [--skip-gitops-commit]

Options:
  --tag TAG              Existing image tag published for both services (required)
  --skip-gitops-commit   Update values file but skip git commit/push in ../gitops
  -h, --help             Show this help

Environment overrides:
  GITOPS_DIR, VALUES_FILE, KUBE_CONTEXT, KUBE_NAMESPACE, HELM_RELEASE, HELM_CHART_PATH
  BACKEND_IMAGE_REPO, FRONTEND_IMAGE_REPO
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

  [ -n "$explicit_tag" ] || { echo "--tag is required; use a tag from a successful GitHub Actions build." >&2; exit 1; }
  [[ "$explicit_tag" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]] || { echo "Invalid image tag: $explicit_tag" >&2; exit 1; }
  local image_tag="$explicit_tag"

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

  log "Checking published images for tag: $image_tag"
  docker buildx imagetools inspect "${BACKEND_IMAGE_REPO}:${image_tag}" >/dev/null
  docker buildx imagetools inspect "${FRONTEND_IMAGE_REPO}:${image_tag}" >/dev/null

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
