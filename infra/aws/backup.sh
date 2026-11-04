#!/usr/bin/env bash
# Nightly dump of the site database to the backup bucket (run by backup.timer as root).
# The dump goes to a temp file first and is listed with pg_restore -l; only a dump that restores cleanly
# is uploaded, so a failed pg_dump can never replace a good backup with a truncated one. No value is printed.
set -euo pipefail
umask 077

# shellcheck source=/dev/null
source /opt/repolace/host.env

tmp="$(mktemp /var/tmp/site-db-backup.XXXXXX)"
trap 'rm -f "${tmp}"' EXIT

docker exec repolace-site-db pg_dump -U postgres -d repolace --format=custom --no-owner > "${tmp}"
[[ -s "${tmp}" ]] || { echo "backup failed: empty dump" >&2; exit 1; }
docker exec -i repolace-site-db pg_restore -l < "${tmp}" > /dev/null

key="backups/site-db-$(date -u +%Y-%m-%dT%H%M%SZ).dump"
aws s3 cp "${tmp}" "s3://${BACKUP_BUCKET}/${key}" --region "${AWS_REGION}" --only-show-errors
echo "backup written: ${key}"
