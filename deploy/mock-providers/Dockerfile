# Standalone image -- no build context beyond this directory (see its
# reference in docker-compose.yml: `build: { context: ./deploy/mock-providers }`),
# so it never accidentally picks up the gateway's own app/ package.
FROM python:3.13-slim

WORKDIR /srv

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY chaos.py main.py ./

EXPOSE 9000

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD curl -f http://localhost:9000/healthz || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
