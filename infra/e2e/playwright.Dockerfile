FROM mcr.microsoft.com/playwright:v1.63.0-noble

RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json /app/frontend/package.json
RUN pnpm install --frozen-lockfile --filter enterprise-agentic-rag-v6-frontend
COPY frontend /app/frontend

WORKDIR /app/frontend
CMD ["pnpm", "test:e2e"]
