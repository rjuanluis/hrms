#!/usr/bin/env bash
set -Eeuo pipefail

SITE_NAME=hr.aroypedal.com
APP_NETWORK=ayp_hr_net
SITES_VOLUME=ayp_hr_sites
LOGS_VOLUME=ayp_hr_logs
ARCHIVE_DIR=/opt/ayp-hr/backups

IMAGE="$(docker service inspect ayphr_backend --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}' 2>/dev/null || true)"
if [[ -z "$IMAGE" ]]; then
  echo "AyP HR backend is not deployed; backup skipped"
  exit 0
fi

if ! docker run --rm -v "$SITES_VOLUME:/home/frappe/frappe-bench/sites" "$IMAGE" \
    bash -ec "test -f sites/$SITE_NAME/site_config.json"; then
  echo "Site $SITE_NAME does not exist; backup skipped"
  exit 0
fi

echo "Starting AyP HR backup at $(date -u +%FT%TZ)"
docker run --rm --network "$APP_NETWORK" \
  -v "$SITES_VOLUME:/home/frappe/frappe-bench/sites" \
  -v "$LOGS_VOLUME:/home/frappe/frappe-bench/logs" \
  "$IMAGE" bench --site "$SITE_NAME" backup --with-files --compress

install -d -m 750 "$ARCHIVE_DIR"
docker run --rm \
  -v "$SITES_VOLUME:/sites:ro" \
  -v "$ARCHIVE_DIR:/archive" \
  alpine:3.22 sh -ec '
    cp -a /sites/hr.aroypedal.com/private/backups/. /archive/
    find /archive -type f -mtime +14 -delete
  '

echo "AyP HR backup completed at $(date -u +%FT%TZ)"
