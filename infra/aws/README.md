# Public site on AWS

One small VM runs the public site (`site`), its Postgres (`site-db`) and the Cloudflare Tunnel connector
(`cloudflared`) under Docker Compose. Nothing listens on a public port: the security group has no inbound
rule, admin and deploys go through SSM (no SSH, no key pair), and the tunnel is outbound only.

```
push to main -> test (GitHub) -> build arm64 image -> ECR (build role)
                                  -> deploy job (production environment, deploy role)
                                     -> SSM RunCommand -> /opt/repolace/deploy.sh <sha> on the VM
                                        pull, render secrets, up -d, prove the sha on localhost and the
                                        public URL, roll back to the previous tag on any failure
```

## What is here

| File | Purpose |
| --- | --- |
| `stacks/site_stack.py`, `app.py`, `cdk.json` | The CDK stack `RepolaceSiteStack` (VM, ECR repo, backup bucket, instance role, two OIDC roles) |
| `compose.site.yml` | `site`, `site-db`, `cloudflared`; nothing published on a host port |
| `deploy.sh` | Run by SSM on the VM; refreshes the other files from the image, then deploys |
| `bootstrap.sh` | First boot only (docker, compose plugin, backup timer) |
| `backup.sh`, `backup.service`, `backup.timer` | Nightly `pg_dump` streamed to the backup bucket |
| `cloudflared/config.yml` | Tunnel ingress (`http://site:8100`) for the locally managed tunnel `repolace-site` |
| `site-db/init.sql` | `waitlist_signups` (same DDL as alembic 0017) and the INSERT-only role `repolace_site` |
| `../../.github/workflows/deploy-site.yml` | The pipeline, behind `SITE_DEPLOY_ENABLED` |

Stack parameters (context, `-c key=value` or `cdk.json`): `architecture` (`arm64` default, or `x86_64`, which
selects `t3a.small`), `instance_type`, `github_repo` (default `repolace/repolace`), `create_oidc_provider`
(`true` default; `false` if the account already has the GitHub OIDC provider, which an account can have only one of).
Changing `architecture` also needs the workflow's runner and `platforms` changed (`ubuntu-24.04` and `linux/amd64`).

The stack is environment-agnostic, so `cdk synth` needs no credentials:

```
cd infra/aws
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt pytest
npx aws-cdk synth --strict
python -m pytest --noconftest -c /dev/null --rootdir=. tests      # the stack's tests
```

## First-time setup (you run these; nothing here has been deployed)

1. **Sign in.** `aws login` (profile for the account). Check `aws sts get-caller-identity`. The account had an
   "account being verified" block for Bedrock; EC2 may hit the same, so expect the first launch to be the test.
2. **Bootstrap and deploy the stack** (creates cloud resources, about 15 to 18 USD a month: needs your approval):
   `npx aws-cdk bootstrap` once per account and region, then `npx aws-cdk diff` and `npx aws-cdk deploy`.
   Note the outputs: `InstanceId`, `EcrRepositoryUri`, `BackupBucketName`, `BuildRoleArn`, `DeployRoleArn`.
   If the account already has a GitHub OIDC provider, use `-c create_oidc_provider=false`.
3. **Create the two secrets by hand in the Secrets Manager console** (plaintext, same region). The names are
   fixed because the instance role is scoped to exactly these two:
   - `repolace-site-env`: the full content of `infra/site.env` for production. `SITE_ALLOWED_HOSTS=repolace.rounakkumarsingh.com`,
     `SITE_TRUST_CF_IP=true`, the Turnstile keys, and
     `SITE_DATABASE_URL=postgresql+asyncpg://repolace_site:<password>@site-db:5432/repolace`. Pick the password
     yourself (URL-encode special characters); `deploy.sh` sets the database role's password from this URL on every deploy.
   - `repolace-tunnel-credentials`: the content of the tunnel's credentials JSON, `~/.cloudflared/5babc1c9-6e2b-489f-9e65-890ac7ec3cbd.json`.
     Open the file in your editor and paste it; do not print it into a terminal log or chat.
   The tunnel stays locally managed: do not migrate it to remote management or fetch a tunnel token.
4. **GitHub.** In `repolace/repolace` (needs org admin): create the environment `production` (restrict its
   deployment branches to `main`); set repository variables `AWS_REGION`, `AWS_BUILD_ROLE_ARN`, `AWS_DEPLOY_ROLE_ARN`,
   `SITE_INSTANCE_ID`, `ECR_REPOSITORY_URI` from the stack outputs. Leave `SITE_DEPLOY_ENABLED` unset for now.
5. **Check the host.** Once the instance shows as a managed node: `aws ssm start-session --target <InstanceId>`, then
   `docker --version`, `docker compose version`, `ls /opt/repolace`. (If bootstrap failed: `/var/log/cloud-init-output.log`.)
6. **Pin check.** `bootstrap.sh` pins Docker Compose `v2.35.1`; `compose.site.yml` pins `cloudflare/cloudflared:2025.11.1`
   (override with `CLOUDFLARED_IMAGE`). Confirm both tags exist and bump if you prefer newer ones.

## Cutover

The laptop connector must not be running (never two connectors for one tunnel: stop `cloudflared` on the laptop and keep it stopped).

1. Enable the pipeline and run the first deploy: set `SITE_DEPLOY_ENABLED=true`, then run the workflow from the
   Actions tab (`workflow_dispatch`) on `main`. It fails visibly if the version cannot be proven on the public URL.
2. **Restore the signups from the laptop** (waitlist rows are personal data: stream them, never save or print them).
   `ssm start-session` cannot take stdin, so the bucket is the pipe, and the object is deleted right after. Use your own
   credentials for the upload; the instance role may read and write only under `backups/`:
   ```
   docker exec <laptop-postgres-container> pg_dump -U <user> -d repolace --data-only --table waitlist_signups --no-owner \
     | aws s3 cp - s3://<BackupBucketName>/backups/restore/waitlist.sql
   aws ssm start-session --target <InstanceId>
   # on the host (the first deploy already created the empty table):
   aws s3 cp s3://<BackupBucketName>/backups/restore/waitlist.sql - | docker exec -i repolace-site-db psql -U postgres -d repolace -v ON_ERROR_STOP=1
   aws s3 rm s3://<BackupBucketName>/backups/restore/waitlist.sql
   ```
3. Verify: `curl -I https://repolace.rounakkumarsingh.com/`, `curl https://repolace.rounakkumarsingh.com/healthz` (shows the deployed `sha`),
   `/benchmark/` loads, and a test signup lands (then delete that one row).
4. Reboot the instance once and check the site returns without anyone logging in.

## Day to day

- **Deploy**: merge to `main` (paths filter: site, shared, `api/Dockerfile`, `api/pyproject.toml`, `uv.lock`, `infra/aws/**`, the workflow), or run the workflow by hand.
- **Rollback**: automatic on a failed deploy (`deploy.sh` restores the previous tag and exits non-zero). By hand: run the
  workflow on the older commit, or `aws ssm send-command ... /opt/repolace/deploy.sh <older sha>` (the image must still be among the last 10 in ECR).
- **Look at the host**: `aws ssm start-session --target <InstanceId>`, then `cd /opt/repolace && docker compose -f compose.site.yml ps`.
  Do not paste the contents of `site.env` or `cloudflared/credentials.json` anywhere.
- **Rotate a secret**: edit it in the console, then re-run the workflow (deploy renders the files again).
- **Restore from backup**: backups are `backups/site-db-<timestamp>.dump` (pg_dump custom format, 30-day expiry, versioned).
  On the host: `aws s3 cp s3://<BackupBucketName>/backups/<file> - | docker exec -i repolace-site-db pg_restore -U postgres -d repolace --clean --if-exists --no-owner`,
  then `docker exec -i repolace-site-db psql -U postgres -d repolace < site-db/init.sql` to re-apply the role grants. To test a backup,
  restore into a scratch database (`createdb scratch`) and count rows.
- **Replacing the VM**: the database lives on the instance's disk; a replacement starts empty. Take or find a fresh backup first, deploy, then restore as above.

## Honest limits

A single VM is not highly available: recovery is a reboot (everything restarts at boot), a nightly backup and the
CDK stack. Not run by the author: `cdk deploy`, the user data on a real instance, `deploy.sh` and the workflow end to end.
