#!/bin/bash
# Install (not start) the continuous workers next to the legacy poll timer.
# Run on the VM as root from a directory containing compose.workers.yaml.
# Nothing changes for the running pipeline until `workers.sh trial|enable`.
set -euo pipefail
[[ $EUID == 0 ]]
install -m 600 compose.workers.yaml /opt/jobradar/
install -m 700 workers.sh /opt/jobradar/
install -m 600 recovery.py runtime_control.py /opt/jobradar/
cat > /etc/tmpfiles.d/jobradar.conf <<'TMP'
d /run/jobradar 0755 root root -
d /run/jobradar/status 0755 10001 10001 -
TMP
systemd-tmpfiles --create /etc/tmpfiles.d/jobradar.conf
cat > /etc/systemd/system/jobradar-workers.service <<'UNIT'
[Unit]
Description=JobRadar continuous collectors, filter, matching, and Telegram outbox workers
Requires=jobradar-stack.service
After=jobradar-stack.service
ConditionPathExists=/var/lib/jobradar/migration-complete
StartLimitIntervalSec=1800
StartLimitBurst=3

[Service]
WorkingDirectory=/opt/jobradar
# Shared maintenance lock: the legacy poll (exclusive, non-blocking) skips and
# compaction waits while workers run. Online backups share this lock.
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
# Clear the old stop/resume hooks. The backup now drains only heavy work.
ExecStartPre=
ExecStopPost=
UNIT
cat > /etc/systemd/system/jobradar-recovery.service <<'UNIT'
[Unit]
Description=Verify and recover JobRadar worker and Telegram listener
After=jobradar-stack.service
ConditionPathExists=/var/lib/jobradar/migration-complete
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/jobradar/recovery.py check
TimeoutStartSec=300
UNIT
cat > /etc/systemd/system/jobradar-recovery.timer <<'UNIT'
[Unit]
Description=Check local JobRadar liveness every minute
[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
echo 'Workers installed but not started. Use /opt/jobradar/workers.sh trial|enable|rollback|status.'
