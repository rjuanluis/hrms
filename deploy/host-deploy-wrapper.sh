#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="/opt/ayp-hr/source"
ORIGINAL="${SSH_ORIGINAL_COMMAND:-}"

if [[ "$ORIGINAL" =~ ^deploy[[:space:]]+(ghcr\.io/rjuanluis/ayp-hrms:([0-9a-f]{40}|production))[[:space:]]+([A-Za-z0-9-]+)$ ]]; then
  IMAGE="${BASH_REMATCH[1]}"
  REGISTRY_USER="${BASH_REMATCH[3]}"
else
  echo "Only an immutable AyP HR deploy command is allowed" >&2
  exit 64
fi

IFS= read -r REGISTRY_TOKEN
[[ -n "$REGISTRY_TOKEN" ]] || { echo "Missing ephemeral GHCR token" >&2; exit 65; }
DOCKER_CONFIG="$(mktemp -d /tmp/ayphr-docker-config.XXXXXX)"
chmod 700 "$DOCKER_CONFIG"
export DOCKER_CONFIG
printf '%s' "$REGISTRY_TOKEN" | docker login ghcr.io --username "$REGISTRY_USER" --password-stdin >/dev/null
unset REGISTRY_TOKEN

cleanup() {
  docker logout ghcr.io >/dev/null 2>&1 || true
  rm -rf "$DOCKER_CONFIG"
}
trap cleanup EXIT

exec 9>/opt/ayp-hr/deploy.lock
flock -n 9 || { echo "Another AyP HR deploy is running" >&2; exit 75; }

git -C "$ROOT_DIR" fetch --depth=1 origin ayp-production
git -C "$ROOT_DIR" reset --hard origin/ayp-production
"$ROOT_DIR/deploy/deploy.sh" "$IMAGE"
