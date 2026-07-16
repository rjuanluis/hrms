#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${EUID}" -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

ROOT_DIR=/opt/ayp-hr
SOURCE_DIR="$ROOT_DIR/source"
SECRETS_DIR="$ROOT_DIR/secrets"
BACKUP_DIR="$ROOT_DIR/backups"
DEPLOY_USER=aypdeploy
REPO_URL=https://github.com/rjuanluis/hrms.git
BRANCH=ayp-production
PUBLIC_KEY_FILE="${AYP_DEPLOY_PUBLIC_KEY_FILE:-}"

if [[ -z "$PUBLIC_KEY_FILE" || ! -s "$PUBLIC_KEY_FILE" ]]; then
  echo "AYP_DEPLOY_PUBLIC_KEY_FILE must point to the restricted deploy public key" >&2
  exit 2
fi

if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

install -d -m 775 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$ROOT_DIR" "$BACKUP_DIR"
install -d -m 750 -o root -g "$DEPLOY_USER" "$SECRETS_DIR"

if [[ ! -s "$SECRETS_DIR/db_root_password" ]]; then
  umask 0077
  openssl rand -base64 48 > "$SECRETS_DIR/db_root_password"
fi
if [[ ! -s "$SECRETS_DIR/admin_password" ]]; then
  umask 0077
  openssl rand -base64 36 > "$SECRETS_DIR/admin_password"
fi
chown root:"$DEPLOY_USER" "$SECRETS_DIR/db_root_password" "$SECRETS_DIR/admin_password"
chmod 640 "$SECRETS_DIR/db_root_password" "$SECRETS_DIR/admin_password"

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  sudo -u "$DEPLOY_USER" git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$SOURCE_DIR"
else
  sudo -u "$DEPLOY_USER" git -C "$SOURCE_DIR" fetch --depth=1 origin "$BRANCH"
  sudo -u "$DEPLOY_USER" git -C "$SOURCE_DIR" reset --hard "origin/$BRANCH"
fi

install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
KEY="$(tr -d '\r\n' < "$PUBLIC_KEY_FILE")"
case "$KEY" in
  ssh-ed25519\ *|sk-ssh-ed25519@openssh.com\ *) ;;
  *) echo "Only an Ed25519 public key is accepted" >&2; exit 3 ;;
esac
printf 'restrict,command="%s/deploy/host-deploy-wrapper.sh" %s\n' "$SOURCE_DIR" "$KEY" \
  > "/home/$DEPLOY_USER/.ssh/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh/authorized_keys"
chmod 600 "/home/$DEPLOY_USER/.ssh/authorized_keys"

if ! docker network inspect ayp_hr_net >/dev/null 2>&1; then
  docker network create --driver overlay --attachable ayp_hr_net >/dev/null
fi
for volume in ayp_hr_sites ayp_hr_logs ayp_hr_db_data ayp_hr_redis_queue_data; do
  docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null
done
docker secret inspect ayp_hr_db_root_password >/dev/null 2>&1 || \
  docker secret create ayp_hr_db_root_password "$SECRETS_DIR/db_root_password" >/dev/null

install -m 644 -o root -g root "$SOURCE_DIR/deploy/traefik-ayp-hr.yaml" \
  /etc/easypanel/traefik/config/ayp-hr.yaml

if ! swapon --show=NAME --noheadings | grep -qx /swapfile-ayphr; then
  if [[ ! -f /swapfile-ayphr ]]; then
    fallocate -l 4G /swapfile-ayphr
    chmod 600 /swapfile-ayphr
    mkswap /swapfile-ayphr >/dev/null
  fi
  swapon /swapfile-ayphr
fi
if ! grep -q '^/swapfile-ayphr ' /etc/fstab; then
  printf '/swapfile-ayphr none swap sw 0 0\n' >> /etc/fstab
fi

cat > /etc/cron.d/ayp-hr-backup <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 */6 * * * root /opt/ayp-hr/source/deploy/backup.sh >> /var/log/ayp-hr-backup.log 2>&1
CRON
chmod 644 /etc/cron.d/ayp-hr-backup

python3 - <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
receipt = {
    "status": "provisioned",
    "site": "hr.aroypedal.com",
    "repository": "https://github.com/rjuanluis/hrms",
    "branch": "ayp-production",
    "provisioned_at": datetime.now(timezone.utc).isoformat(),
}
p = Path("/opt/ayp-hr/provision-receipt.json")
p.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
p.chmod(0o644)
PY

echo "AyP HR host provisioning complete; secrets were not printed"
