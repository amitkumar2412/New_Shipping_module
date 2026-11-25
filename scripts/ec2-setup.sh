#!/bin/bash
# One-time EC2 setup for the shipping module (Amazon Linux 2023).
# Installs Docker + the compose and buildx plugins and creates the app directory.
# Run as: sudo bash ec2-setup.sh
# The GitHub deploy workflow does the rest (code upload, .env, docker compose up).
set -euo pipefail

APP_DIR="/opt/shipping-module"
APP_USER="ec2-user"
PLUGIN_DIR="/usr/local/lib/docker/cli-plugins"
ARCH="$(uname -m)"                      # x86_64 or aarch64
case "$ARCH" in
  x86_64)  GOARCH=amd64 ;;
  aarch64) GOARCH=arm64 ;;
  *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

echo "==> Installing Docker..."
dnf install -y docker
systemctl enable --now docker
usermod -aG docker "$APP_USER"

echo "==> Installing docker compose plugin..."
mkdir -p "$PLUGIN_DIR"
curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$ARCH" \
  -o "$PLUGIN_DIR/docker-compose"
chmod +x "$PLUGIN_DIR/docker-compose"

echo "==> Installing docker buildx plugin..."
BUILDX_VERSION="$(curl -fsSL https://api.github.com/repos/docker/buildx/releases/latest | grep -m1 '"tag_name"' | cut -d'"' -f4)"
curl -fsSL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-${GOARCH}" \
  -o "$PLUGIN_DIR/docker-buildx"
chmod +x "$PLUGIN_DIR/docker-buildx"

echo "==> Creating app directory..."
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"

docker --version
docker compose version
docker buildx version

echo
echo "Done. Log out and back in (or run 'newgrp docker') so $APP_USER can use docker without sudo."
echo "Before the first deploy, check that this server can reach the database:"
echo "  curl -v telnet://<DB_HOST>:5432   (should connect; Ctrl+C to exit)"
