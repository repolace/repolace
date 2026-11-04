#!/usr/bin/env bash
# First-boot setup of the site host (Amazon Linux 2023). Run once by the instance's user data after it has
# extracted the infra/aws files and written /opt/repolace/host.env.
set -euo pipefail

COMPOSE_VERSION="v2.35.1"

dnf install -y docker jq
systemctl enable --now docker

case "$(uname -m)" in
  aarch64) arch="aarch64" ;;
  x86_64) arch="x86_64" ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

plugin_dir="/usr/local/lib/docker/cli-plugins"
mkdir -p "${plugin_dir}"
base="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}"
tmp="$(mktemp -d)"
curl -fsSL -o "${tmp}/docker-compose" "${base}/docker-compose-linux-${arch}"
curl -fsSL -o "${tmp}/docker-compose.sha256" "${base}/docker-compose-linux-${arch}.sha256"
# The .sha256 file names the release asset; compare the hash only.
expected="$(cut -d' ' -f1 "${tmp}/docker-compose.sha256")"
echo "${expected}  ${tmp}/docker-compose" | sha256sum --check --status
install -m 0755 "${tmp}/docker-compose" "${plugin_dir}/docker-compose"
rm -rf "${tmp}"

cd /opt/repolace
mkdir -p secrets cloudflared
chmod 0700 secrets
chmod 0755 deploy.sh backup.sh
install -m 0644 backup.service backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now backup.timer
