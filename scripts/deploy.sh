#!/bin/bash
# Installs a new release on the server. Run by the GitHub deploy workflow:
#   sudo bash deploy.sh <release.tar.gz> <shipping.env>
# Steps: unpack into a new release dir -> build its venv -> install settings ->
# migrate the schema -> switch the `current` symlink -> install/enable the timer ->
# keep the 2 newest releases (current + one to roll back to). A scrape already in progress keeps running on the
# old release; the next timer run uses the new one.
set -euo pipefail

TARBALL="$1"
NEW_ENV="$2"
APP_DIR="/opt/shipping-module"
ETC_DIR="/etc/shipping-module"
APP_USER="shipping"
RELEASE="$APP_DIR/releases/$(date -u +%Y%m%dT%H%M%SZ)"

[ -d "$APP_DIR/releases" ] && id "$APP_USER" >/dev/null 2>&1 \
  || { echo "Run scripts/ec2-setup.sh on this server first"; exit 1; }

echo "==> Unpacking release into $RELEASE"
mkdir -p "$RELEASE"
tar -xzf "$TARBALL" -C "$RELEASE"

echo "==> Building virtualenv"
python3.11 -m venv "$RELEASE/venv"
"$RELEASE/venv/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check -r "$RELEASE/requirements.txt"
# Pre-compile: at run time the release is read-only to the service.
"$RELEASE/venv/bin/python" -m compileall -q "$RELEASE" -x '/venv/' >/dev/null
chown -R root:"$APP_USER" "$RELEASE"
chmod -R u=rwX,g=rX,o=rX "$RELEASE"

echo "==> Installing settings"
install -m 640 -o root -g "$APP_USER" "$NEW_ENV" "$ETC_DIR/shipping.env"
rm -f "$NEW_ENV"

run_as_app() {
  cd /tmp && sudo -u "$APP_USER" SHIPPING_ENV_FILE="$ETC_DIR/shipping.env" "$RELEASE/venv/bin/python" "$RELEASE/main.py" "$@"
}

echo "==> Migrating schema (also proves the database is reachable)"
run_as_app migrate

echo "==> Switching current -> $RELEASE"
ln -sfn "$RELEASE" "$APP_DIR/current.tmp"
mv -T "$APP_DIR/current.tmp" "$APP_DIR/current"

echo "==> Installing systemd units and the 'shipping' command"
install -m 644 "$RELEASE/deploy/shipping-scrape.service" /etc/systemd/system/shipping-scrape.service
install -m 644 "$RELEASE/deploy/shipping-scrape.timer" /etc/systemd/system/shipping-scrape.timer
install -m 755 "$RELEASE/deploy/shipping" /usr/local/bin/shipping
systemctl daemon-reload
systemctl enable --now shipping-scrape.timer

echo "==> Removing old releases (keeping 2)"
ls -1dt "$APP_DIR"/releases/*/ | tail -n +3 | while read -r old; do rm -rf "$old"; done

echo "==> Status"
run_as_app status
systemctl list-timers shipping-scrape.timer --no-pager
df -h / | tail -1
