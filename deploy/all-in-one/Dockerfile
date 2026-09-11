# AKB all-in-one image — every component in one container.
#
# Use cases:
#   * Glama MCP introspection / listing
#   * Quick demos (single `docker run` brings up the full stack)
#   * Self-hosted single-box deployments
#
# Bundles:
#   * PostgreSQL 16 + pgvector (main DB + derived vector index schema)
#   * Redis (event stream fanout)
#   * MinIO + mc (S3-compatible object storage for file uploads)
#   * Backend (FastAPI + MCP Streamable HTTP)
#   * Frontend (Vite-built SPA served by nginx)
#   * nginx reverse proxy on :8080 — routes /, /api, /mcp, /akb-files
#
# Boot flow is supervisord-managed. See entrypoint.sh + supervisord.conf.
#
# Build:
#   docker build -f deploy/all-in-one/Dockerfile -t dnseahorse/akb .
# Run (MCP introspection works without any external API key):
#   docker run --rm -p 8080:8080 dnseahorse/akb
# Run with embeddings/search enabled:
#   docker run --rm -p 8080:8080 \
#       -e EMBED_API_KEY=sk-... \
#       -e EMBED_BASE_URL=https://api.openai.com/v1 \
#       -e EMBED_MODEL=text-embedding-3-small \
#       -e EMBED_DIMENSIONS=1536 \
#       dnseahorse/akb

# ---------- frontend build ----------
FROM node:22-slim AS frontend-builder

WORKDIR /src
COPY pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json frontend/pnpm-lock.yaml ./frontend/
COPY packages/markdown-editor/package.json packages/markdown-editor/pnpm-lock.yaml packages/markdown-editor/tsconfig.json packages/markdown-editor/tsconfig.build.json ./packages/markdown-editor/
COPY packages/markdown-editor/src ./packages/markdown-editor/src
RUN corepack enable && corepack prepare "$(node -p 'require("./frontend/package.json").packageManager')" --activate
# The workspace install runs the common package's prepare build before Vite;
# the workspace allowBuilds list stays limited to esbuild/msw.
RUN pnpm install --frozen-lockfile

COPY frontend/ ./frontend/
RUN pnpm --filter frontend run build

# ---------- final image ----------
FROM pgvector/pgvector:pg16
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        redis-server \
        nginx \
        supervisor \
        git \
        curl ca-certificates \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# MinIO server + mc client (bucket bootstrap), pinned to dated archive releases.
# minio sha256: 7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f
# mc sha256: 01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891
ARG MINIO_RELEASE=RELEASE.2025-09-07T16-13-09Z
ARG MC_RELEASE=RELEASE.2025-08-13T08-35-41Z
RUN curl -fsSL https://dl.min.io/server/minio/release/linux-${TARGETARCH}/archive/minio.${MINIO_RELEASE} \
        -o /usr/local/bin/minio && \
    echo "7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f  /usr/local/bin/minio" | sha256sum -c - && \
    chmod +x /usr/local/bin/minio && \
    curl -fsSL https://dl.min.io/client/mc/release/linux-${TARGETARCH}/archive/mc.${MC_RELEASE} \
        -o /usr/local/bin/mc && \
    echo "01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891  /usr/local/bin/mc" | sha256sum -c - && \
    chmod +x /usr/local/bin/mc

# Backend requires Python >=3.14 (backend/pyproject.toml). The pgvector
# base image (debian bookworm) only ships system python 3.11 — kept
# above for entrypoint.sh's stdlib scripting (token gen + config
# rendering). uv provisions a standalone CPython 3.14 for the backend's
# /opt/venv so `pip install akb` actually resolves; building against the
# bookworm 3.11 fails with "requires a different Python: 3.11 not in
# '>=3.14'" — the all-in-one was unbuildable from 0.6.5's 3.14 bump
# until this.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH=/root/.local/bin:$PATH
RUN uv venv --python 3.14 /opt/venv

COPY backend/pyproject.toml /tmp/akb-backend/pyproject.toml
COPY backend/app /tmp/akb-backend/app
COPY backend/mcp_server /tmp/akb-backend/mcp_server
RUN uv pip install --python /opt/venv/bin/python --no-cache /tmp/akb-backend \
    && rm -rf /tmp/akb-backend

# Backend source (used at runtime — venv only carries dependencies).
COPY backend /app/backend
# Drop any locally-rendered backend/config/ that leaked into the build
# context. The all-in-one image must use /etc/akb/{app,secret}.yaml,
# rendered by entrypoint.sh — a stray ./config/app.yaml short-circuits
# `config.py:_CONFIG_CANDIDATES` and starves backend of secret.yaml.
RUN rm -rf /app/backend/config

# Frontend static bundle.
COPY --from=frontend-builder /src/frontend/dist /var/www/akb

# All-in-one bundle config + supervisord + nginx + entrypoint.
COPY deploy/all-in-one/app.yaml /opt/akb/app.yaml
COPY deploy/all-in-one/configure.py /usr/local/bin/akb-demo-configure.py
COPY deploy/all-in-one/supervisord.conf /etc/supervisor/conf.d/akb.conf
COPY deploy/all-in-one/nginx.conf /etc/nginx/sites-available/akb
COPY deploy/all-in-one/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY deploy/all-in-one/seed.py /usr/local/bin/akb-seed.py
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    rm -f /etc/nginx/sites-enabled/default && \
    ln -s /etc/nginx/sites-available/akb /etc/nginx/sites-enabled/akb

# Data + runtime dirs (postgres user is created by the pgvector base image).
RUN mkdir -p /data/vaults /data/minio /data/pgsql /var/log/akb /var/lib/akb && \
    chown -R postgres:postgres /data/pgsql && \
    chown -R www-data:www-data /var/www/akb

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/readyz >/dev/null && /opt/venv/bin/python -m app.worker_health
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
