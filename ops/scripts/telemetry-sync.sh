#!/usr/bin/env bash
# Upload the collector's full-fidelity telemetry files to S3 (instance role, no keys). Run by the
# svc-sim-telemetry-sync timer every 15 minutes, or by hand with `make telemetry-sync`.
#   s3://$BUCKET/svc-sim/telemetry/<instance id>/{traces,metrics,logs}*.jsonl.zst
set -euo pipefail
ROOT="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
SRC="$ROOT/runtime/telemetry"
BUCKET="${SVC_SIM_BACKUP_BUCKET:-your-s3-bucket}"
TOKEN=$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
IID=$(curl -s -m 3 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id || hostname)
DEST="s3://$BUCKET/svc-sim/telemetry/$IID/"
[ -d "$SRC" ] || { echo "no telemetry directory at $SRC"; exit 0; }
aws s3 sync "$SRC" "$DEST" --sse AES256 --only-show-errors --region eu-west-2
echo "$(date -u +%FT%TZ) synced $(find "$SRC" -type f | wc -l) files ($(du -sh "$SRC" | cut -f1)) to $DEST"
