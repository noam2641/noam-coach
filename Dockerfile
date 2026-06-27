FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl fonts-noto-core tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.lock

COPY . /app

RUN useradd --create-home --uid 10001 coach \
    && mkdir -p /data /storage /backups /tmp/matplotlib \
    && chown -R coach:coach /app /data /storage /backups /tmp/matplotlib

USER coach

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/healthz || exit 1

CMD ["python", "coach_bot.py"]
