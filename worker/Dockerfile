# The docker *client* only: the worker talks to the host's rootless daemon through a mounted
# socket (see infra/docker-compose.yml) and never runs a daemon of its own. A client older than
# the daemon is fine; the daemon accepts every API version back to its minimum.
ARG DOCKER_CLI_TAG=28-cli
FROM docker:${DOCKER_CLI_TAG} AS docker-cli

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# What a task shells out to. git: the clone, merge-tree and the credential helper need >= 2.38
# (`merge-tree --write-tree`), and the build fails here rather than on the first conflicting
# task if the base image ever ships an older one. util-linux: the agent's `grep` tool runs git
# under `prlimit`, and `build_toolbox` refuses to start without it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && dpkg --compare-versions "$(git --version | awk '{print $3}')" ge 2.38 \
    && prlimit --version > /dev/null \
    && docker --version

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN useradd --create-home --uid 1000 repolace

WORKDIR /repo
RUN chown repolace:repolace /repo

COPY --chown=repolace:repolace pyproject.toml uv.lock ./
COPY --chown=repolace:repolace api/pyproject.toml api/pyproject.toml
COPY --chown=repolace:repolace worker/pyproject.toml worker/pyproject.toml
COPY --chown=repolace:repolace agents/pyproject.toml agents/pyproject.toml
COPY --chown=repolace:repolace gateway/pyproject.toml gateway/pyproject.toml
COPY --chown=repolace:repolace rag/pyproject.toml rag/pyproject.toml
COPY --chown=repolace:repolace shared/pyproject.toml shared/pyproject.toml
COPY --chown=repolace:repolace eval/pyproject.toml eval/pyproject.toml
COPY --chown=repolace:repolace pipeline/pyproject.toml pipeline/pyproject.toml
COPY --chown=repolace:repolace verify/pyproject.toml verify/pyproject.toml

USER repolace

RUN uv sync --frozen --no-install-project --package repolace-worker

COPY --chown=repolace:repolace . .

RUN uv sync --frozen --package repolace-worker

CMD ["uv", "run", "--project", "worker", "celery", "-A", "repolace_worker.celery_app", "worker", "--loglevel=info"]
