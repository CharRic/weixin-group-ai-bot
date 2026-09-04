#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec docker compose \
  --project-directory "$project_root" \
  --env-file "$project_root/.env" \
  -f "$project_root/compose.yaml" \
  "$@"
