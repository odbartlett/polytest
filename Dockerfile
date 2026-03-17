# =============================================================================
# Stage 1: Build — install dependencies into an isolated virtual environment
# =============================================================================
FROM python:3.11-slim AS builder

WORKDIR /build

# Install system build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies (leverage Docker layer cache)
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# =============================================================================
# Stage 2: Runtime — minimal image for production
# =============================================================================
FROM python:3.11-slim AS runtime

# Install only runtime system libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1001 botuser \
    && useradd --uid 1001 --gid 1001 --no-create-home --shell /sbin/nologin botuser

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copy application source
COPY --chown=botuser:botuser . .

# Switch to non-root user
USER botuser

# Monitoring dashboard port (overridden by PORT env var on Railway)
EXPOSE 8080

# Health check via the monitoring API
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/api/status || exit 1

ENTRYPOINT ["python", "main.py"]
