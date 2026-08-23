FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /repo
COPY . .

RUN uv sync --frozen --package repolace-api

CMD ["uv", "run", "--project", "api", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
