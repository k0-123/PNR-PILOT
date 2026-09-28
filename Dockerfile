# Official Playwright image: Python + Chromium + all system libraries.
# The tag must match the playwright version pinned in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY config ./config
COPY .streamlit ./.streamlit

# Run as an unprivileged user with a fixed UID (the ./data volume must be writable by it:
#   sudo chown -R 10001:10001 data   on the host).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
USER appuser

VOLUME ["/data"]
EXPOSE 8501

# Default command = the UI; docker-compose overrides it for the worker.
CMD ["streamlit", "run", "app/ui.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
