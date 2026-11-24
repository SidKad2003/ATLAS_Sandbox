#!/usr/bin/env bash
# Install (or refresh) the host systemd timers. Run with sudo: make s3-timers
#   svc-sim-telemetry-sync: telemetry files to S3 every 15 minutes
#   svc-sim-backup:         full make backup nightly at 02:00 UTC (as ec2-user)
set -euo pipefail
ROOT="${1:?usage: install-timers.sh <repo root>}"
OWNER="$(stat -c %U "$ROOT")"

cat > /etc/systemd/system/svc-sim-telemetry-sync.service <<EOF
[Unit]
Description=svc-sim: upload collector telemetry files to S3
After=network-online.target docker.service
[Service]
Type=oneshot
ExecStart=/bin/bash $ROOT/ops/scripts/telemetry-sync.sh $ROOT
EOF
cat > /etc/systemd/system/svc-sim-telemetry-sync.timer <<'EOF'
[Unit]
Description=svc-sim: telemetry to S3 every 15 minutes
[Timer]
OnCalendar=*:0/15
Persistent=true
RandomizedDelaySec=60
[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/svc-sim-backup.service <<EOF
[Unit]
Description=svc-sim: full backup to S3
After=network-online.target docker.service
[Service]
Type=oneshot
User=$OWNER
WorkingDirectory=$ROOT
ExecStart=/usr/bin/make backup
EOF
cat > /etc/systemd/system/svc-sim-backup.timer <<'EOF'
[Unit]
Description=svc-sim: nightly backup at 02:00 UTC
[Timer]
OnCalendar=*-*-* 02:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now svc-sim-telemetry-sync.timer svc-sim-backup.timer
systemctl list-timers 'svc-sim-*' --no-pager
