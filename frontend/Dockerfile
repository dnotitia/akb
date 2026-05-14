FROM node:22-slim AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npx vite build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html

# SPA fallback. /api and /mcp are proxied to the backend service in k8s.
# If BACKEND_URL env is unset, falls back to expecting an external ingress.
RUN echo 'server { \
  listen 3000; \
  root /usr/share/nginx/html; \
  location / { try_files $uri $uri/ /index.html; } \
  location /api/ { proxy_pass http://backend:8000/api/; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; } \
  location /mcp/ { proxy_pass http://backend:8000/mcp/; proxy_set_header Host $host; } \
  location /health { proxy_pass http://backend:8000/health; } \
}' > /etc/nginx/conf.d/default.conf

EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
