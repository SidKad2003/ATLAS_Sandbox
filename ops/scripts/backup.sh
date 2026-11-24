#!/usr/bin/env bash
# Back up all svc-sim data to S3 (instance role only, no keys). Safe to run while the stack is up.
#   s3://$BUCKET/svc-sim/backups/<UTC timestamp>/
# Local passwords (ops/secrets) are deliberately not backed up; they are regenerated on a new host.
set -euo pipefail
cd "$(dirname "$0")/../.."
BUCKET="${SVC_SIM_BACKUP_BUCKET:-your-s3-bucket}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="s3://$BUCKET/svc-sim/backups/$TS"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 777 "$WORK"
step() { echo "[$(date -u +%H:%M:%S)] $*"; }

step "agent-memory database (runs, replays, memories, approvals, incidents, fraud cases)"
docker exec svc-sim-agent-memory pg_dump -U svc_sim -d svc_sim -Fc > "$WORK/agent-memory.pgdump"

step "shop database (orders and accounting)"
docker exec svc-sim-astronomy-db pg_dump -U postgres -d astronomy_db -Fc > "$WORK/astronomy-db.pgdump"

step "policy-store volume"
docker run --rm -v svc-sim-policy-store:/data:ro svc-sim/py:dev tar czf - -C /data . > "$WORK/policy-store.tar.gz"

step "runtime flag file"
cp runtime/flagd/demo.flagd.json "$WORK/demo.flagd.json"

step "Prometheus metrics store"
docker exec svc-sim-prometheus tar czf - -C /prometheus . > "$WORK/prometheus-tsdb.tar.gz" 2>/dev/null || echo "  prometheus copy incomplete (store was being written); kept what was readable"

step "Jaeger traces and OpenSearch logs"
docker run --rm --network svc-sim -e OTEL_SDK_DISABLED=true -v "$PWD/ops/scripts/export_telemetry.py:/export.py:ro" -v "$WORK:/out" \
  svc-sim/py:dev python /export.py /out

step "manifest"
{
  echo "{"
  echo "  \"created_utc\": \"$TS\","
  echo "  \"git_commit\": \"$(git rev-parse HEAD)\","
  echo "  \"runs\": $(docker exec svc-sim-agent-memory psql -U svc_sim -d svc_sim -At -c "select coalesce(json_agg(json_build_object('run_id',run_id,'status',status,'seed',seed,'label',config->>'label','created_at',created_at) order by created_at),'[]') from runs"),"
  echo "  \"viz_events\": $(docker exec svc-sim-agent-memory psql -U svc_sim -d svc_sim -At -c 'select count(*) from viz_events'),"
  echo "  \"images\": $(docker ps --filter label=com.docker.compose.project=svc-sim --format '{{json .Image}}' | sort -u | paste -sd, | sed 's/^/[/; s/$/]/')"
  echo "}"
} > "$WORK/manifest.json"
(cd "$WORK" && sha256sum ./* > SHA256SUMS)

step "upload to $DEST/"
aws s3 cp --recursive --only-show-errors --sse AES256 "$WORK" "$DEST/"
aws s3 ls "$DEST/" --human-readable
echo "$DEST" > runtime/last-backup
step "backup complete: $DEST/"
