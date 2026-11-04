#!/usr/bin/env bash
# Deploy one image tag of the public site on this host. Run as root through SSM (AWS-RunShellScript) by
# .github/workflows/deploy-site.yml:   /opt/repolace/deploy.sh <full 40-hex git sha>
#
# Its output goes to the GitHub Actions log, so it never prints a secret or the rendered env files.
# On any failure after the new image was started, it rolls back to the previous tag and exits non-zero.
set -euo pipefail
set +x
umask 077

APP_DIR="/opt/repolace"
# shellcheck source=/dev/null
source "${APP_DIR}/host.env"   # AWS_REGION, ECR_REPO_URI, SITE_ENV_SECRET, TUNNEL_SECRET, PUBLIC_URL, BACKUP_BUCKET

log() { printf '[deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

sha="${1:-}"
if [[ ! "${sha}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: deploy.sh <40-hex git sha>" >&2
  exit 2
fi

cd "${APP_DIR}"
image="${ECR_REPO_URI}:${sha}"
previous_sha=""
[[ -f current_sha ]] && previous_sha="$(cat current_sha)"

compose() { docker compose -f "${APP_DIR}/compose.site.yml" --project-directory "${APP_DIR}" "$@"; }

# --- 1. Pull the image -------------------------------------------------------------------------------
registry="${ECR_REPO_URI%%/*}"
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${registry}" >/dev/null
log "pulling ${sha}"
docker pull --quiet "${image}" >/dev/null

# --- 2. Refresh this directory's files from the image, then re-run the new deploy.sh -----------------
# The image carries infra/aws (see .dockerignore), so the compose file, tunnel ingress, init SQL and these
# scripts always match the commit being deployed. A script replaced mid-run is re-executed once.
if [[ -z "${REPOLACE_DEPLOY_REEXEC:-}" ]]; then
  stage="$(mktemp -d)"
  cid="$(docker create "${image}")"
  docker cp "${cid}:/repo/infra/aws/." "${stage}/" >/dev/null
  docker rm "${cid}" >/dev/null
  for f in compose.site.yml backup.sh backup.service backup.timer; do
    install -m 0644 "${stage}/${f}" "${APP_DIR}/${f}.new" && mv "${APP_DIR}/${f}.new" "${APP_DIR}/${f}"
  done
  chmod 0755 "${APP_DIR}/backup.sh"
  install -d -m 0755 "${APP_DIR}/cloudflared" "${APP_DIR}/site-db"
  install -m 0644 "${stage}/cloudflared/config.yml" "${APP_DIR}/cloudflared/config.yml"
  install -m 0644 "${stage}/site-db/init.sql" "${APP_DIR}/site-db/init.sql"
  install -m 0644 "${APP_DIR}/backup.service" "${APP_DIR}/backup.timer" /etc/systemd/system/
  systemctl daemon-reload
  install -m 0755 "${stage}/deploy.sh" "${APP_DIR}/deploy.sh.new"
  mv "${APP_DIR}/deploy.sh.new" "${APP_DIR}/deploy.sh"
  rm -rf "${stage}"
  export REPOLACE_DEPLOY_REEXEC=1
  exec "${APP_DIR}/deploy.sh" "${sha}"
fi

# --- 3. Render secrets (never printed) ---------------------------------------------------------------
fetch_secret() {  # fetch_secret <secret-id> <destination file>
  aws secretsmanager get-secret-value --region "${AWS_REGION}" --secret-id "$1" \
    --query SecretString --output text > "$2.tmp"
  mv "$2.tmp" "$2"
}
fetch_secret "${SITE_ENV_SECRET}" "${APP_DIR}/site.env"
mkdir -p "${APP_DIR}/cloudflared" "${APP_DIR}/secrets"
fetch_secret "${TUNNEL_SECRET}" "${APP_DIR}/cloudflared/credentials.json"
# cloudflared runs as uid 65532 inside its image and must be able to read the file.
chown 65532:65532 "${APP_DIR}/cloudflared/credentials.json"
chmod 0400 "${APP_DIR}/cloudflared/credentials.json"
if [[ ! -s "${APP_DIR}/secrets/pg_admin_password" ]]; then
  head -c 32 /dev/urandom | base64 | tr -d '\n=+/' > "${APP_DIR}/secrets/pg_admin_password"
fi
# The postgres entrypoint reads this file as the postgres user inside the container.
chmod 0444 "${APP_DIR}/secrets/pg_admin_password"
printf 'IMAGE=%s\n' "${image}" > "${APP_DIR}/.env"

# --- 4. Database: start, apply init.sql, set the INSERT-only role's password -----------------------
log "starting site-db"
compose up -d site-db >/dev/null
for _ in $(seq 1 30); do
  [[ "$(docker inspect --format '{{.State.Health.Status}}' repolace-site-db 2>/dev/null || true)" == "healthy" ]] && break
  sleep 2
done
[[ "$(docker inspect --format '{{.State.Health.Status}}' repolace-site-db)" == "healthy" ]] || { log "site-db is not healthy"; exit 1; }

# The role's password is the one in SITE_DATABASE_URL, so the two cannot drift apart.
site_pw="$(SITE_ENV_FILE="${APP_DIR}/site.env" python3 - <<'PY'
import os
from urllib.parse import unquote, urlsplit

for line in open(os.environ["SITE_ENV_FILE"], encoding="utf-8"):
    if line.startswith("SITE_DATABASE_URL="):
        url = line.split("=", 1)[1].strip().strip("'\"")
        print(unquote(urlsplit(url).password or ""))
        break
PY
)"
if [[ -z "${site_pw}" ]]; then
  log "SITE_DATABASE_URL in ${SITE_ENV_SECRET} has no password"
  exit 1
fi
docker exec -i -e REPOLACE_SITE_PW="${site_pw}" repolace-site-db \
  psql -U postgres -d repolace -q -v ON_ERROR_STOP=1 -f - < "${APP_DIR}/site-db/init.sql"
unset site_pw

# --- 5. Start the new site and the connector, then prove the version ---------------------------------
site_sha_local() {  # prints the sha the running site container reports, or nothing
  compose exec -T site python -c \
    "import json,urllib.request;print(json.load(urllib.request.urlopen('http://localhost:8100/healthz',timeout=3)).get('sha',''))" \
    2>/dev/null || true
}
site_sha_public() {
  curl -fsS --max-time 5 "${PUBLIC_URL}/healthz?cb=$(date +%s)" 2>/dev/null \
    | jq -r '.sha // empty' 2>/dev/null || true
}
wait_for_sha() {  # wait_for_sha <expected sha>: both the container and the public URL, up to ~2 minutes
  local want="$1" local_ok="" public_ok=""
  for _ in $(seq 1 24); do
    [[ "$(site_sha_local)" == "${want}" ]] && local_ok=1
    [[ "$(site_sha_public)" == "${want}" ]] && public_ok=1
    if [[ -n "${local_ok}" && -n "${public_ok}" ]]; then
      log "version ${want} confirmed on localhost and at ${PUBLIC_URL}"
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
  printf 'IMAGE=%s\n' "${ECR_REPO_URI}:${previous_sha}" > "${APP_DIR}/.env"
  docker pull --quiet "${ECR_REPO_URI}:${previous_sha}" >/dev/null || true
  compose up -d --remove-orphans site cloudflared >/dev/null || true
  if wait_for_sha "${previous_sha}"; then
    log "rolled back to ${previous_sha}"
  else
    log "ROLLBACK ALSO FAILED; the site needs a manual look"
  fi
}

log "starting site and cloudflared"
if ! compose up -d --remove-orphans site cloudflared >/dev/null || ! wait_for_sha "${sha}"; then
  rollback
  exit 1
fi

printf '%s\n' "${sha}" > current_sha
# Keep this deploy and the previous one; drop the rest.
docker image prune --all --force --filter "until=72h" >/dev/null 2>&1 || true
log "deployed ${sha}"
