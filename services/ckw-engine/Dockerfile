FROM ghcr.io/astral-sh/uv:0.9.26 AS uv

FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp/communi-ckw \
    PATH=/app/.venv/bin:$PATH

COPY --from=uv /uv /uvx /usr/local/bin/

RUN groupadd --gid 10001 communi \
    && useradd --uid 10001 --gid communi --home-dir /nonexistent --no-create-home communi \
    && mkdir -p /app /tmp/communi-ckw \
    && chown -R communi:communi /app /tmp/communi-ckw

WORKDIR /app
COPY --chown=communi:communi pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=communi:communi src ./src
RUN uv sync --frozen --no-dev

FROM base AS test
RUN uv sync --frozen
COPY --chown=communi:communi tests ./tests
RUN pytest -q

FROM base AS runtime

EXPOSE 8000

CMD ["python", "-m", "ckw_script.startup"]
