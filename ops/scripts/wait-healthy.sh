#!/usr/bin/env bash
# Wait until every running container in compose project $1 is healthy (or has no healthcheck).
set -euo pipefail
project="${1:-svc-sim}"; timeout="${2:-600}"; start=$(date +%s)
while true; do
  bad=$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Names}} {{.Status}}' \
        | grep -vE '\(healthy\)|Exited \(0\)' | grep -E 'unhealthy|starting|Restarting|Exited|Created' || true)
  if [ -z "$bad" ]; then echo "all $project containers healthy"; exit 0; fi
  if [ $(( $(date +%s) - start )) -gt "$timeout" ]; then echo "timed out waiting for:"; echo "$bad"; exit 1; fi
  sleep 5
done
