FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# FreeTDS for pymssql; curl for the container health check.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential freetds-dev freetds-bin libssl-dev libffi-dev ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip wheel && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --system --uid 10001 app && mkdir -p /app/logs && chown -R app /app/logs
USER app

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://localhost:8001/health || exit 1

# The same image runs the sync worker with:
#   python -m services.loyaltri.loyaltri_scheduler
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--proxy-headers", "--forwarded-allow-ips", "*"]
