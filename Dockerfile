# Phase 5: the gateway's first Dockerfile. Phases 1-4 never needed one --
# the entire test suite runs in-process against fakeredis (see
# tests/unit/conftest.py) -- but "docker-compose up" containerizing the
# real gateway is Document 06 Phase 5's own build task.
#
# Two stages: `builder` compiles wheels once (pip's build cache layer),
# `runtime` copies only the installed packages + app code, never the
# build toolchain -- keeps the final image meaningfully smaller without
# hand-rolling apt-get purge choreography.

FROM python:3.14-slim AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.14-slim AS runtime

# curl: used only by the HEALTHCHECK below (compose's own
# service_healthy condition reads this, not just a human running
# `docker ps`) -- everything else is what pip already installed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /srv
COPY app/ ./app/
COPY config/ ./config/
# scripts/ is only actually invoked by the one-shot `demo-seed` service in
# docker-compose.yml (it reuses this same image rather than a second
# build -- see that file's comments) -- shipping it here rather than a
# separate image keeps this a single Dockerfile for both roles.
COPY scripts/ ./scripts/

# A non-root user -- the gateway process never needs root, and running
# as root in a container that terminates real (if mocked, in Phase 5)
# API keys is worth avoiding even for a portfolio project.
RUN useradd --create-home --uid 1000 gateway \
    && chown -R gateway:gateway /srv
USER gateway

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=6 \
    CMD curl -f http://localhost:8000/readyz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
