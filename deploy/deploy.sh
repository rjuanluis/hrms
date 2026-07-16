#!/usr/bin/env bash
set -Eeuo pipefail

SITE_NAME="hr.aroypedal.com"
EASYPANEL_PROJECT="web"
EASYPANEL_SERVICE="ayp-hrms"
COMPOSE_PROJECT="${EASYPANEL_PROJECT}_${EASYPANEL_SERVICE}"
COMPOSE_DIR="/etc/easypanel/projects/${EASYPANEL_PROJECT}/${EASYPANEL_SERVICE}/code"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.yml"
COMPOSE_OVERRIDE="${COMPOSE_DIR}/docker-compose.override.yml"
PRODUCTION_IMAGE="ghcr.io/rjuanluis/ayp-hrms:production"
SITES_VOLUME="ayp_hr_sites"
LOGS_VOLUME="ayp_hr_logs"
SECRETS_DIR="/opt/ayp-hr/secrets"
DEPLOY_URL_FILE="${SECRETS_DIR}/easypanel_deploy_url"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:-}"

if [[ ! "$IMAGE" =~ ^ghcr\.io/rjuanluis/ayp-hrms:[0-9a-f]{40}$ ]]; then
  echo "Invalid immutable AyP HR image reference" >&2
  exit 2
fi

for path in \
  "$SECRETS_DIR/db_root_password" \
  "$SECRETS_DIR/admin_password" \
  "$DEPLOY_URL_FILE" \
  "$COMPOSE_FILE"; do
  [[ -s "$path" ]] || { echo "Missing required file: $path" >&2; exit 3; }
done

for volume in "$SITES_VOLUME" "$LOGS_VOLUME" ayp_hr_db_data ayp_hr_redis_queue_data; do
  docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null
done

EASYPANEL_CONTAINER="$(docker ps --filter name=easypanel. --format '{{.ID}}' | head -1)"
[[ -n "$EASYPANEL_CONTAINER" ]] || { echo "EasyPanel container is not running" >&2; exit 4; }

compose() {
  local args=(-p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE")
  [[ -s "$COMPOSE_OVERRIDE" ]] && args+=(-f "$COMPOSE_OVERRIDE")
  docker exec "$EASYPANEL_CONTAINER" docker compose "${args[@]}" "$@"
}

container_id() {
  docker ps -q \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
    --filter "label=com.docker.compose.service=$1" | head -1
}

wait_for_compose() {
  local expected=9
  for _ in {1..120}; do
    local running db_id db_health
    running="$(docker ps \
      --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
      --format '{{.Label "com.docker.compose.service"}}' | sort -u | wc -l | tr -d ' ')"
    db_id="$(container_id db)"
    db_health=""
    if [[ -n "$db_id" ]]; then
      db_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$db_id" 2>/dev/null || true)"
    fi
    if [[ "$running" == "$expected" && "$db_health" == "healthy" ]]; then
      return 0
    fi
    sleep 3
  done
  echo "EasyPanel Compose services did not become ready" >&2
  docker ps -a --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
    --format '{{.Names}}|{{.Status}}|{{.Image}}' >&2 || true
  return 1
}

backend_id="$(container_id backend)"
if [[ -n "$backend_id" ]] && docker exec "$backend_id" test -f "sites/$SITE_NAME/site_config.json"; then
  echo "Creating pre-deploy backup"
  docker exec "$backend_id" bench --site "$SITE_NAME" backup --with-files
fi

echo "Preparing immutable image $IMAGE"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker pull "$IMAGE" >/dev/null
fi
docker tag "$IMAGE" "$PRODUCTION_IMAGE"

echo "Requesting deployment through EasyPanel"
curl -fsS --max-time 30 -X POST "$(<"$DEPLOY_URL_FILE")" >/dev/null
wait_for_compose

HOOK_DIR="/opt/ayp-hr/deploy-hooks"
install -d -m 700 "$HOOK_DIR"
install -m 644 "$ROOT_DIR/deploy/bootstrap_site.py" "$HOOK_DIR/bootstrap_site.py"
install -m 644 "$ROOT_DIR/deploy/configure_standard.py" "$HOOK_DIR/configure_standard.py"

echo "Writing common Frappe configuration"
compose exec -T backend bash -ec '
  ls -1 apps > sites/apps.txt
  bench set-config -g db_host db
  bench set-config -gp db_port 3306
  bench set-config -g redis_cache redis://redis-cache:6379
  bench set-config -g redis_queue redis://redis-queue:6379
  bench set-config -g redis_socketio redis://redis-queue:6379
  bench set-config -gp socketio_port 9000
  bench set-config -g chromium_path /usr/bin/chromium-headless-shell
'

if compose exec -T backend test -f "sites/$SITE_NAME/site_config.json"; then
  echo "Migrating existing site"
  compose exec -T backend bench --site "$SITE_NAME" migrate
else
  echo "Creating official site $SITE_NAME"
  compose run --rm \
    -e "AYP_SITE_NAME=$SITE_NAME" \
    -v "$SECRETS_DIR:/run/ayp-secrets:ro" \
    -v "$HOOK_DIR/bootstrap_site.py:/opt/ayp/bootstrap_site.py:ro" \
    backend /home/frappe/frappe-bench/env/bin/python /opt/ayp/bootstrap_site.py
fi

compose exec -T backend bench --site "$SITE_NAME" set-config host_name "https://$SITE_NAME"

echo "Applying idempotent standard AyP configuration"
compose run --rm \
  -e "AYP_SITE_NAME=$SITE_NAME" \
  -v "$SECRETS_DIR:/run/ayp-secrets:ro" \
  -v "$HOOK_DIR/configure_standard.py:/opt/ayp/configure_standard.py:ro" \
  backend /home/frappe/frappe-bench/env/bin/python /opt/ayp/configure_standard.py

compose restart backend frontend websocket queue-short queue-long scheduler >/dev/null
wait_for_compose

python3 - "$IMAGE" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
receipt = {
    "status": "deployed",
    "operator": "easypanel-compose",
    "project": "web",
    "service": "ayp-hrms",
    "image": sys.argv[1],
    "site": "hr.aroypedal.com",
    "deployed_at": datetime.now(timezone.utc).isoformat(),
}
path = Path("/opt/ayp-hr/last-deploy.json")
tmp = path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
tmp.chmod(0o644)
tmp.replace(path)
PY

rm -rf "$HOOK_DIR"
echo "EasyPanel deployment completed for $IMAGE"
