#!/usr/bin/env bash
# First-boot setup of the APP host (Ubuntu 24.04, x86_64). Run once as root by the instance's user data, after
# it has written /opt/repolace/host.env. Kept small and in the repository (fetched at BOOTSTRAP_REF), so editing
# deploy-app.sh or compose.app.yml never changes the instance's user data.
#
# What it builds, in this order (every step is idempotent, so a re-run after a failure is safe):
#   1. packages, Docker's apt repository (rootless extras, compose plugin), AWS CLI, nftables
#   2. the unprivileged user `repolace` that owns the rootless daemon, with sub-ids and lingering
#   3. the three extra EBS volumes, found by id, formatted only when empty, mounted before the user manager
#   4. cgroup v2 delegation of cpu/memory/pids to that user (without it the sandbox's limits are ignored)
#   5. the firewall rule that keeps that user (and so every rootless container) away from the instance
#      metadata service. The instance's hop limit of 1 does NOT do this: see the README, "The app host".
#   6. the rootless daemon itself, and proof of steps 5 and 4 against a real container
# Only after all of that does it write /opt/repolace/bootstrap.ok, which deploy-app.sh requires.
#
# NOT RUN YET on a real instance. Read it as a reviewed draft, and expect to fix a detail on the first boot.
set -euo pipefail

APP_DIR="/opt/repolace"
RUN_USER="repolace"
DATA_MOUNT="/var/lib/repolace-data"
WORKSPACE_MOUNT="/var/tmp/repolace-workspaces"

# shellcheck source=/dev/null
source "${APP_DIR}/host.env"   # DATA_VOLUME_ID, DOCKER_VOLUME_ID, WORKSPACE_VOLUME_ID, BOOTSTRAP_REPO, BOOTSTRAP_REF

log() { printf '[bootstrap-app %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

[[ "$(uname -m)" == "x86_64" ]] || { echo "this host is x86_64 only (the images are built for amd64)" >&2; exit 1; }
[[ "$(stat -fc %T /sys/fs/cgroup)" == "cgroup2fs" ]] || { echo "cgroup v2 is required" >&2; exit 1; }

# --- 1. Packages ----------------------------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl jq uidmap nftables iptables dbus-user-session e2fsprogs slirp4netns

# Docker's own repository, set up the way Docker's documentation does it. The rootless extras ship the
# AppArmor profile that lets rootlesskit create a user namespace on Ubuntu 24.04.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-ce-rootless-extras
# The rootful daemon must never run: its socket is root-equivalent (CLAUDE.md, "Deployment requirements").
# Masked, not just disabled, so a later package upgrade cannot switch it back on.
systemctl mask --now docker.service docker.socket containerd.service 2>/dev/null || true

# The AWS CLI (not in Ubuntu's archive as v2). Used by deploy-app.sh, which runs as root, never as ${RUN_USER}.
if ! command -v aws >/dev/null; then
  snap wait system seed.loaded   # snapd may still be seeding on a first boot
  snap install aws-cli --classic
  ln -sf /snap/bin/aws /usr/local/bin/aws   # the SSM agent's PATH may not include /snap/bin
fi

# --- 2. The user that owns the daemon -------------------------------------------------------------------------
if ! id "${RUN_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${RUN_USER}"
fi
uid="$(id -u "${RUN_USER}")"
# Rootless Docker needs at least 65,536 sub-ids. useradd usually allocates them; make sure.
grep -q "^${RUN_USER}:" /etc/subuid || usermod --add-subuids 300000-365535 "${RUN_USER}"
grep -q "^${RUN_USER}:" /etc/subgid || usermod --add-subgids 300000-365535 "${RUN_USER}"
home="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
DOCKER_MOUNT="${home}/.local/share/docker"

# --- 3. Volumes -----------------------------------------------------------------------------------------------
# Each appears as an NVMe disk whose by-id name carries the volume id without its dash.
prepare_volume() {  # prepare_volume <volume id> <mount point> <label>
  local vol="$1" mount_point="$2" label="$3" device uuid
  device="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${vol/-/}"
  for _ in $(seq 1 90); do
    [[ -e "${device}" ]] && break
    sleep 2
  done
  [[ -e "${device}" ]] || { echo "volume ${vol} did not appear" >&2; exit 1; }
  # A volume that already holds a filesystem (a replacement instance, a restored snapshot) is never formatted.
  if ! blkid "${device}" >/dev/null 2>&1; then
    mkfs.ext4 -q -L "${label}" "${device}"   # an ext4 label is at most 16 characters
  fi
  install -d -m 0755 "${mount_point}"
  uuid="$(blkid -s UUID -o value "${device}")"
  grep -q "${uuid}" /etc/fstab || echo "UUID=${uuid} ${mount_point} ext4 defaults,nofail 0 2" >> /etc/fstab
  mountpoint -q "${mount_point}" || mount "${mount_point}"
  mountpoint -q "${mount_point}"
  chown "${RUN_USER}:${RUN_USER}" "${mount_point}"
}
install -d -o "${RUN_USER}" -g "${RUN_USER}" "${home}/.local" "${home}/.local/share"
prepare_volume "${DATA_VOLUME_ID}" "${DATA_MOUNT}" repolace-data
prepare_volume "${DOCKER_VOLUME_ID}" "${DOCKER_MOUNT}" repolace-docker
prepare_volume "${WORKSPACE_VOLUME_ID}" "${WORKSPACE_MOUNT}" repolace-work
for d in postgres redis rabbitmq; do
  install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 0700 "${DATA_MOUNT}/${d}"
done

# The rootless daemon starts with the user manager. If a volume failed to mount (fstab says `nofail`), the
# daemon would silently use the 20 GiB root disk, so the user manager waits for all three.
install -d /etc/systemd/system/user@.service.d "/etc/systemd/system/user@${uid}.service.d"
# Also: no metadata block, no user manager, so no rootless daemon (the block can fail or be flushed at boot).
# IPAddressDeny is a second, independent layer (cgroup BPF on everything the user manager runs); it is
# defence in depth and not relied on, because it is unverified here.
printf '[Unit]\nRequiresMountsFor=%s %s %s\nRequires=repolace-imds-block.service\nAfter=repolace-imds-block.service\n\n[Service]\nIPAddressDeny=169.254.169.254 fd00:ec2::254\n' \
  "${DATA_MOUNT}" "${DOCKER_MOUNT}" "${WORKSPACE_MOUNT}" > "/etc/systemd/system/user@${uid}.service.d/10-volumes.conf"

# --- 4. cgroup v2 delegation (Docker's documented drop-in) ----------------------------------------------------
printf '[Service]\nDelegate=cpu cpuset io memory pids\n' > /etc/systemd/system/user@.service.d/delegate.conf

# --- 5. Instance metadata: drop it for ${RUN_USER} --------------------------------------------------------------
# Rootless Docker's user-space networking re-originates every container connection from a process owned by
# ${RUN_USER}, in the host's network namespace. The IMDSv2 hop limit never sees those packets, so without this
# rule any task container could read the instance role's credentials. The match is on the owning uid, so root
# (deploy-app.sh, the SSM agent) is unaffected. 169.254.169.253 (the VPC resolver) is deliberately not blocked.
install -d /etc/repolace
cat > /etc/repolace/imds-block.nft <<EOF
table inet repolace_imds
delete table inet repolace_imds
table inet repolace_imds {
  chain output {
    type filter hook output priority 0; policy accept;
    meta skuid ${uid} ip daddr 169.254.169.254 counter drop
    meta skuid ${uid} ip6 daddr fd00:ec2::254 counter drop
  }
}
EOF
cat > /etc/systemd/system/repolace-imds-block.service <<EOF
[Unit]
Description=Keep the rootless Docker user away from the instance metadata service
Before=user@${uid}.service network-pre.target
Wants=network-pre.target
# nftables.service (masked below) would otherwise `flush ruleset` after us.
After=nftables.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/repolace/imds-block.nft

[Install]
WantedBy=multi-user.target
EOF
systemctl mask nftables.service
systemctl daemon-reload
systemctl enable repolace-imds-block.service
systemctl restart repolace-imds-block.service   # a re-run with a changed rule file must reload it
rules="$(nft list table inet repolace_imds)"
grep -q "skuid ${uid} ip daddr 169.254.169.254" <<<"${rules}" \
  || { echo "the metadata block is not loaded" >&2; exit 1; }

# --- 6. The rootless daemon -----------------------------------------------------------------------------------
loginctl enable-linger "${RUN_USER}"
systemctl restart "user@${uid}.service"   # picks up the delegation and mount requirements even on a re-run
for _ in $(seq 1 30); do
  [[ -S "/run/user/${uid}/bus" ]] && break
  sleep 1
done
[[ -S "/run/user/${uid}/bus" ]] || { echo "no user session bus for ${RUN_USER}" >&2; exit 1; }

as_user() {
  sudo -u "${RUN_USER}" env "XDG_RUNTIME_DIR=/run/user/${uid}" \
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus" "DOCKER_HOST=unix:///run/user/${uid}/docker.sock" \
    "PATH=/usr/bin:/usr/sbin:/bin:/sbin" "$@"
}
if ! as_user systemctl --user is-active --quiet docker.service; then
  as_user dockerd-rootless-setuptool.sh install
fi
as_user systemctl --user enable --now docker.service
for _ in $(seq 1 60); do
  as_user docker info >/dev/null 2>&1 && break
  sleep 2
done
as_user docker info >/dev/null 2>&1 || { echo "the rootless daemon did not come up" >&2; exit 1; }

# Proof, not belief. Each check runs inside a real container on the rootless daemon.
info="$(as_user docker info --format '{{.SecurityOptions}} {{.CgroupVersion}} {{.DockerRootDir}}')"
[[ "${info}" == *rootless* ]] || { echo "the daemon is not rootless: ${info}" >&2; exit 1; }
[[ "${info}" == *"${DOCKER_MOUNT}"* ]] || { echo "the daemon's data root is not ${DOCKER_MOUNT}: ${info}" >&2; exit 1; }
as_user docker pull -q busybox:1.36 >/dev/null

# (a) The metadata service. IMDSv2 answers an unauthenticated GET with 401, so a plain GET cannot tell "blocked"
# from "reachable": ask for a session token instead, which only a reachable service hands out. A positive
# control proves the container has network at all, and the rule's own packet counter proves the block is what
# stopped the request (that the connection really is made as uid ${uid}), not a broken container.
as_user docker run --rm busybox:1.36 wget -q -T 6 -O /dev/null http://archive.ubuntu.com/ubuntu/ \
  || { echo "a container has no outbound network; the metadata check would prove nothing" >&2; exit 1; }
token="$(as_user docker run --rm busybox:1.36 wget -q -T 4 -O - --post-data='' \
  --header 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token 2>/dev/null || true)"
if [[ -n "${token}" ]]; then
  echo "a container reached the instance metadata service: the host is NOT safe to run tasks" >&2
  exit 1
fi
rules="$(nft list chain inet repolace_imds output)"
grep -Eq "skuid ${uid} ip daddr 169\.254\.169\.254 counter packets [1-9]" <<<"${rules}" \
  || { echo "the metadata rule never matched: container traffic does not originate as uid ${uid}" >&2; exit 1; }
log "a container cannot reach the metadata service (and the rule counted the attempt)"

# (b) cgroup limits. Without delegation rootless Docker warns and drops the flags, and `run` still exits 0, so
# read the limits back from inside the container.
limits="$(as_user docker run --rm --memory 64m --pids-limit 32 busybox:1.36 \
  sh -c 'cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max' | tr '\n' ' ')"
[[ "${limits}" == "67108864 32 " ]] || { echo "cgroup limits are not enforced (got: ${limits})" >&2; exit 1; }
log "cgroup limits are enforced"

# --- Files deploy-app.sh needs before its first run (it refreshes the rest from the image) --------------------
install -d -m 0700 "${APP_DIR}/secrets"
install -d -m 0755 "${APP_DIR}/cloudflared"
raw="https://raw.githubusercontent.com/${BOOTSTRAP_REPO}/${BOOTSTRAP_REF}/infra/aws"
curl -fsSL -o "${APP_DIR}/deploy-app.sh" "${raw}/deploy-app.sh"
chmod 0755 "${APP_DIR}/deploy-app.sh"
printf 'uid=%s\n' "${uid}" > "${APP_DIR}/bootstrap.ok"
log "done"
