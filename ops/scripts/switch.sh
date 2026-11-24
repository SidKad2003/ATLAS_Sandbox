#!/usr/bin/env bash
# Switch the host between workloads. Only one runs at a time.
set -euo pipefail
cd "$(dirname "$0")/../.."
target="${1:?usage: switch.sh <svc-sim|agent-eval>}"
case "$target" in svc-sim) other=agent-eval ;; agent-eval) other=svc-sim ;; *) echo "unknown workload $target"; exit 1 ;; esac

echo "stopping $other"
if [ "$other" = svc-sim ]; then make --no-print-directory down-svc-sim; else
  docker compose -p agent-eval down --remove-orphans 2>/dev/null || true; fi

# verify the other workload is fully gone: no containers, ports free, memory released
left=$(docker ps -a --filter "label=com.docker.compose.project=$other" -q | wc -l)
[ "$left" -eq 0 ] || { echo "containers from $other still present"; exit 1; }
for p in 8080 8090 8091; do
  if ss -ltnH "sport = :$p" | grep -q .; then echo "port $p still in use"; exit 1; fi
done
avail_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
echo "memory available: ${avail_mb} MiB"
[ "$avail_mb" -gt 9000 ] || { echo "less than 9 GiB available after stopping $other"; exit 1; }

echo "starting $target"
make --no-print-directory "up-$target"
