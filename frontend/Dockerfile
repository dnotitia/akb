FROM node:22-slim AS builder

WORKDIR /app
COPY pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json frontend/pnpm-lock.yaml ./frontend/
COPY packages/markdown-editor/package.json packages/markdown-editor/pnpm-lock.yaml packages/markdown-editor/tsconfig.json packages/markdown-editor/tsconfig.build.json ./packages/markdown-editor/
COPY packages/markdown-editor/src ./packages/markdown-editor/src
RUN corepack enable && corepack prepare "$(node -p 'require("./frontend/package.json").packageManager')" --activate
# The workspace install runs the common package's prepare build before the
# frontend bundle; the workspace allowBuilds list stays limited to esbuild/msw.
RUN pnpm install --frozen-lockfile

COPY frontend ./frontend
RUN pnpm --filter frontend run build

FROM nginx:alpine
COPY --from=builder /app/frontend/dist /usr/share/nginx/html

# nginx config lives in a tracked file (frontend/nginx.conf) — it carries
# the SPA fallback, the backend proxies, and the asset cache/404 rules
# (missing hashed chunks must 404, not fall back to index.html).
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
