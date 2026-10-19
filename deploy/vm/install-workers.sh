#!/bin/bash
# Install (not start) the continuous workers next to the legacy poll timer.
# Run on the VM as root from a directory containing compose.workers.yaml.
# Nothing changes for the running pipeline until `workers.sh trial|enable`.
set -euo pipefail
[[ $EUID == 0 ]]
install -m 600 compose.workers.yaml /opt/jobradar/
install -m 700 workers.sh /opt/jobradar/
cat > /etc/systemd/system/jobradar-workers.service <<'UNIT'
[Unit]
Description=JobRadar continuous collectors, filter, matching, and Telegram outbox workers
Requires=jobradar-stack.service
After=jobradar-stack.service
ConditionPathExists=/var/lib/jobradar/migration-complete

[Service]
WorkingDirectory=/opt/jobradar
# Shared maintenance lock: the legacy poll (exclusive, non-blocking) skips and
# maintenance scripts wait while workers run. Backups stop the workers first.
ExecStart=/usr/bin/flock --shared /var/lib/jobradar/maintenance.lock /usr/bin/docker compose -f compose.yaml -f compose.workers.yaml run --rm --no-deps --name jobradar-worker worker
# Graceful: finish/release in-flight work; unfinished leases expire anyway.
ExecStop=-/usr/bin/docker stop --time 120 jobradar-worker
ExecStopPost=-/usr/bin/docker rm -f jobradar-worker
TimeoutStopSec=180
Restart=on-failure
RestartSec=30
# 75: the legacy cycle held the pipeline lock; retry after it finishes.
RestartForceExitStatus=75

[Install]
WantedBy=multi-user.target
UNIT
mkdir -p /etc/systemd/system/jobradar-backup.service.d
cat > /etc/systemd/system/jobradar-backup.service.d/10-workers.conf <<'UNIT'
[Service]
# The dump needs the workers' memory and the exclusive maintenance lock.
# Resume them afterwards only if they were running (trial or enabled).
ExecStartPre=/bin/sh -c 'if systemctl is-active --quiet jobradar-workers.service; then touch /run/jobradar-workers-resume; systemctl stop jobradar-workers.service; fi; true'
ExecStopPost=/bin/sh -c 'if [ -e /run/jobradar-workers-resume ]; then rm -f /run/jobradar-workers-resume; systemctl start --no-block jobradar-workers.service; fi'
UNIT
systemctl daemon-reload
echo 'Workers installed but not started. Use /opt/jobradar/workers.sh trial|enable|rollback|status.'
