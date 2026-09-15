#!/usr/bin/env bash

# Deploy or roll back one immutable production image pair on a pre-provisioned host.
# The host keeps secrets in infra/production/.env.production; this script never prints it.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  production-deploy.sh deploy <release-sha> <backend-image> <frontend-image>
  production-deploy.sh rollback

The working directory is the deployment root and must contain
infra/production/compose.yml and infra/production/.env.production.
EOF
}

die() {
  printf 'production deployment failed: %s\n' "$*" >&2
  exit 1
}

env_value() {
  local key="$1"
  local file="$2"
  awk -v key="$key" '
    index($0, key "=") == 1 {
      print substr($0, length(key) + 2)
      found = 1
      exit
    }
    END { if (!found) exit 1 }
  ' "$file"
}

require_env_value() {
  local key="$1"
  local file="$2"
  local value
  value="$(env_value "$key" "$file")" || die "missing ${key} in ${file}"
  [[ -n "$value" ]] || die "empty ${key} in ${file}"
  printf '%s' "$value"
}

set_env_value() {
  local key="$1"
  local value="$2"
  local file="$3"
  local temporary
  temporary="$(mktemp "${file}.tmp.XXXXXX")"
  if ! awk -v key="$key" -v value="$value" '
    index($0, key "=") == 1 {
      print key "=" value
      found = 1
      next
    }
    { print }
    END { if (!found) exit 1 }
  ' "$file" > "$temporary"; then
    rm -f -- "$temporary"
    die "missing ${key} in ${file}"
  fi
  chmod 600 "$temporary"
  mv -- "$temporary" "$file"
}

valid_sha() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

valid_image_for_sha() {
  local image="$1"
  local sha="$2"
  [[ "$image" =~ ^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+:${sha}$ ]]
}

valid_deploy_path() {
  [[ "$1" == /* && "$1" != *[!a-zA-Z0-9_./-]* ]]
}

require_runtime() {
  local root="$1"
  valid_deploy_path "$root" || die "deployment root must be an absolute path without shell metacharacters"
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  command -v curl >/dev/null 2>&1 || die "curl is not installed"

  compose_file="${root}/infra/production/compose.yml"
  env_file="${root}/infra/production/.env.production"
  state_file="${root}/.deploy-state"
  [[ -f "$compose_file" ]] || die "missing ${compose_file}"
  [[ -f "$env_file" ]] || die "missing ${env_file}"
  docker compose version >/dev/null 2>&1 || die "docker compose is not available"

  public_domain="$(require_env_value PUBLIC_DOMAIN "$env_file")"
  [[ "$public_domain" =~ ^[A-Za-z0-9.-]+$ ]] || die "PUBLIC_DOMAIN is not a hostname"
  compose config --quiet || die "production compose configuration is invalid"
}

compose() {
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
}

cleanup() {
  local status=$?
  if [[ "$status" != 0 && "${env_changed:-0}" == 1 && -n "${env_backup:-}" && -f "$env_backup" ]]; then
    # An application failure must not leave the host pointing at a half-deployed image.
    # Migration state is intentionally untouched; only the image references are restored.
    set +e
    cp -- "$env_backup" "$env_file"
    if valid_sha "${failure_sha:-}" \
      && valid_image_for_sha "${failure_backend:-}" "$failure_sha" \
      && valid_image_for_sha "${failure_frontend:-}" "$failure_sha"; then
      compose pull api worker frontend >/dev/null 2>&1
      compose up -d api worker frontend gateway >/dev/null 2>&1
    fi
    set -e
  fi
  if [[ -n "${smoke_dir:-}" && -d "$smoke_dir" ]]; then
    rm -rf -- "$smoke_dir"
  fi
  if [[ -n "${env_backup:-}" && -f "$env_backup" ]]; then
    rm -f -- "$env_backup"
  fi
  if [[ -n "${backup_file:-}" && "${backup_created:-0}" != 1 && -f "$backup_file" ]]; then
    rm -f -- "$backup_file"
  fi
  exit "$status"
}

smoke() {
  smoke_dir="$(mktemp -d "${TMPDIR:-/tmp}/enterprise-rag-smoke.XXXXXX")"
  local cookie_file="${smoke_dir}/cookies.txt"
  local base_url="https://${public_domain}"
  local -a curl_options=(
    --fail --silent --show-error --insecure --max-time 15
    --retry 12 --retry-delay 2 --retry-max-time 60 --retry-connrefused
    --resolve "${public_domain}:443:127.0.0.1"
  )

  curl "${curl_options[@]}" "${base_url}/health/live" -o /dev/null
  curl "${curl_options[@]}" --cookie-jar "$cookie_file" \
    "${base_url}/api/v1/auth/me" -o /dev/null
  curl "${curl_options[@]}" --cookie "$cookie_file" \
    "${base_url}/api/v1/workspace/overview" -o /dev/null

  # Keep the credential inside the already running API container. It is never put in
  # a remote command, a log line, or a host process argument.
  compose exec -T api python - <<'PY'
import json
import os
import urllib.request

payload = json.dumps(
    {
        "email": os.environ["ADMIN_BOOTSTRAP_EMAIL"],
        "password": os.environ["ADMIN_BOOTSTRAP_PASSWORD"],
    }
).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/auth/login",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=15) as response:
    body = json.load(response)
if body.get("actor_type") != "user" or body.get("role") != "super_admin":
    raise SystemExit("administrator smoke did not return a super_admin session")
PY
}

write_state() {
  local current_sha="$1"
  local current_backend="$2"
  local current_frontend="$3"
  local previous_sha="$4"
  local previous_backend="$5"
  local previous_frontend="$6"
  local temporary
  temporary="$(mktemp "${state_file}.tmp.XXXXXX")"
  {
    printf 'current_sha=%s\n' "$current_sha"
    printf 'current_backend_image=%s\n' "$current_backend"
    printf 'current_frontend_image=%s\n' "$current_frontend"
    printf 'previous_sha=%s\n' "$previous_sha"
    printf 'previous_backend_image=%s\n' "$previous_backend"
    printf 'previous_frontend_image=%s\n' "$previous_frontend"
  } > "$temporary"
  chmod 600 "$temporary"
  mv -- "$temporary" "$state_file"
}

state_value() {
  env_value "$1" "$state_file"
}

deploy() {
  local root="$1"
  local release_sha="$2"
  local backend_image="$3"
  local frontend_image="$4"
  valid_sha "$release_sha" || die "release SHA must be a 40-character lowercase commit SHA"
  valid_image_for_sha "$backend_image" "$release_sha" || die "backend image is not an immutable GHCR image for this SHA"
  valid_image_for_sha "$frontend_image" "$release_sha" || die "frontend image is not an immutable GHCR image for this SHA"

  require_runtime "$root"
  previous_sha="$(env_value APP_COMMIT_SHA "$env_file" || true)"
  previous_backend="$(env_value BACKEND_IMAGE "$env_file" || true)"
  previous_frontend="$(env_value FRONTEND_IMAGE "$env_file" || true)"
  if ! valid_sha "$previous_sha" || ! valid_image_for_sha "$previous_backend" "$previous_sha" \
    || ! valid_image_for_sha "$previous_frontend" "$previous_sha"; then
    previous_sha=''
    previous_backend=''
    previous_frontend=''
  fi

  env_backup="$(mktemp "${env_file}.backup.XXXXXX")"
  cp -- "$env_file" "$env_backup"
  failure_sha="$previous_sha"
  failure_backend="$previous_backend"
  failure_frontend="$previous_frontend"
  backup_created=0
  backup_file="${root}/backups/deploy/pre-deploy-${release_sha}-$(date -u +%Y%m%dT%H%M%SZ).dump"
  mkdir -p -- "${root}/backups/deploy"
  chmod 700 "${root}/backups/deploy"

  # Keep the database snapshot before changing image references or applying a migration.
  compose up -d postgres
  compose exec -T postgres sh -c \
    'pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    > "$backup_file"
  [[ -s "$backup_file" ]] || die "PostgreSQL pre-deploy backup is empty"
  chmod 600 "$backup_file"
  backup_created=1

  env_changed=1
  set_env_value APP_COMMIT_SHA "$release_sha" "$env_file"
  set_env_value BACKEND_IMAGE "$backend_image" "$env_file"
  set_env_value FRONTEND_IMAGE "$frontend_image" "$env_file"

  compose config --quiet || die "updated production compose configuration is invalid"
  compose pull api worker frontend
  compose run --rm migrate
  compose up -d api worker frontend gateway
  smoke
  write_state "$release_sha" "$backend_image" "$frontend_image" \
    "$previous_sha" "$previous_backend" "$previous_frontend"
  printf 'production deployment succeeded: %s\n' "$release_sha"
}

rollback() {
  local root="$1"
  require_runtime "$root"
  [[ -f "$state_file" ]] || die "no deployment state exists; rollback needs a previous release"

  current_sha="$(state_value current_sha || true)"
  current_backend="$(state_value current_backend_image || true)"
  current_frontend="$(state_value current_frontend_image || true)"
  previous_sha="$(state_value previous_sha || true)"
  previous_backend="$(state_value previous_backend_image || true)"
  previous_frontend="$(state_value previous_frontend_image || true)"
  valid_sha "$current_sha" || die "deployment state current SHA is invalid"
  valid_sha "$previous_sha" || die "deployment state has no previous release"
  valid_image_for_sha "$current_backend" "$current_sha" || die "deployment state current backend image is invalid"
  valid_image_for_sha "$current_frontend" "$current_sha" || die "deployment state current frontend image is invalid"
  valid_image_for_sha "$previous_backend" "$previous_sha" || die "deployment state previous backend image is invalid"
  valid_image_for_sha "$previous_frontend" "$previous_sha" || die "deployment state previous frontend image is invalid"

  env_backup="$(mktemp "${env_file}.backup.XXXXXX")"
  cp -- "$env_file" "$env_backup"
  failure_sha="$current_sha"
  failure_backend="$current_backend"
  failure_frontend="$current_frontend"
  env_changed=1
  set_env_value APP_COMMIT_SHA "$previous_sha" "$env_file"
  set_env_value BACKEND_IMAGE "$previous_backend" "$env_file"
  set_env_value FRONTEND_IMAGE "$previous_frontend" "$env_file"

  compose config --quiet || die "rollback compose configuration is invalid"
  compose pull api worker frontend
  # There is deliberately no migration downgrade here. The deployment contract requires
  # forward-compatible migrations; rollback only changes immutable application image refs.
  compose up -d api worker frontend gateway
  smoke
  write_state "$previous_sha" "$previous_backend" "$previous_frontend" \
    "$current_sha" "$current_backend" "$current_frontend"
  printf 'production rollback succeeded: %s\n' "$previous_sha"
}

main() {
  local action="${1:-}"
  local root="${DEPLOY_PATH:-/opt/enterprise-agentic-rag-v6}"
  case "$action" in
    deploy)
      [[ $# -eq 4 ]] || { usage >&2; exit 2; }
      deploy "$root" "$2" "$3" "$4"
      ;;
    rollback)
      [[ $# -eq 1 ]] || { usage >&2; exit 2; }
      rollback "$root"
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

trap cleanup EXIT
main "$@"
