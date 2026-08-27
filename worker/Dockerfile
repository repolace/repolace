FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

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

USER repolace

RUN uv sync --frozen --no-install-project --package repolace-worker

COPY --chown=repolace:repolace . .

RUN uv sync --frozen --package repolace-worker

CMD ["uv", "run", "--project", "worker", "celery", "-A", "repolace_worker.celery_app", "worker", "--loglevel=info"]
