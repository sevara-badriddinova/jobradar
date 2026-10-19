#!/bin/bash
set -euo pipefail
[[ $EUID == 0 ]]
install -m 600 compose.yaml cloud_io.py /opt/jobradar/
cat > /etc/systemd/system/jobradar-stack.service <<'EOF'
[Unit]
Description=JobRadar database and Telegram listener
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
ConditionPathExists=/var/lib/jobradar/migration-complete
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/jobradar
ExecStartPre=/usr/bin/python3 /opt/jobradar/cloud_io.py configure
ExecStart=/usr/bin/docker compose up -d --wait postgres listener
ExecStop=/usr/bin/docker compose stop listener postgres
TimeoutStartSec=300
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/jobradar-poll.service <<'EOF'
[Unit]
Description=JobRadar bounded scan, match, and notification cycle
Requires=jobradar-stack.service
After=jobradar-stack.service
ConditionPathExists=/var/lib/jobradar/migration-complete
[Service]
Type=oneshot
WorkingDirectory=/opt/jobradar
ExecStart=/usr/bin/flock --exclusive --nonblock --conflict-exit-code 0 /var/lib/jobradar/maintenance.lock /usr/bin/docker compose run --rm --no-deps --name jobradar-cycle poll
ExecStopPost=-/usr/bin/docker rm -f jobradar-cycle
TimeoutStartSec=2700
# Exit 1 can mean an isolated board failure. The independent heartbeat verifies
# that matching and notification stages really completed successfully.
SuccessExitStatus=1
EOF
cat > /etc/systemd/system/jobradar-poll.timer <<'EOF'
[Unit]
Description=Run JobRadar every five minutes without overlap
[Timer]
OnCalendar=*:0/5
Persistent=true
AccuracySec=5s
Unit=jobradar-poll.service
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/jobradar-backup.service <<'EOF'
[Unit]
Description=Private seven-day rotating PostgreSQL backups
Requires=jobradar-stack.service
After=jobradar-stack.service
ConditionPathExists=/var/lib/jobradar/migration-complete
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -u /opt/jobradar/cloud_io.py backup
# Includes waiting for a running scan, dump, archive validation, and upload.
TimeoutStartSec=9000
Nice=10
EOF
cat > /etc/systemd/system/jobradar-backup.timer <<'EOF'
[Unit]
Description=Daily JobRadar database backup
[Timer]
OnCalendar=*-*-* 07:17:00 UTC
Persistent=true
RandomizedDelaySec=300
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
echo 'Services installed, not enabled. Restore and verify data before cutover.'
