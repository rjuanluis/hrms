#!/usr/bin/env bash
set -Eeuo pipefail

SITE_NAME="hr.aroypedal.com"
STACK_NAME="ayphr"
APP_NETWORK="ayp_hr_net"
SITES_VOLUME="ayp_hr_sites"
LOGS_VOLUME="ayp_hr_logs"
SECRETS_DIR="/opt/ayp-hr/secrets"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:-}"

if [[ ! "$IMAGE" =~ ^ghcr\.io/rjuanluis/ayp-hrms:([0-9a-f]{40}|production)$ ]]; then
  echo "Invalid immutable AyP HR image reference" >&2
  exit 2
fi

for path in "$SECRETS_DIR/db_root_password" "$SECRETS_DIR/admin_password"; do
  [[ -s "$path" ]] || { echo "Missing required secret file: $path" >&2; exit 3; }
done

for resource in "$APP_NETWORK" "$SITES_VOLUME" "$LOGS_VOLUME" ayp_hr_db_data ayp_hr_redis_queue_data; do
  if [[ "$resource" == "$APP_NETWORK" ]]; then
    docker network inspect "$resource" >/dev/null 2>&1 || docker network create --driver overlay --attachable "$resource" >/dev/null
  else
    docker volume inspect "$resource" >/dev/null 2>&1 || docker volume create "$resource" >/dev/null
  fi
done

docker secret inspect ayp_hr_db_root_password >/dev/null 2>&1 || {
  docker secret create ayp_hr_db_root_password "$SECRETS_DIR/db_root_password" >/dev/null
}

echo "Pulling $IMAGE"
docker pull "$IMAGE" >/dev/null
export AYP_HR_IMAGE="$IMAGE"
docker stack deploy --with-registry-auth --prune -c "$ROOT_DIR/deploy/swarm-stack.yml" "$STACK_NAME"

wait_for_service() {
  local service="$1" attempts="${2:-60}"
  for ((i=1; i<=attempts; i++)); do
    local state
    state="$(docker service ps "$service" --filter desired-state=running --format '{{.CurrentState}}' 2>/dev/null | head -1 || true)"
    if [[ "$state" == Running* ]]; then return 0; fi
    sleep 3
  done
  echo "Service $service did not become ready" >&2
  docker service ps "$service" --no-trunc >&2 || true
  return 1
}

wait_for_service "${STACK_NAME}_db" 80
wait_for_service "${STACK_NAME}_redis-cache" 60
wait_for_service "${STACK_NAME}_redis-queue" 60

for i in {1..60}; do
  if docker run --rm --network "$APP_NETWORK" \
      -v "$SECRETS_DIR:/run/ayp-secrets:ro" \
      mariadb:11.8 sh -ec 'export MYSQL_PWD="$(cat /run/ayp-secrets/db_root_password)"; mariadb-admin ping -h db -uroot --silent' \
      >/dev/null 2>&1; then
    break
  fi
  if [[ "$i" == 60 ]]; then echo "MariaDB health check failed" >&2; exit 4; fi
  sleep 3
done

echo "Writing common Frappe configuration"
docker run --rm --network "$APP_NETWORK" \
  -v "$SITES_VOLUME:/home/frappe/frappe-bench/sites" \
  -v "$LOGS_VOLUME:/home/frappe/frappe-bench/logs" \
  "$IMAGE" bash -ec '
    ls -1 apps > sites/apps.txt
    bench set-config -g db_host db
    bench set-config -gp db_port 3306
    bench set-config -g redis_cache redis://redis-cache:6379
    bench set-config -g redis_queue redis://redis-queue:6379
    bench set-config -g redis_socketio redis://redis-queue:6379
    bench set-config -gp socketio_port 9000
    bench set-config -g chromium_path /usr/bin/chromium-headless-shell
  '

SITE_EXISTS="$(docker run --rm -v "$SITES_VOLUME:/home/frappe/frappe-bench/sites" "$IMAGE" \
  bash -ec "test -f sites/$SITE_NAME/site_config.json && echo yes || echo no")"

if [[ "$SITE_EXISTS" == "yes" ]]; then
  echo "Creating pre-deploy backup"
  docker run --rm --network "$APP_NETWORK" \
    -v "$SITES_VOLUME:/home/frappe/frappe-bench/sites" \
    -v "$LOGS_VOLUME:/home/frappe/frappe-bench/logs" \
    "$IMAGE" bench --site "$SITE_NAME" backup --with-files
  echo "Migrating existing site"
  docker run --rm --network "$APP_NETWORK" \
    -v "$SITES_VOLUME:/home/frappe/frappe-bench/sites" \
    -v "$LOGS_VOLUME:/home/frappe/frappe-bench/logs" \
    "$IMAGE" bench --site "$SITE_NAME" migrate
else
  echo "Creating official site $SITE_NAME"
  docker run --rm --network "$APP_NETWORK" \
    -e AYP_SITE_NAME="$SITE_NAME" \
    -e AYP_SECRETS_DIR=/run/ayp-secrets \
    -v "$SITES_VOLUME:/home/frappe/frappe-bench/sites" \
    -v "$LOGS_VOLUME:/home/frappe/frappe-bench/logs" \
    -v "$SECRETS_DIR:/run/ayp-secrets:ro" \
    -v "$ROOT_DIR/deploy/bootstrap_site.py:/opt/ayp/bootstrap_site.py:ro" \
    "$IMAGE" python /opt/ayp/bootstrap_site.py
fi

docker run --rm --network "$APP_NETWORK" \
  -v "$SITES_VOLUME:/home/frappe/frappe-bench/sites" \
  -v "$LOGS_VOLUME:/home/frappe/frappe-bench/logs" \
  "$IMAGE" bench --site "$SITE_NAME" set-config host_name "https://$SITE_NAME"

for service in backend frontend websocket queue-short queue-long scheduler; do
  docker service update --force "${STACK_NAME}_${service}" >/dev/null
  wait_for_service "${STACK_NAME}_${service}" 80
done

python3 - "$IMAGE" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
receipt = {
    "status": "deployed",
    "image": sys.argv[1],
    "site": "hr.aroypedal.com",
    "deployed_at": datetime.now(timezone.utc).isoformat(),
}
path = Path("/opt/ayp-hr/last-deploy.json")
path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
path.chmod(0o644)
PY

echo "Deployment completed for $IMAGE"
