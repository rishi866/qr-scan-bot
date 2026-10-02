# Builds the admin panel (static export) and serves it with Caddy, which also terminates TLS
# (automatic Let's Encrypt certificate) and proxies /api to the backend. Build context: the repository root.
FROM node:22-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/out /srv/panel
COPY deploy/Caddyfile /etc/caddy/Caddyfile
