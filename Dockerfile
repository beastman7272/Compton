FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    RAILWAY_VOLUME_MOUNT_PATH=/app/data \
    CQE_USE_XVFB=1

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install chromium

COPY . .

CMD ["sh", "-c", "xvfb-run -a gunicorn -w 1 -b 0.0.0.0:${PORT:-8080} app_web:app"]
