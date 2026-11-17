#!/usr/bin/env bash
# Deploy one commit of the app (API, task worker, maintenance worker, stores, tunnel) on this host. Run as root
# through SSM (AWS-RunShellScript) by .github/workflows/deploy-app.yml:   /opt/repolace/deploy-app.sh <40-hex sha>
#
# Its output goes to the GitHub Actions log, so it never prints a secret or a rendered env file.
# Order, and why:
#   1. pull both images at the sha, refresh this directory's files from the new API image, re-run once
#   2. render secrets from Secrets Manager (api and worker env files are separate: provider keys never reach the API)
#   3. start the stores, then run the migration ALONE; a failed migration stops the deploy with the old version
#      still serving. Migrations must stay backward compatible with the previous image (expand, then contract),
#      because the rollback below restarts the previous image against the already migrated schema.
#   4. start everything, wait for health, and PROVE the version: the API's /healthz carries the build sha, read
#      locally and through the public URL; the workers' containers must run the expected image and be healthy.
#   5. on failure after the new images started, roll back to the previous sha and exit non-zero.
# Operators: run docker as root with DOCKER_HOST=unix:///run/user/<uid of repolace>/docker.sock (see as_user below).
set -euo pipefail
set +x
umask 077
# SSM's PATH may not include /snap/bin (the AWS CLI).
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/snap/bin"
# Two deploys (a manual SSM run and the workflow, or a retry after a timeout) must never interleave.
exec 9>/run/repolace-deploy.lock
flock -n 9 || { echo "another deploy is running" >&2; exit 1; }

APP_DIR="/opt/repolace"
RUN_USER="repolace"
# shellcheck source=/dev/null
source "${APP_DIR}/host.env"   # AWS_REGION, API_ECR_REPO_URI, WORKER_ECR_REPO_URI, API_ENV_SECRET, WORKER_ENV_SECRET,
                               # TUNNEL_SECRET, PUBLIC_URL

log() { printf '[deploy-app %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

sha="${1:-}"
if [[ ! "${sha}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: deploy-app.sh <40-hex git sha>" >&2
  exit 2
fi
[[ -f "${APP_DIR}/bootstrap.ok" ]] || { echo "bootstrap-app.sh has not finished on this host" >&2; exit 1; }
# shellcheck source=/dev/null
source "${APP_DIR}/bootstrap.ok"   # uid=<uid of ${RUN_USER}>
rules="$(nft list table inet repolace_imds 2>/dev/null || true)"
grep -q "skuid ${uid} ip daddr 169.254.169.254" <<<"${rules}" \
  || { echo "the instance-metadata block is not loaded; refusing to start tasks" >&2; exit 1; }

DATA_DIR="/var/lib/repolace-data"
WORKSPACE_PARENT_DIR="/var/tmp/repolace-workspaces"
for m in "${DATA_DIR}" "${WORKSPACE_PARENT_DIR}" "/home/${RUN_USER}/.local/share/docker"; do
  mountpoint -q "${m}" || { echo "${m} is not mounted" >&2; exit 1; }
done

cd "${APP_DIR}"
api_image="${API_ECR_REPO_URI}:${sha}"
worker_image="${WORKER_ECR_REPO_URI}:${sha}"
previous_sha=""
[[ -f current_sha ]] && previous_sha="$(cat current_sha)"

# Docker commands run as ROOT against the rootless daemon's socket (root bypasses the socket's file permissions;
# containers it starts are still the unprivileged user's). That keeps every secret file root-only: the task
# worker holds this same socket as the user `repolace`, and anything that took over that user could otherwise
# read api.env, worker.env and the store passwords by bind-mounting /opt/repolace. Only the tunnel credentials
# stay user-owned, because cloudflared reads them as that user.
as_user() {
  env "DOCKER_HOST=unix:///run/user/${uid}/docker.sock" "API_IMAGE=${API_IMAGE:-}" "WORKER_IMAGE=${WORKER_IMAGE:-}" "$@"
}
compose() {
  as_user docker compose -f "${APP_DIR}/compose.app.yml" --project-directory "${APP_DIR}" \
    --env-file "${APP_DIR}/stack.env" "$@"
}

# --- 1. Pull the images ---------------------------------------------------------------------------------------
registry="${API_ECR_REPO_URI%%/*}"
aws ecr get-login-password --region "${AWS_REGION}" \
  | as_user docker login --username AWS --password-stdin "${registry}" >/dev/null
log "pulling ${sha}"
as_user docker pull --quiet "${api_image}" >/dev/null
as_user docker pull --quiet "${worker_image}" >/dev/null

# --- 2. Refresh this directory's files from the image, then re-run the new deploy-app.sh ----------------------
if [[ -z "${REPOLACE_DEPLOY_REEXEC:-}" ]]; then
  stage="$(mktemp -d)"
  cid="$(as_user docker create "${api_image}")"
  trap 'as_user docker rm -f "${cid}" >/dev/null 2>&1 || true; rm -rf "${stage}"' EXIT
  # Streamed as a tar and unpacked here, so the files land owned by this process (a client-side `docker cp`
  # into the 0700 temporary directory would be refused).
  as_user docker cp "${cid}:/repo/infra/aws/." - | tar -x -C "${stage}"
  as_user docker cp "${cid}:/repo/infra/rabbitmq.conf" - | tar -x -C "${stage}"
  as_user docker rm "${cid}" >/dev/null
  install -m 0644 "${stage}/compose.app.yml" "${APP_DIR}/compose.app.yml.new" && mv "${APP_DIR}/compose.app.yml.new" "${APP_DIR}/compose.app.yml"
  install -m 0644 "${stage}/rabbitmq.conf" "${APP_DIR}/rabbitmq.conf"
  install -m 0755 "${stage}/deploy-app.sh" "${APP_DIR}/deploy-app.sh.new"
  mv "${APP_DIR}/deploy-app.sh.new" "${APP_DIR}/deploy-app.sh"
  rm -rf "${stage}"
  trap - EXIT
  export REPOLACE_DEPLOY_REEXEC=1
  exec "${APP_DIR}/deploy-app.sh" "${sha}"
fi

# --- 3. Render secrets (never printed) ------------------------------------------------------------------------
fetch_secret() {  # fetch_secret <secret-id> <destination file> <owner:group> <mode>   (validated before it is moved in)
  local tmp="$2.tmp"
  aws secretsmanager get-secret-value --region "${AWS_REGION}" --secret-id "$1" \
    --query SecretString --output text > "${tmp}"
  chown "$3" "${tmp}"
  chmod "$4" "${tmp}"
}
# An env file must be KEY=value lines, comments and blanks. Anything else is reported by line number only, never by
# content: a compose parse error can quote the offending line, which would put a secret in the Actions log.
check_env_file() {  # check_env_file <file> <label>
  local bad
  bad="$(grep -nEv '^[A-Za-z_][A-Za-z0-9_]*=|^[[:space:]]*#|^[[:space:]]*$' "$1" | cut -d: -f1 | head -5 | tr '\n' ' ' || true)"
  if [[ -n "${bad}" ]]; then
    log "$2 is not a plain KEY=value file (line numbers: ${bad})"
    return 1
  fi
}
install -d -m 0755 "${APP_DIR}/cloudflared"
fetch_secret "${API_ENV_SECRET}" "${APP_DIR}/api.env" "root:root" 0600
fetch_secret "${WORKER_ENV_SECRET}" "${APP_DIR}/worker.env" "root:root" 0600
fetch_secret "${TUNNEL_SECRET}" "${APP_DIR}/cloudflared/credentials.json" "${RUN_USER}:${RUN_USER}" 0400
check_env_file "${APP_DIR}/api.env.tmp" "${API_ENV_SECRET}"
check_env_file "${APP_DIR}/worker.env.tmp" "${WORKER_ENV_SECRET}"
# Provider and cloud credentials belong in the worker's secret only. One in the API's secret would put it in the
# process that serves the web, so the deploy stops (before the file is moved into place) rather than start it.
if grep -Eiq '^(export[[:space:]]+)?[A-Za-z_]*(ANTHROPIC|OPENAI|GEMINI|MINIMAX|OPENROUTER|NVIDIA|GOOGLE|VERTEX|LITELLM|BEDROCK|AWS_)[A-Za-z_]*[[:space:]]*=[[:space:]]*[^[:space:]]' "${APP_DIR}/api.env.tmp"; then
  log "${API_ENV_SECRET} holds a model provider or cloud credential; move it to ${WORKER_ENV_SECRET}"
  rm -f "${APP_DIR}/api.env.tmp" "${APP_DIR}/worker.env.tmp" "${APP_DIR}/cloudflared/credentials.json.tmp"
  exit 1
fi
mv "${APP_DIR}/api.env.tmp" "${APP_DIR}/api.env"
mv "${APP_DIR}/worker.env.tmp" "${APP_DIR}/worker.env"
mv "${APP_DIR}/cloudflared/credentials.json.tmp" "${APP_DIR}/cloudflared/credentials.json"

# The tunnel's id is in its credentials file; the hostname is the public URL's. Anything else gets a 404.
tunnel_id="$(jq -r '.TunnelID // empty' "${APP_DIR}/cloudflared/credentials.json")"
host="${PUBLIC_URL#*://}"; host="${host%%/*}"
[[ -n "${tunnel_id}" && -n "${host}" ]] || { log "tunnel credentials or PUBLIC_URL unusable"; exit 1; }
printf 'tunnel: %s\ncredentials-file: /etc/cloudflared/credentials.json\ningress:\n  - hostname: %s\n    service: http://api:8000\n  - service: http_status:404\n' \
  "${tunnel_id}" "${host}" > "${APP_DIR}/cloudflared/config.yml"
chmod 0644 "${APP_DIR}/cloudflared/config.yml"

# Store passwords are generated once, on this host, and kept on the root disk with the other secrets: losing the
# root disk loses them, and the data volume's stores are then unreadable. Back them up with the data (README).
install -d -m 0700 "${APP_DIR}/secrets"
for name in postgres redis rabbitmq; do
  [[ -s "${APP_DIR}/secrets/${name}_password" ]] \
    || head -c 32 /dev/urandom | base64 | tr -d '\n=+/' > "${APP_DIR}/secrets/${name}_password"
done
{
  printf 'POSTGRES_PASSWORD=%s\n' "$(cat "${APP_DIR}/secrets/postgres_password")"
  printf 'REDIS_PASSWORD=%s\n' "$(cat "${APP_DIR}/secrets/redis_password")"
  printf 'RABBITMQ_PASSWORD=%s\n' "$(cat "${APP_DIR}/secrets/rabbitmq_password")"
  printf 'DATA_DIR=%s\nWORKSPACE_PARENT_DIR=%s\nDOCKER_SOCKET=/run/user/%s/docker.sock\n' "${DATA_DIR}" "${WORKSPACE_PARENT_DIR}" "${uid}"
} > "${APP_DIR}/stack.env"
chown root:root "${APP_DIR}/stack.env"
chmod 0600 "${APP_DIR}/stack.env"
export API_IMAGE="${api_image}" WORKER_IMAGE="${worker_image}"

# --- 4. Stores, then the migration alone ----------------------------------------------------------------------
log "starting the stores"
compose up -d --wait --wait-timeout 180 postgres redis rabbitmq >/dev/null
log "running the migration"
if ! compose run -T --rm --no-deps migrate; then
  log "migration failed; the running version is untouched"
  exit 1
fi

# --- 5. Start the new version, then prove it ------------------------------------------------------------------
api_sha_local() {  # prints the sha the running API container reports, or nothing
  compose exec -T api python -c \
    "import json,urllib.request;print(json.load(urllib.request.urlopen('http://localhost:8000/healthz',timeout=3)).get('sha',''))" \
    2>/dev/null || true
}
api_sha_public() {
  curl -fsS --max-time 5 "${PUBLIC_URL}/healthz?cb=$(date +%s)" 2>/dev/null | jq -r '.sha // empty' 2>/dev/null || true
}
running_image() {  # running_image <service>: the image reference its container was created from
  as_user docker inspect --format '{{.Config.Image}}' "$(compose ps -q "$1" | head -n1)" 2>/dev/null || true
}
healthy() {  # healthy <service>: the container reports healthy (or, with no healthcheck, running)
  local cid state
  cid="$(compose ps -q "$1" | head -n1)"
  [[ -n "${cid}" ]] || return 1
  state="$(as_user docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${cid}" 2>/dev/null || true)"
  [[ "${state}" == "healthy" || "${state}" == "running" ]]
}
wait_for_version() {  # wait_for_version <sha>: API (local and public), both workers and the connector, up to ~6 minutes.
  # The task worker's children load torch and the embedding model before they answer a ping, hence the length.
  local want="$1" local_ok="" public_ok=""
  for _ in $(seq 1 72); do
    [[ "$(api_sha_local)" == "${want}" ]] && local_ok=1
    [[ "$(api_sha_public)" == "${want}" ]] && public_ok=1
    if [[ -n "${local_ok}" && -n "${public_ok}" \
          && "$(running_image worker)" == "${WORKER_ECR_REPO_URI}:${want}" \
          && "$(running_image worker-maintenance)" == "${WORKER_ECR_REPO_URI}:${want}" ]] \
       && healthy api && healthy worker && healthy worker-maintenance && healthy cloudflared; then
      log "version ${want} confirmed: API on localhost and at ${PUBLIC_URL}, both workers on the image, all healthy"
      return 0
    fi
    sleep 5
  done
  log "version ${want} not confirmed (localhost: ${local_ok:-no}, public: ${public_ok:-no})"
  return 1
}

rollback() {
  log "FAILED; rolling back"
  if [[ -z "${previous_sha}" ]]; then
    log "no previous version recorded; leaving the failed one in place"
    return
  fi
  export API_IMAGE="${API_ECR_REPO_URI}:${previous_sha}" WORKER_IMAGE="${WORKER_ECR_REPO_URI}:${previous_sha}"
  as_user docker pull --quiet "${API_IMAGE}" >/dev/null || true
  as_user docker pull --quiet "${WORKER_IMAGE}" >/dev/null || true
  compose up -d --remove-orphans --no-deps api worker worker-maintenance cloudflared >/dev/null || true
  if wait_for_version "${previous_sha}"; then
    log "rolled back to ${previous_sha}"
  else
    log "ROLLBACK ALSO FAILED; the app needs a manual look"
  fi
}

log "starting the app"
if ! compose up -d --remove-orphans >/dev/null || ! wait_for_version "${sha}"; then
  rollback
  exit 1
fi

printf '%s\n' "${sha}" > current_sha
# What an operator needs to run compose by hand on the proven version:  --env-file stack.env --env-file last-good.env
printf 'API_IMAGE=%s\nWORKER_IMAGE=%s\n' "${api_image}" "${worker_image}" > "${APP_DIR}/last-good.env"
# Keep this deploy and the previous one; drop our older images. Never `image prune --all`: the sandbox's
# per-environment images live on this daemon too, and rebuilding them costs minutes per task.
while read -r ref; do
  case "${ref}" in
    *":${sha}"|*":${previous_sha:-none}") ;;
    *) as_user docker rmi "${ref}" >/dev/null 2>&1 || true ;;
  esac
done < <(as_user docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E "^(${API_ECR_REPO_URI}|${WORKER_ECR_REPO_URI}):" || true)
as_user docker image prune --force >/dev/null 2>&1 || true   # dangling layers only; never --all
log "deployed ${sha}"
