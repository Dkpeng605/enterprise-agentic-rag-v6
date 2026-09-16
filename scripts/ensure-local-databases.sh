#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${repository_root}/infra/compose/compose.dev.yml"
database_names=("enterprise_rag_dev" "enterprise_rag_test")

# Older checkouts started the same file under the implicit Compose project name
# `compose`.  A second project cannot bind 55432, so first reuse a running
# PostgreSQL container that owns the documented local port.  The container is
# accepted only when Docker identifies it as the postgres service; an unrelated
# process occupying the port must still fail loudly.
postgres_container=""
while IFS= read -r candidate; do
  if [[ -n "${candidate}" ]] && [[ "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "${candidate}" 2>/dev/null || true)" == "postgres" ]]; then
    postgres_container="${candidate}"
    break
  fi
done < <(docker ps --filter "publish=55432" --format '{{.Names}}')

if [[ -z "${postgres_container}" ]]; then
  docker compose -f "${compose_file}" up -d postgres
  postgres_container="$(docker compose -f "${compose_file}" ps -q postgres)"
fi

if [[ -z "${postgres_container}" ]]; then
  echo "Could not resolve the local PostgreSQL container." >&2
  exit 1
fi

postgres_exec=(docker exec -i "${postgres_container}")

for attempt in {1..30}; do
  if "${postgres_exec[@]}" \
    psql -U enterprise_rag -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  if [[ "${attempt}" == "30" ]]; then
    echo "PostgreSQL did not become available." >&2
    exit 1
  fi
  sleep 1
done

for database_name in "${database_names[@]}"; do
  exists="$("${postgres_exec[@]}" \
    psql -U enterprise_rag -d postgres -Atc \
    "SELECT 1 FROM pg_database WHERE datname = '${database_name}'")"
  if [[ "${exists}" != "1" ]]; then
    "${postgres_exec[@]}" \
      createdb -U enterprise_rag -O enterprise_rag "${database_name}"
    echo "Created local PostgreSQL database: ${database_name}"
  fi
done
