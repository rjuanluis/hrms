#!/usr/bin/env bash
set -Eeuo pipefail

SITE_NAME="hr.aroypedal.com"
EASYPANEL_PROJECT="web"
EASYPANEL_SERVICE="ayp-hrms"
COMPOSE_PROJECT="${EASYPANEL_PROJECT}_${EASYPANEL_SERVICE}"
COMPOSE_DIR="/etc/easypanel/projects/${EASYPANEL_PROJECT}/${EASYPANEL_SERVICE}/code"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.yml"
COMPOSE_OVERRIDE="${COMPOSE_DIR}/docker-compose.override.yml"
EASYPANEL_IMAGE="easypanel/easypanel"
PRODUCTION_IMAGE="ghcr.io/rjuanluis/ayp-hrms:production"
SITES_VOLUME="ayp_hr_sites"
LOGS_VOLUME="ayp_hr_logs"
SECRETS_DIR="/opt/ayp-hr/secrets"
DEPLOY_URL_FILE="${SECRETS_DIR}/easypanel_deploy_url"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:-}"
COMPOSE_ROLLBACK_PATH=""
ROLLBACK_ARMED=0
DEPLOY_PHASE="preflight"
PREVIOUS_SERVICE_COUNT=0

if [[ ! "$IMAGE" =~ ^ghcr\.io/rjuanluis/ayp-hrms:[0-9a-f]{40}$ ]]; then
  echo "Invalid immutable AyP HR image reference" >&2
  exit 2
fi

for path in \
  "$SECRETS_DIR/db_root_password" \
  "$SECRETS_DIR/admin_password" \
  "$ROOT_DIR/deploy/easypanel-compose.yml" \
  "$ROOT_DIR/deploy/easypanel_compose_api.js"; do
  [[ -s "$path" ]] || { echo "Missing required file: $path" >&2; exit 3; }
done

for volume in "$SITES_VOLUME" "$LOGS_VOLUME" ayp_hr_db_data ayp_hr_redis_queue_data ayp_hr_clamav_data; do
  docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null
done

compose() {
  local args=(-p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE")
  [[ -s "$COMPOSE_OVERRIDE" ]] && args+=(-f "$COMPOSE_OVERRIDE")
  docker compose "${args[@]}" "$@"
}

deploy_compose_source() {
  local source="$ROOT_DIR/deploy/easypanel-compose.yml"
  local helper="$ROOT_DIR/deploy/easypanel_compose_api.js"
  local panel_id candidate_path helper_path result deploy_rc started_epoch discovered_path
  panel_id="$(docker ps -q --filter "ancestor=$EASYPANEL_IMAGE" | head -1)"
  [[ -n "$panel_id" ]] || { echo "EasyPanel control-plane container not found" >&2; return 1; }
  docker inspect "$panel_id" --format '{{.Config.Image}}' | grep -q '^easypanel/easypanel:' || {
    echo "Unexpected EasyPanel control-plane container identity" >&2
    return 1
  }
  candidate_path="/tmp/ayp-hrms-compose-$$.yml"
  helper_path="/tmp/ayp-hrms-compose-api-$$.js"
  docker cp "$source" "$panel_id:$candidate_path"
  docker cp "$helper" "$panel_id:$helper_path"
  started_epoch="$(date +%s)"
  if result="$(docker exec "$panel_id" node "$helper_path" "$candidate_path")"; then
    deploy_rc=0
  else
    deploy_rc=$?
  fi
  docker exec "$panel_id" rm -f "$candidate_path" "$helper_path" >/dev/null
  COMPOSE_ROLLBACK_PATH="$(python3 -c 'import json,sys
for line in sys.stdin:
    try: data=json.loads(line)
    except json.JSONDecodeError: continue
    if data.get("rollbackPath"): print(data["rollbackPath"])' <<<"$result" | tail -1)"
  if [[ -z "$COMPOSE_ROLLBACK_PATH" ]]; then
    discovered_path="$(find /etc/easypanel/hermes-backups -mindepth 1 -maxdepth 1 -type d \
      -name 'ayp-hrms-compose-pre-*' -newermt "@$started_epoch" -print 2>/dev/null | sort | tail -1)"
    COMPOSE_ROLLBACK_PATH="$discovered_path"
  fi
  if (( deploy_rc != 0 )); then
    return "$deploy_rc"
  fi
  [[ "$result" == *'"canonicalSourceMatch":true'* ]] || {
    echo "EasyPanel canonical source verification failed" >&2
    return 1
  }
  echo "EasyPanel canonical Compose updated; rollback artifact: $COMPOSE_ROLLBACK_PATH"
}

container_id() {
  docker ps -q \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
    --filter "label=com.docker.compose.service=$1" | head -1
}

wait_for_compose() {
  local previous_backend_id="${1:-}"
  local expect_replacement="${2:-0}"
  local expected="${3:-11}"
  local stable_seconds=0
  local last_backend_id=""
  for _ in {1..120}; do
    local running db_id db_health clamav_id clamav_health current_backend_id replacement_ready
    running="$(docker ps \
      --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
      --format '{{.Label "com.docker.compose.service"}}' | sort -u | wc -l | tr -d ' ')"
    db_id="$(container_id db)"
    db_health=""
    if [[ -n "$db_id" ]]; then
      db_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$db_id" 2>/dev/null || true)"
    fi
    clamav_id="$(container_id clamav)"
    clamav_health=""
    if [[ -n "$clamav_id" ]]; then
      clamav_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$clamav_id" 2>/dev/null || true)"
    fi
    current_backend_id="$(container_id backend)"
    replacement_ready=1
    if [[ "$expect_replacement" == 1 && ( -z "$current_backend_id" || "$current_backend_id" == "$previous_backend_id" ) ]]; then
      replacement_ready=0
    fi
    if [[ "$running" == "$expected" && "$db_health" == "healthy" && "$clamav_health" == "healthy" && "$replacement_ready" == 1 ]] \
      && [[ -n "$current_backend_id" ]] \
      && docker exec "$current_backend_id" test -f "sites/apps.txt" 2>/dev/null; then
      if [[ "$current_backend_id" == "$last_backend_id" ]]; then
        stable_seconds=$((stable_seconds + 3))
      else
        last_backend_id="$current_backend_id"
        stable_seconds=0
      fi
      if (( stable_seconds >= 15 )); then
        return 0
      fi
    else
      stable_seconds=0
      last_backend_id="$current_backend_id"
    fi
    sleep 3
  done
  echo "EasyPanel Compose services did not become ready" >&2
  docker ps -a --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
    --format '{{.Names}}|{{.Status}}|{{.Image}}' >&2 || true
  return 1
}

rollback_pre_migration() {
  local reason="${1:-pre-migration deployment failure}"
  local source="$COMPOSE_ROLLBACK_PATH/source-before.yml"
  local helper="$ROOT_DIR/deploy/easypanel_compose_api.js"
  local panel_id helper_path result rollback_rc current_backend current_image
  ROLLBACK_ARMED=0
  set +e
  echo "Restoring pre-migration EasyPanel state after: $reason" >&2
  if [[ -n "$old_production_id" ]]; then
    docker tag "$old_production_id" "$PRODUCTION_IMAGE"
  fi
  panel_id="$(docker ps -q --filter "ancestor=$EASYPANEL_IMAGE" | head -1)"
  helper_path="/tmp/ayp-hrms-compose-api-rollback-$$.js"
  if [[ -z "$panel_id" || ! -s "$source" ]]; then
    echo "Rollback prerequisites are missing; manual recovery required" >&2
    set -e
    return 1
  fi
  docker cp "$helper" "$panel_id:$helper_path"
  if result="$(docker exec "$panel_id" node "$helper_path" "$source" --rollback)"; then
    rollback_rc=0
  else
    rollback_rc=$?
  fi
  docker exec "$panel_id" rm -f "$helper_path" >/dev/null 2>&1 || true
  if (( rollback_rc != 0 )) || [[ "$result" != *'"canonicalSourceMatch":true'* ]] || [[ "$result" != *'"managedSourceMatch":true'* ]]; then
    echo "Canonical EasyPanel rollback failed; manual recovery required" >&2
    set -e
    return 1
  fi
  if ! wait_for_compose "" 0 "$PREVIOUS_SERVICE_COUNT"; then
    echo "Rollback runtime did not recover all prior persistent services" >&2
    set -e
    return 1
  fi
  cmp -s "$source" "$COMPOSE_FILE" || {
    echo "Rollback managed Compose does not match prior canonical source" >&2
    set -e
    return 1
  }
  if [[ -n "$old_production_id" ]]; then
    current_backend="$(container_id backend)"
    current_image="$(docker inspect "$current_backend" --format '{{.Image}}' 2>/dev/null || true)"
    if [[ "$current_image" != "$old_production_id" ]]; then
      echo "Rollback backend image does not match the previous image ID" >&2
      set -e
      return 1
    fi
  fi
  echo "Pre-migration rollback verified at canonical, managed-file, and runtime layers" >&2
  set -e
}

on_deploy_error() {
  local rc="$1" line="$2"
  trap - ERR
  if [[ "$ROLLBACK_ARMED" == 1 && "$DEPLOY_PHASE" == "pre_migration" ]]; then
    rollback_pre_migration "exit $rc at line $line" || true
  else
    echo "Deployment stopped in phase $DEPLOY_PHASE at line $line; automatic image downgrade is disabled" >&2
  fi
  exit "$rc"
}

trap 'on_deploy_error $? $LINENO' ERR

backend_id="$(container_id backend)"
PREVIOUS_SERVICE_COUNT="$(docker ps \
  --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
  --format '{{.Label "com.docker.compose.service"}}' | sort -u | wc -l | tr -d ' ')"
(( PREVIOUS_SERVICE_COUNT > 0 )) || { echo "No existing EasyPanel services found" >&2; exit 4; }
if [[ -n "$backend_id" ]] && docker exec "$backend_id" test -f "sites/$SITE_NAME/site_config.json"; then
  echo "Creating pre-deploy backup"
  docker exec "$backend_id" bench --site "$SITE_NAME" backup --with-files
fi

echo "Preparing immutable image $IMAGE"
old_production_id="$(docker image inspect "$PRODUCTION_IMAGE" --format '{{.Id}}' 2>/dev/null || true)"
old_production_tags="$(docker image inspect "$PRODUCTION_IMAGE" --format '{{json .RepoTags}}' 2>/dev/null || true)"
[[ -n "$old_production_id" ]] || {
  echo "Previous production image ID is required for pre-migration rollback" >&2
  exit 5
}
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker pull "$IMAGE" >/dev/null
fi
new_image_id="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
expect_replacement=0
if [[ -z "$old_production_id" || "$old_production_id" != "$new_image_id" ]]; then
  expect_replacement=1
fi
docker tag "$IMAGE" "$PRODUCTION_IMAGE"

echo "Synchronizing canonical EasyPanel Compose source"
DEPLOY_PHASE="pre_migration"
ROLLBACK_ARMED=1
deploy_compose_source
wait_for_compose "$backend_id" "$expect_replacement"
cmp -s "$ROOT_DIR/deploy/easypanel-compose.yml" "$COMPOSE_FILE" || {
  echo "Managed EasyPanel Compose file does not match canonical repository source" >&2
  exit 8
}

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
  DEPLOY_PHASE="migration_started"
  ROLLBACK_ARMED=0
  compose exec -T backend bench --site "$SITE_NAME" migrate
else
  echo "Creating official site $SITE_NAME"
  DEPLOY_PHASE="migration_started"
  ROLLBACK_ARMED=0
  compose run --rm \
    -e "AYP_SITE_NAME=$SITE_NAME" \
    -v "$SECRETS_DIR:/run/ayp-secrets:ro" \
    -v "$HOOK_DIR/bootstrap_site.py:/opt/ayp/bootstrap_site.py:ro" \
    backend /home/frappe/frappe-bench/env/bin/python /opt/ayp/bootstrap_site.py
fi
DEPLOY_PHASE="post_migration"

compose exec -T backend bench --site "$SITE_NAME" set-config host_name "https://$SITE_NAME"

echo "Applying idempotent standard AyP configuration"
compose run --rm \
  -e "AYP_SITE_NAME=$SITE_NAME" \
  -v "$SECRETS_DIR:/run/ayp-secrets:ro" \
  -v "$HOOK_DIR/configure_standard.py:/opt/ayp/configure_standard.py:ro" \
  backend /home/frappe/frappe-bench/env/bin/python /opt/ayp/configure_standard.py

compose restart backend frontend websocket queue-short queue-long queue-documents scheduler >/dev/null
wait_for_compose


echo "Verifying dedicated candidate-document worker"
documents_id="$(container_id queue-documents)"
[[ -n "$documents_id" ]] || { echo "Candidate-document worker is not running" >&2; exit 8; }
documents_command="$(docker inspect "$documents_id" --format '{{json .Config.Cmd}}')"
[[ "$documents_command" == *'"documents"'* ]] || {
  echo "Candidate-document worker is not consuming the documents queue" >&2
  exit 8
}
compose exec -T backend python3 - <<'PY'
import json
from pathlib import Path
config = json.loads(Path("sites/common_site_config.json").read_text(encoding="utf-8"))
documents = config.get("workers", {}).get("documents")
if documents != {"timeout": 600, "background_workers": 1}:
    raise SystemExit(f"Invalid documents worker configuration: {documents!r}")
PY

echo "Verifying public recruitment routes"
frontend_id="$(container_id frontend)"
[[ -n "$frontend_id" ]] || { echo "Frontend container is not running" >&2; exit 6; }
canonical_form="$(
  docker exec "$frontend_id" curl -fsS --max-time 30 \
    -H "Host: $SITE_NAME" \
    "http://127.0.0.1:8080/empleos/solicitud/new"
)"
if [[ "$canonical_form" != *"Solicitud de empleo — Aro y Pedal"* ]] \
  || [[ "$canonical_form" != *"futuras oportunidades de Aro y Pedal"* ]]; then
  echo "Canonical recruitment form is missing the expected title or privacy consent" >&2
  exit 6
fi
legacy_status="$(
  docker exec "$frontend_id" curl -sS -o /dev/null -w '%{http_code}' --max-time 30 \
    -H "Host: $SITE_NAME" \
    "http://127.0.0.1:8080/job_application/new"
)"
case "$legacy_status" in
  403|404|410) ;;
  *)
    echo "Legacy recruitment route returned unexpected HTTP $legacy_status" >&2
    exit 7
    ;;
esac

python3 - "$IMAGE" "$old_production_id" "$old_production_tags" "$COMPOSE_ROLLBACK_PATH" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
receipt = {
    "status": "deployed",
    "operator": "easypanel-compose",
    "project": "web",
    "service": "ayp-hrms",
    "image": sys.argv[1],
    "previous_image_id": sys.argv[2] or None,
    "previous_image_tags": json.loads(sys.argv[3]) if sys.argv[3] else [],
    "compose_rollback": sys.argv[4] or None,
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
DEPLOY_PHASE="completed"
echo "EasyPanel deployment completed for $IMAGE"
