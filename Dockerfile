FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app

# Files stay owned by root (read-only for the app, which writes nothing under /app): a
# `chown -R` would copy the whole virtualenv into a new layer on every build.
RUN useradd --system --uid 10001 --home /app scraper

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    COMPETITOR_CONFIG_DIR=/app/config/competitors

USER scraper

EXPOSE 8000
ENTRYPOINT ["catalog-scraper"]
CMD ["worker"]
