FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATABASE=/data/greatgatsby.db

WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY household_features.py ./
COPY budget_backup.py ./
COPY planning_features.py ./
COPY desktop_features.py ./
COPY templates ./templates
COPY static ./static
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data && chown -R app:app /app /data \
    && chmod 755 /usr/local/bin/docker-entrypoint.sh
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--access-logfile", "-", "app:app"]
