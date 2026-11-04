#!/usr/bin/env bash
# First-boot setup of the site host (Amazon Linux 2023). Run once by the instance's user data, after it has
# written /opt/repolace/host.env. Kept deliberately small and in the repository (fetched at BOOTSTRAP_REF),
# so editing deploy.sh or compose.site.yml never changes the instance's user data.
set -euo pipefail

COMPOSE_VERSION="v2.35.1"
APP_DIR="/opt/repolace"
DATA_MOUNT="/var/lib/docker-volumes"

# shellcheck source=/dev/null
source "${APP_DIR}/host.env"   # DATA_VOLUME_ID, BOOTSTRAP_REPO, BOOTSTRAP_REF

dnf install -y docker jq xfsprogs

# --- Data volume: Postgres and every docker volume live on a separate, retained EBS volume ----------------
# The device appears as an NVMe disk whose by-id name carries the volume id without its dash.
device="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${DATA_VOLUME_ID/-/}"
for _ in $(seq 1 60); do
  [[ -e "${device}" ]] && break
  sleep 2
done
[[ -e "${device}" ]] || { echo "data volume ${DATA_VOLUME_ID} did not appear" >&2; exit 1; }
# A volume that already holds a filesystem (a replacement instance, a restored volume) is never formatted.
if ! blkid "${device}" >/dev/null 2>&1; then
  # an XFS label is at most 12 characters
  mkfs.xfs -L repolace-db "${device}"
fi
mkdir -p "${DATA_MOUNT}"
uuid="$(blkid -s UUID -o value "${device}")"
grep -q "${uuid}" /etc/fstab || echo "UUID=${uuid} ${DATA_MOUNT} xfs defaults,nofail 0 2" >> /etc/fstab
mount "${DATA_MOUNT}" || mount -a
mountpoint -q "${DATA_MOUNT}"

mkdir -p /etc/docker /etc/systemd/system/docker.service.d
printf '{\n  "data-root": "%s/docker"\n}\n' "${DATA_MOUNT}" > /etc/docker/daemon.json
printf '[Unit]\nRequiresMountsFor=%s\n' "${DATA_MOUNT}" > /etc/systemd/system/docker.service.d/10-data-volume.conf
systemctl daemon-reload
systemctl enable --now docker

# --- Compose plugin (pinned, checksum verified) -------------------------------------------------------------
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
expected="$(cut -d' ' -f1 "${tmp}/docker-compose.sha256")"
echo "${expected}  ${tmp}/docker-compose" | sha256sum --check --status
install -m 0755 "${tmp}/docker-compose" "${plugin_dir}/docker-compose"
rm -rf "${tmp}"

# --- Repository files needed before the first deploy (deploy.sh refreshes everything from the image) ----------
mkdir -p "${APP_DIR}/secrets" "${APP_DIR}/cloudflared"
chmod 0700 "${APP_DIR}/secrets"
raw="https://raw.githubusercontent.com/${BOOTSTRAP_REPO}/${BOOTSTRAP_REF}/infra/aws"
for f in deploy.sh backup.sh backup.service backup.timer; do
  curl -fsSL -o "${APP_DIR}/${f}" "${raw}/${f}"
done
chmod 0755 "${APP_DIR}/deploy.sh" "${APP_DIR}/backup.sh"
install -m 0644 "${APP_DIR}/backup.service" "${APP_DIR}/backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now backup.timer
