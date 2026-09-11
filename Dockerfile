# syntax=docker/dockerfile:1

FROM python:3.13-slim AS base

# uv installs faster than pip and resolves from the committed lockfile, so an image
# built today and one built in six months contain the same dependency versions.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Almaty

WORKDIR /app

# Dependencies first: this layer is cached unless the lockfile itself changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/
# The migration tree ships with the image: the schema is brought up to date on boot,
# so the container must carry the migrations that do it.
COPY alembic.ini ./
COPY migrations/ ./migrations/
RUN uv sync --frozen --no-dev

# Unprivileged, and owning only what it must write to.
RUN useradd --create-home --uid 10001 tabelshchik \
    && mkdir -p /data \
    && chown -R tabelshchik:tabelshchik /data /app
USER tabelshchik

ENV PATH="/app/.venv/bin:$PATH" \
    TABELSHCHIK_CONFIG=/app/config \
    TABELSHCHIK_DB=/data/tabelshchik.db

VOLUME ["/data"]

# Long polling means no inbound port, so a health check has to be something the process
# can answer for itself. Validating config exercises the interpreter and the config path.
HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD ["tabelshchik", "validate"]

ENTRYPOINT ["tabelshchik"]
CMD ["run", "--json-logs"]
