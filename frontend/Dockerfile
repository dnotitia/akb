FROM node:22-slim AS builder

RUN npm install -g pnpm@latest

WORKDIR /app
COPY package.json pnpm-lock.yaml ./
# `--ignore-scripts` skips devDependency postinstall hooks (e.g. msw's
# service-worker bundle registration). The image only runs the built
# SPA via nginx — none of those hooks are needed at runtime, and pnpm
# 10+ otherwise exits non-zero when it sees them.
RUN pnpm install --frozen-lockfile --ignore-scripts

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
