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

# Reads the heartbeat the bot touches from its own event loop, so "healthy" means the
# loop is turning — not merely that a second process can parse the config, which is what
# the old `validate` check proved and which a wedged bot would also pass. The short
# interval is what lets a deploy get a verdict in seconds rather than minutes.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["tabelshchik", "healthcheck"]

ENTRYPOINT ["tabelshchik"]
CMD ["run", "--json-logs"]
