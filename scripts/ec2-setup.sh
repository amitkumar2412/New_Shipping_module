#!/bin/bash
# One-time EC2 setup for the shipping module (Amazon Linux 2023).
# Installs Python 3.11 and creates the service user and directories.
# Changes nothing that chem-os, the email platform or the Next.js apps use.
# Run as: sudo bash ec2-setup.sh
# The GitHub deploy workflow does the rest (code, venv, settings, systemd timer).
set -euo pipefail

APP_DIR="/opt/shipping-module"
ETC_DIR="/etc/shipping-module"
APP_USER="shipping"

echo "==> Installing Python 3.11 (alongside the system Python 3.9; nothing else changes)..."
dnf install -y python3.11

echo "==> Creating service user '$APP_USER' (no login shell, no home)..."
id "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin "$APP_USER"

echo "==> Creating directories..."
mkdir -p "$APP_DIR/releases"
chown -R root:"$APP_USER" "$APP_DIR"
chmod 755 "$APP_DIR" "$APP_DIR/releases"
mkdir -p "$ETC_DIR"
chown root:"$APP_USER" "$ETC_DIR"
chmod 750 "$ETC_DIR"

python3.11 --version
echo
echo "Done. Nothing is running yet: the first deploy installs the code and the timer."
echo "Optional check that this server can reach the database:"
echo "  timeout 5 bash -c '</dev/tcp/<DB_HOST>/5432' && echo reachable"
