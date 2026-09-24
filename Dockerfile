# ==============================================================================
# ROOSTOO AUTONOMOUS QUANT BOT - PRODUCTION DOCKERFILE
# ==============================================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies to wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Final production stage
FROM python:3.11-slim

# Create non-root system user
RUN groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -s /bin/bash -m appuser

WORKDIR /app

# Copy installed python packages from builder
COPY --from=builder /root/.local /home/appuser/.local
ENV PATH=/home/appuser/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy source code and default configuration
COPY --chown=appuser:appgroup config/ ./config/
COPY --chown=appuser:appgroup core/ ./core/
COPY --chown=appuser:appgroup strategies/ ./strategies/
COPY --chown=appuser:appgroup state/ ./state/
COPY --chown=appuser:appgroup backtest/ ./backtest/
COPY --chown=appuser:appgroup logs/ ./logs/
COPY --chown=appuser:appgroup tests/ ./tests/
COPY --chown=appuser:appgroup main.py ./main.py

# Create persistent state and log directories with non-root ownership
RUN mkdir -p /app/data /app/logs && chown -R appuser:appgroup /app/data /app/logs

# Switch to non-root user
USER appuser

# Healthcheck invoking the status check routine
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python main.py --status || exit 1

# Default entrypoint runs the autonomous bot
ENTRYPOINT ["python", "main.py"]
