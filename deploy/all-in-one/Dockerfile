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
#   docker build -f deploy/all-in-one/Dockerfile -t dnotitia/akb .
# Run (MCP introspection works without any external API key):
#   docker run --rm -p 8080:8080 dnotitia/akb
# Run with embeddings/search enabled:
#   docker run --rm -p 8080:8080 \
#       -e EMBED_API_KEY=sk-... \
#       -e EMBED_BASE_URL=https://api.openai.com/v1 \
#       -e EMBED_MODEL=text-embedding-3-small \
#       -e EMBED_DIMENSIONS=1536 \
#       dnotitia/akb

# ---------- frontend build ----------
FROM node:22-slim AS frontend-builder

WORKDIR /src
RUN npm install -g pnpm@latest

COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY frontend/ ./
RUN npx vite build

# ---------- final image ----------
FROM pgvector/pgvector:pg16

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

# MinIO server + mc client (bucket bootstrap)
RUN curl -fsSL https://dl.min.io/server/minio/release/linux-amd64/minio \
        -o /usr/local/bin/minio && chmod +x /usr/local/bin/minio && \
    curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc \
        -o /usr/local/bin/mc && chmod +x /usr/local/bin/mc

# Backend deps in an isolated venv so PEP 668 doesn't bite us.
RUN python3 -m venv /opt/venv && pip install --no-cache-dir --upgrade pip

COPY backend/pyproject.toml /tmp/akb-backend/pyproject.toml
COPY backend/app /tmp/akb-backend/app
COPY backend/mcp_server /tmp/akb-backend/mcp_server
RUN pip install --no-cache-dir /tmp/akb-backend && rm -rf /tmp/akb-backend

# Backend source (used at runtime — venv only carries dependencies).
COPY backend /app/backend

# Frontend static bundle.
COPY --from=frontend-builder /src/dist /var/www/akb

# All-in-one bundle config + supervisord + nginx + entrypoint.
COPY deploy/all-in-one/app.yaml /etc/akb/app.yaml
COPY deploy/all-in-one/secret.yaml.template /etc/akb/secret.yaml.template
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
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
