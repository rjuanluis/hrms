#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="/opt/ayp-hr/source"
ORIGINAL="${SSH_ORIGINAL_COMMAND:-}"

if [[ "$ORIGINAL" =~ ^deploy[[:space:]]+(ghcr\.io/rjuanluis/ayp-hrms:([0-9a-f]{40}|production))$ ]]; then
  IMAGE="${BASH_REMATCH[1]}"
else
  echo "Only an immutable AyP HR deploy command is allowed" >&2
  exit 64
fi

exec 9>/opt/ayp-hr/deploy.lock
flock -n 9 || { echo "Another AyP HR deploy is running" >&2; exit 75; }

git -C "$ROOT_DIR" fetch --depth=1 origin ayp-production
git -C "$ROOT_DIR" reset --hard origin/ayp-production
exec "$ROOT_DIR/deploy/deploy.sh" "$IMAGE"
