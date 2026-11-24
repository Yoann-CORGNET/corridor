# corridor-server image, built from backend/: `docker build -t corridor-backend backend`.
#
# The project is installed editable on purpose: SCENARIOS_DIR and DATA_DIR are resolved from the
# source tree (src/corridor/... -> backend/scenarios, backend/data), so the code must stay under
# /app/src and not move into site-packages.
#
# Same supply-chain rules as CI: dependencies from the lockfile only, wheels only; the corridor
# package itself is the only build.

FROM ghcr.io/astral-sh/uv:0.10.9 AS uv

FROM python:3.12-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv

# The rasterio wheel bundles GDAL but links against the system libexpat. The server runs as an
# unprivileged user, created here so that this rarely changing layer stays cached.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 corridor

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_BINARY_PACKAGE=corridor \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first: this layer is reused as long as the lockfile does not change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-build --no-dev --no-install-project

COPY src ./src
COPY scenarios ./scenarios
COPY data ./data
RUN uv sync --locked --no-build --no-dev

USER corridor

# Cloud Run sets PORT; CORRIDOR_SCENARIO picks a file of backend/scenarios.
ENV PORT=8080 \
    CORRIDOR_SCENARIO=demo.json
EXPOSE 8080

CMD ["sh", "-c", "exec corridor-server --host 0.0.0.0 --port \"$PORT\" --scenario \"scenarios/$CORRIDOR_SCENARIO\""]
