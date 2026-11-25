FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HEARTBEAT_FILE=/tmp/shipping-worker.heartbeat

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --system --uid 10001 --no-create-home app
COPY --chown=app:app . .
USER app

# Default process is the scrape worker; the API service overrides the command.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "main.py", "healthcheck"]

CMD ["python", "main.py", "worker"]
