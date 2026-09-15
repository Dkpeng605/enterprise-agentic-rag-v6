# syntax=docker/dockerfile:1.7

ARG VCS_REF=unknown
ARG IMAGE_VERSION=0.1.0

FROM node:22-bookworm-slim AS build

RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json /app/frontend/package.json
RUN pnpm install --frozen-lockfile --filter enterprise-agentic-rag-v6-frontend
COPY frontend /app/frontend
RUN pnpm --dir /app/frontend build

FROM caddy:2.10-alpine

ARG VCS_REF
ARG IMAGE_VERSION
LABEL org.opencontainers.image.title="enterprise-agentic-rag-frontend" \
      org.opencontainers.image.version="$IMAGE_VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.source="https://github.com/Dkpeng605/enterprise-agentic-rag-v6"

COPY --from=build /app/frontend/dist /srv
COPY infra/production/Caddyfile /etc/caddy/Caddyfile
RUN addgroup -S app \
    && adduser -S -D -H -u 10001 -G app app \
    && mkdir -p /data /config \
    && chown -R app:app /srv /etc/caddy /data /config

USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q --spider http://127.0.0.1:8080/ || exit 1

CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
