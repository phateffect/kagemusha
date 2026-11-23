FROM ghcr.io/astral-sh/uv:0.13.9-python3.15-trixie-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock ./

RUN uv sync \
    --locked \
    --no-dev \
    --no-install-project

COPY src/ /app/src/

RUN uv sync \
    --locked \
    --no-dev


FROM ghcr.io/jdx/mise:latest AS mise


FROM docker.io/library/python:3.15.2-slim-trixie

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' \
       /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       git \
       git-lfs \
       tar \
       zstd \
       ca-certificates \
       curl \
       unzip \
       xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ /app/src/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KAGEMUSHA_DATA_DIR=/data

RUN groupadd --system app \
    && useradd --system --gid app app \
    && chmod 755 /app/src/kagemusha/askpass.py \
    && mkdir -p \
       /data/git/mirrors \
       /data/copies/staging \
       /data/copies/artifacts \
       /data/cache/work \
       /data/cache/staging \
       /data/cache/snapshots \
       /data/mise \
       /data/jobs \
       /data/backups \
       /data/locks \
    && chown -R app:app /app /data

USER app

EXPOSE 8000

CMD ["kagemusha", "serve"]
