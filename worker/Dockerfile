FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /repo
COPY . .

RUN uv sync --frozen --package repolace-worker

CMD ["uv", "run", "--project", "worker", "celery", "-A", "app.celery_app", "worker", "--loglevel=info"]
