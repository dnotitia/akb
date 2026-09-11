FROM node:22-slim AS builder

WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN corepack enable && corepack prepare "$(node -p 'require("./package.json").packageManager')" --activate
# The Git-pinned shared Markdown package runs its artifact-scoped prepare
# build here so the production bundle consumes the resolved dist/. The
# frontend workspace keeps third-party build approval narrow.
# pnpm's Git prepare invokes npm for this subdirectory. Keep the peer resolver
# workaround scoped to this install; it is not a global lifecycle allowance.
RUN NPM_CONFIG_LEGACY_PEER_DEPS=true pnpm install --frozen-lockfile

COPY . .
RUN npx vite build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html

# nginx config lives in a tracked file (frontend/nginx.conf) — it carries
# the SPA fallback, the backend proxies, and the asset cache/404 rules
# (missing hashed chunks must 404, not fall back to index.html).
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
