# =============================================================================
# Dockerfile — SRE Health Checker v3
# =============================================================================
#
# Build:    docker build -t sre-health-checker .
#
# state.json needs a DIRECTORY bind mount, not a single-file one — see the
# STATE_FILE_PATH comment in docker-compose.yml for why (rename() onto a
# bind-mounted single file fails with EBUSY; a directory mount doesn't have
# this problem). history.db doesn't need this: SQLite writes in place rather
# than via a tmp-file rename, so a single-file mount is fine for it.
#
# Run once: docker run --rm \
#               --env-file .env \
#               -e STATE_FILE_PATH=/app/data/state.json \
#               -v "$(pwd)/data:/app/data" \
#               -v "$(pwd)/history.db:/app/history.db" \
#               -v "$(pwd)/targets.yaml:/app/targets.yaml:ro" \
#               sre-health-checker
#
# Run on a schedule (every 60 s via shell loop):
#   docker run --rm --env-file .env \
#       -e STATE_FILE_PATH=/app/data/state.json \
#       -v "$(pwd)/data:/app/data" \
#       -v "$(pwd)/history.db:/app/history.db" \
#       -v "$(pwd)/targets.yaml:/app/targets.yaml:ro" \
#       sre-health-checker \
#       sh -c 'while true; do python main.py; sleep 60; done'
# =============================================================================


# ── Stage 1: dependency builder ───────────────────────────────────────────────
# Use the full slim image to compile any C-extension wheels (e.g. aiohttp's
# optional speedups).  This layer is never shipped in the final image.
FROM python:3.11-slim AS builder

# Prevent .pyc files and enable unbuffered stdout/stderr for clean log streaming.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Copy only the dependency manifest first so Docker's layer cache is
# invalidated only when requirements actually change — not on every code edit.
COPY requirements.txt .

# Install into an isolated prefix so copying to the final stage is surgical.
RUN pip install --upgrade pip --no-cache-dir \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: lean runtime image ───────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# ── Security: run as a non-root user ──────────────────────────────────────────
# Creating a dedicated user with no login shell and no home directory is a
# Docker hardening best practice; it limits blast radius if the container is
# ever compromised.
RUN groupadd --system sre && useradd --system --gid sre --no-create-home sre

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Tell Python where to find the packages installed in the builder stage.
    PYTHONPATH=/app/site-packages

WORKDIR /app

# Copy pre-built packages from the builder stage — no pip, no compiler needed
# in the runtime image, which keeps it small and reduces the attack surface.
COPY --from=builder /install/lib/python3.11/site-packages /app/site-packages

# Copy application source files — all three modules main.py imports.
# targets.yaml and .env are expected to be bind-mounted at runtime (see above)
# so they are NOT baked into the image — this keeps secrets out of image layers.
COPY main.py diagnostics.py history.py ./

# state.json (ephemeral fallback path) and /app/data (the STATE_FILE_PATH
# directory docker-compose.yml mounts — see its comment for why state.json
# needs a directory mount, not a single-file one) must both be writable by
# the sre user. When bind-mounted from the host, the host path's own
# permissions apply instead; these just cover the ephemeral, non-mounted run.
RUN install -d -o sre -g sre /app /app/data \
 && touch /app/state.json && chown sre:sre /app/state.json

# Drop privileges before the process starts.
USER sre

# Healthcheck: import the actual application module, not just its deps.
# `import aiohttp, yaml` passed even when main.py itself couldn't be
# imported (ModuleNotFoundError on a sibling module missing from the image) —
# a healthcheck that only proves the dependencies exist proves nothing about
# whether the application can start. Docker marks the container unhealthy if
# this fails.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import main; print('main OK')" || exit 1

# Default command — runs one full check cycle and exits.
# Pair with a CronJob (Kubernetes) or --restart=always + sleep loop (Docker)
# to run repeatedly.
CMD ["python", "main.py"]
