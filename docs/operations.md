# Operations

This page is for running repolace's services: what each compose service is, which env file it reads, how the worker behaves, and how the public site is deployed. For first-time setup, start with [getting-started.md](getting-started.md).

## What has been exercised

Be clear about this before relying on any of it.

- The Celery worker has run one real task end to end, which opened one pull request on a private smoke-test repository. That is the extent of real worker use.
- Every benchmark sweep used the `repolace-run-task` CLI path with `--no-pr`, not the worker.
- The public site runs on the AWS deployment (CDK stack, deploy script, workflow). A push to `main` was built, deployed and verified by commit sha on `/healthz`. Not yet tested: recovery after an instance reboot, a restore from a backup.
- Observability is structured JSON logs with a task id on every line. There is no tracing or metrics stack.

## Compose services

All services are in `infra/docker-compose.yml`. Run compose from the repository root with `-f infra/docker-compose.yml`, with `DOCKER_HOST` exported to the rootless daemon. Every published port binds to `127.0.0.1`.

| Service | Image or build | Port | Env file | Role |
|---|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | 5432 | none (`POSTGRES_*` from `.env`) | Task state, code chunks and vectors, call records |
| `redis` | `redis:7` | 6379 | none (`REDIS_PASSWORD`) | Sweeper dedupe keys |
| `rabbitmq` | `rabbitmq:3-management` | 5672, 15672 | none (`RABBITMQ_*`) | Task queue. `infra/rabbitmq.conf` raises `consumer_timeout` |
| `api` | `api/Dockerfile` | 8000 | `infra/api.env` | JSON API, task UI, GitHub webhook. No provider keys |
| `site` | `api/Dockerfile`, command overridden | 8100 | `infra/site.env` | The public site. The only service a public tunnel may point at |
| `worker` | `worker/Dockerfile` | none | `infra/worker.env` | Runs tasks on the default queue. Has the provider keys |
| `worker-maintenance` | `worker/Dockerfile` | none | none | Beat: the sweeper and the reaper, on their own queue. No provider keys |

Typical commands:

```bash
docker compose -f infra/docker-compose.yml up -d postgres redis rabbitmq
docker compose -f infra/docker-compose.yml up -d --build api worker worker-maintenance
docker compose -f infra/docker-compose.yml logs -f worker
docker compose -f infra/docker-compose.yml ps
```

`api`, `site` and `worker` define health checks (`/healthz` for the web services, `celery inspect ping` for the workers).

Compose's own `environment:` sets the three service URLs (database, Redis, broker) for the containers and wins over any env file. Fill those values in an env file only to run a process outside compose.

## Env file per service

Copy each `*.env.example` to the same name without `.example`. They are gitignored; the templates hold names only.

| File | Service | Notes |
|---|---|---|
| `.env` | Processes on the host; compose interpolation | Also where `DOCKER_SOCKET` and `WORKSPACE_PARENT_DIR` go for compose |
| `infra/api.env` | `api` | GitHub App values, `API_BASE_URL`, `UI_ALLOWED_HOSTS` |
| `infra/worker.env` | `worker` | GitHub App id and key, provider keys, optional tuning |
| `infra/site.env` | `site` | `SITE_ALLOWED_HOSTS`, `SITE_DATABASE_URL`, optional Turnstile keys |

Every variable is in [reference/env.md](reference/env.md). The security reasons for the split are in [security.md](security.md).

## The worker

### Build and start

The worker image is built from `worker/Dockerfile`: git 2.38 or newer, `prlimit`, and the Docker CLI. It runs as container root on purpose. On the rootless daemon, container uid 0 is the unprivileged host user that owns the Docker socket and the workspace directory; any other uid would map to a subuid that can reach neither.

```bash
docker compose -f infra/docker-compose.yml up -d --build worker worker-maintenance
```

What a first real run taught:

- **Rebuild after code changes.** A stale image registers no tasks. Always pass `--build` after pulling.
- **Keep `WORKER_CONCURRENCY` low on a laptop.** Each prefork child loads the embedding model (and torch) before it reports up. The default is 3, which is about 10 GB of RAM. Set `WORKER_CONCURRENCY=1` in `infra/worker.env` to start.
- **The first start is slow.** A child has up to 300 s to initialize (`worker_proc_alive_timeout`), and the health check allows 120 s before counting failures.
- **Run exactly one `worker-maintenance`.** Beat runs inside it. A second replica would publish every tick twice.

### Mounts

- The rootless Docker socket. `DOCKER_SOCKET` defaults to `/run/user/1000/docker.sock`; set it for another uid. Inside the container it is at `/run/repolace/docker.sock` and `DOCKER_HOST` points there. Never mount the rootful socket.
- `WORKSPACE_PARENT_DIR` (default `/var/tmp/repolace-workspaces`), mounted at **the same path** inside and outside the container. The sandbox bind-mounts paths under it, and the daemon resolves them on the host, so a different container path would give every Verify run an empty `/repo`. Put it on a size- and inode-limited filesystem: a suite can write as much as it likes into its export, and files left behind by the sandbox's subuid may not be deletable by the host.

### Bounds

| Setting | Default | Effect |
|---|---|---|
| `WORKER_CONCURRENCY` | 3 | Prefork children, so concurrent tasks |
| `WORKER_TASK_BOUND_SECONDS` | 11,700 | The task ends with `WorkerTimeout`; Celery's hard limit is this plus 600 s |
| `MAX_CHANGED_FILES`, `MAX_CHANGED_LINES` | 10, 500 | Largest change a task may open a PR for |
| Child recycling | 4,000,000 KiB or 50 tasks | A leak in torch or the model client cannot grow without bound |

If you raise `WORKER_TASK_BOUND_SECONDS`, raise `consumer_timeout` in `infra/rabbitmq.conf` too. It is 14,100,000 ms, the hard limit plus a 30-minute margin, and the broker closes the channel of a task that outlives it.

### Recovery

| Situation | Behavior |
|---|---|
| The API could not reach the broker | The row stays `queued`. The sweeper in `worker-maintenance` republishes `queued` product rows older than 5 minutes, at most once per 15 minutes per row and 50 rows per tick. It runs every 60 s |
| A worker process dies mid-task | The pipeline writes a heartbeat every 30 s. The reaper fails a `running` row whose heartbeat is older than 5 minutes with `runner: abandoned` and removes the task's containers |
| A task outlives its bound | The row reads `interrupted: WorkerTimeout`, or `worker: timeout` if the worker had to settle it itself |
| A message is delivered twice | The conditional claim drops the second delivery |
| A benchmark row is published by hand | The worker refuses it untouched; the eval runner owns those rows |

**A row is never re-run.** A retry could re-enter a row that is half done. To try an issue again, enqueue a new task.

## Public site deployment

The public site is a separate process (`repolace_api.site.main:app`, port 8100) that mounts none of the API routes. Its operator procedure is [infra/aws/README.md](../infra/aws/README.md). In outline:

- **One small VM.** A CDK stack creates one arm64 instance, a container registry, an encrypted backup bucket, and the roles the pipeline uses. Postgres and Docker volumes live on a separate retained, snapshotted data volume. The security group has no inbound rule, access is through AWS Systems Manager only, and the instance requires IMDSv2.
- **Three compose services on the VM** (`infra/aws/compose.site.yml`): `site`, its own `site-db` Postgres, and a Cloudflare Tunnel connector. Nothing is published on a host port. The tunnel is outbound only.
- **Deploys through GitHub Actions** (`.github/workflows/deploy-site.yml`). Every job is gated by the repository variable `SITE_DEPLOY_ENABLED`, so merging the workflow does nothing until you set it. A push to `main` that touches the site builds an arm64 image and pushes it, tagged with the full commit sha. The deploy job runs in a `production` environment, which you configure in GitHub to restrict deployments to `main` (and, if you want, to require a reviewer; that setting lives in GitHub, not in this repository), and runs `deploy.sh <sha>` on the VM through Systems Manager.
- **The version is proven, not assumed.** The image bakes in its commit sha, and `/healthz` returns it. `deploy.sh` requires the new sha on both localhost and the public URL, retrying for about two minutes. On any failure it rolls back to the previous image tag and exits non-zero.
- **No long-lived cloud keys.** The workflow assumes two narrowly scoped roles through GitHub's OIDC identity.
- **Backups.** A systemd timer streams `pg_dump` to the versioned, encrypted bucket nightly with a 30-day expiry, and uploads only a dump that `pg_restore -l` lists cleanly.
- **Secrets** are created by hand in the cloud secret store and rendered to files on the VM at deploy. The repository references them by name in the infrastructure code and never holds a value.

A single VM is not highly available. Recovery is a reboot, the nightly backup and the CDK stack. Not yet tested: recovery after an instance reboot, a restore from a backup, and a rollback under a real failure.

The tunnel stays locally managed, and the private API and UI are never routed through it.

## Related

- [Getting started](getting-started.md)
- [Security](security.md)
- [Architecture: failure and recovery](architecture.md#failure-and-recovery)
- [Environment variables](reference/env.md)
