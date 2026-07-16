#!/usr/bin/env bash
set -Eeuo pipefail

SITE_NAME=hr.aroypedal.com
COMPOSE_PROJECT=web_ayp-hrms
SITES_VOLUME=ayp_hr_sites
ARCHIVE_DIR=/opt/ayp-hr/backups

BACKEND_ID="$(docker ps -q \
  --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
  --filter "label=com.docker.compose.service=backend" | head -1)"
if [[ -z "$BACKEND_ID" ]]; then
  echo "AyP HR EasyPanel backend is not running; backup skipped"
  exit 0
fi

if ! docker exec "$BACKEND_ID" test -f "sites/$SITE_NAME/site_config.json"; then
  echo "Site $SITE_NAME does not exist; backup skipped"
  exit 0
fi

echo "Starting AyP HR backup at $(date -u +%FT%TZ)"
docker exec "$BACKEND_ID" bench --site "$SITE_NAME" backup --with-files --compress

install -d -m 750 "$ARCHIVE_DIR"
docker run --rm \
  -v "$SITES_VOLUME:/sites:ro" \
  -v "$ARCHIVE_DIR:/archive" \
  alpine:3.22 sh -ec '
    cp -a /sites/hr.aroypedal.com/private/backups/. /archive/
    find /archive -type f -mtime +14 -delete
  '

echo "AyP HR backup completed at $(date -u +%FT%TZ)"
