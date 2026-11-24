#!/usr/bin/env bash
# Restore svc-sim data from an S3 backup made by backup.sh. The stack must be up (make up-svc-sim).
#   bash ops/scripts/restore.sh s3://<bucket>/svc-sim/backups/<timestamp>   [--with-shop-db]
# Restores agent-memory (replaces its contents), policy-store and the flag file. The shop database is
# only restored with --with-shop-db. Traces, logs and metrics are archives for analysis and are not loaded back.
set -euo pipefail
cd "$(dirname "$0")/../.."
SRC="${1:?usage: restore.sh s3://bucket/svc-sim/backups/<timestamp> [--with-shop-db]}"
WITH_SHOP="${2:-}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
read -r -p "This REPLACES current svc-sim run data with $SRC. Type restore to continue: " ans
[ "$ans" = "restore" ] || { echo "aborted"; exit 1; }

aws s3 cp --recursive --only-show-errors --exclude "*" --include "agent-memory.pgdump" --include "astronomy-db.pgdump" \
  --include "policy-store.tar.gz" --include "demo.flagd.json" --include "SHA256SUMS" "${SRC%/}/" "$WORK/"
(cd "$WORK" && grep -E "agent-memory|astronomy-db|policy-store|demo.flagd" SHA256SUMS | sha256sum -c -)

echo "restoring agent-memory"
docker exec -i svc-sim-agent-memory pg_restore -U svc_sim -d svc_sim --clean --if-exists --no-owner < "$WORK/agent-memory.pgdump"

if [ "$WITH_SHOP" = "--with-shop-db" ]; then
  echo "restoring shop database"
  docker exec -i svc-sim-astronomy-db pg_restore -U postgres -d astronomy_db --clean --if-exists < "$WORK/astronomy-db.pgdump"
fi

echo "restoring policy-store"
docker run --rm -i -v svc-sim-policy-store:/data svc-sim/py:dev tar xzf - -C /data < "$WORK/policy-store.tar.gz"

echo "restoring flag file"
cp "$WORK/demo.flagd.json" runtime/flagd/demo.flagd.json && chmod 0666 runtime/flagd/demo.flagd.json

echo "restore complete. Run make up-svc-sim to make sure every service is healthy."
