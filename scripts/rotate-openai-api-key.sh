#!/usr/bin/env bash
set -Eeuo pipefail

# Optional: paste the new key here for one-off use.
# Prefer leaving this empty and using NEW_OPENAI_API_KEY env var or secure prompt.
HARDCODED_NEW_OPENAI_API_KEY=""

KUBE_CONTEXT="${KUBE_CONTEXT:-nird-lmd}"
KUBE_NAMESPACE="${KUBE_NAMESPACE:-gbif-no-ns8095k}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
TARGET="${1:-both}"

NEW_OPENAI_API_KEY="${NEW_OPENAI_API_KEY:-$HARDCODED_NEW_OPENAI_API_KEY}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/rotate-openai-api-key.sh [prod|staging|both]

Options:
  prod      Update secret/deployment for production only
  staging   Update secret/deployment for staging only
  both      Update both (default)

How to pass key:
  1) export NEW_OPENAI_API_KEY='sk-...'
  2) or set HARDCODED_NEW_OPENAI_API_KEY in this script
  3) or leave both empty and script will prompt securely
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command not found: $1" >&2
    exit 1
  fi
}

validate_target() {
  case "$1" in
    prod|staging|both) ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: invalid target '$1' (use prod|staging|both)" >&2
      usage
      exit 1
      ;;
  esac
}

prompt_for_key_if_needed() {
  if [[ -n "$NEW_OPENAI_API_KEY" ]]; then
    return 0
  fi

  read -r -s -p "Enter new OPENAI_API_KEY: " NEW_OPENAI_API_KEY
  echo
  if [[ -z "$NEW_OPENAI_API_KEY" ]]; then
    echo "Error: OPENAI key cannot be empty." >&2
    exit 1
  fi
}

warn_if_unexpected_key_shape() {
  if [[ "$NEW_OPENAI_API_KEY" != sk-* ]]; then
    echo "Warning: key does not start with 'sk-'. Continuing anyway." >&2
  fi
}

update_one() {
  local secret_name="$1"
  local deployment_name="$2"
  local key_b64

  key_b64="$(printf '%s' "$NEW_OPENAI_API_KEY" | base64 | tr -d '\n')"

  echo "Checking secret '$secret_name'..."
  kubectl --context "$KUBE_CONTEXT" -n "$KUBE_NAMESPACE" get secret "$secret_name" >/dev/null

  echo "Patching OPENAI_API_KEY in secret '$secret_name'..."
  kubectl --context "$KUBE_CONTEXT" -n "$KUBE_NAMESPACE" patch secret "$secret_name" \
    --type merge \
    -p "{\"data\":{\"OPENAI_API_KEY\":\"$key_b64\"}}"

  echo "Restarting deployment '$deployment_name'..."
  kubectl --context "$KUBE_CONTEXT" -n "$KUBE_NAMESPACE" rollout restart "deployment/$deployment_name"

  echo "Waiting for rollout of '$deployment_name'..."
  kubectl --context "$KUBE_CONTEXT" -n "$KUBE_NAMESPACE" rollout status "deployment/$deployment_name" \
    --timeout="$ROLLOUT_TIMEOUT"
}

main() {
  require_cmd kubectl
  require_cmd base64
  validate_target "$TARGET"
  prompt_for_key_if_needed
  warn_if_unexpected_key_shape

  echo "Using context=$KUBE_CONTEXT namespace=$KUBE_NAMESPACE target=$TARGET"

  case "$TARGET" in
    prod)
      update_one "chatipt-backend" "chatipt-backend"
      ;;
    staging)
      update_one "chatipt-backend-staging" "chatipt-staging-backend"
      ;;
    both)
      update_one "chatipt-backend" "chatipt-backend"
      update_one "chatipt-backend-staging" "chatipt-staging-backend"
      ;;
  esac

  echo "Done."
}

main "$@"
