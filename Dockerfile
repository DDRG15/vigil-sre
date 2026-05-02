# =============================================================================
# Dockerfile — SRE Health Checker v3
# =============================================================================
#
# Build:    docker build -t sre-health-checker .
# Run once: docker run --rm \
#               --env-file .env \
#               -v "$(pwd)/state.json:/app/state.json" \
#               -v "$(pwd)/targets.yaml:/app/targets.yaml:ro" \
#               sre-health-checker
#
# Run on a schedule (every 60 s via shell loop):
#   docker run --rm --env-file .env \
#       -v "$(pwd)/state.json:/app/state.json" \
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

# Copy application source files.
# targets.yaml and .env are expected to be bind-mounted at runtime (see above)
# so they are NOT baked into the image — this keeps secrets out of image layers.
COPY main.py .

# state.json must be writable by the sre user.
# When the file is bind-mounted from the host, the host file's permissions apply.
# When not mounted (ephemeral run), the container writes here.
RUN install -d -o sre -g sre /app && touch /app/state.json && chown sre:sre /app/state.json

# Drop privileges before the process starts.
USER sre

# Healthcheck: verify Python can import the two critical deps and the script
# parses cleanly.  Docker marks the container unhealthy if this fails.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import aiohttp, yaml; print('deps OK')" || exit 1

# Default command — runs one full check cycle and exits.
# Pair with a CronJob (Kubernetes) or --restart=always + sleep loop (Docker)
# to run repeatedly.
CMD ["python", "main.py"]
