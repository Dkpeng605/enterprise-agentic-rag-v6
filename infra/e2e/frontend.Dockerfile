FROM node:22-bookworm-slim

RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json /app/frontend/package.json
RUN pnpm install --frozen-lockfile --filter enterprise-agentic-rag-v6-frontend
COPY frontend /app/frontend

WORKDIR /app/frontend
CMD ["pnpm", "dev", "--host", "0.0.0.0", "--port", "4173"]
