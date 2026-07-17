FROM python:3.12-slim

# The source label is what links the GHCR package page back to this repository (and lets it
# inherit the repo's visibility/README on the package page).
LABEL org.opencontainers.image.source="https://github.com/D4rkm3ch/service-sentinel" \
      org.opencontainers.image.description="Watches your homelab's Docker containers and compose files: AI-summarized updates, log triage, and compose review." \
      org.opencontainers.image.licenses="MIT"

# Under docker-compose.example.yml's read_only root filesystem, Python's own .pyc bytecode
# cache write would silently fail anyway (Python already tolerates an unwritable source tree),
# but skipping the attempt outright avoids the wasted syscalls on every import, every start.
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
