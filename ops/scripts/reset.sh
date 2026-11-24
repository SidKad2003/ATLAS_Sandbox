#!/usr/bin/env bash
# Destroys all svc-sim state: containers, named volumes and the runtime flag file. No backup is taken.
set -euo pipefail
cd "$(dirname "$0")/../.."
read -r -p "This deletes all svc-sim runs, memories and approvals. Type svc-sim to continue: " ans
[ "$ans" = "svc-sim" ] || { echo "aborted"; exit 1; }
make --no-print-directory down-svc-sim || true
docker volume rm svc-sim-agent-memory svc-sim-policy-store 2>/dev/null || true
rm -rf runtime/flagd
echo "svc-sim reset complete"
