#!/usr/bin/env bash
# Nightly dump of the site database to the backup bucket (run by backup.timer as root).
# Streams pg_dump straight to S3; nothing is written to local disk and no value is printed.
set -euo pipefail

# shellcheck source=/dev/null
source /opt/repolace/host.env

stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
key="backups/site-db-${stamp}.dump"

docker exec repolace-site-db pg_dump -U postgres -d repolace --format=custom --no-owner \
  | aws s3 cp - "s3://${BACKUP_BUCKET}/${key}" --region "${AWS_REGION}" --expected-size 104857600 --only-show-errors

echo "backup written: ${key}"
