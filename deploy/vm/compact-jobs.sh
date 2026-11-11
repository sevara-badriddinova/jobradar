#!/bin/bash
# Prune and compact the jobs table in one exclusive rewrite (docs/27).
# Run on the VM as root, e.g. from a one-off timer in a quiet window:
#   systemd-run --unit jobradar-compact --on-calendar '2026-10-30 01:45:00 UTC' \
#     /opt/jobradar/compact-jobs.sh 270
# Stops the workers and the listener, waits for an idle database, rewrites
# `jobs` under the exclusive maintenance lock (one transaction: a failure or
# the time limit rolls it back), then restarts whatever was running.
set -euo pipefail
[[ $EUID == 0 ]]
cd /opt/jobradar
MAX_MINUTES=${1:-270}
log() { echo "$(date -u +%FT%TZ) $*"; }
psql() { docker compose exec -T postgres psql -U jobradar -d jobradar -Atc "$1" </dev/null; }
# Acquire before stopping anything; backup and recovery use the same lock.
exec 9>/var/lib/jobradar/operations.lock
flock --exclusive --nonblock 9 || { log "another operation is active"; exit 1; }

case "$(systemctl show -p ActiveState --value jobradar-backup.service)" in
  active|activating) log "backup running; not compacting"; exit 1 ;;
esac
resume=0
systemctl is-active --quiet jobradar-workers.service && resume=1
listener_resume=0
[ -n "$(docker compose ps --status running -q listener)" ] && listener_resume=1
restart() {
  code=$?
  trap - EXIT
  failed=0
  if [[ "$code" != 0 ]] && docker inspect jobradar-compact >/dev/null 2>&1; then
    # A stopped host shell must not leave its rewrite container running.
    timeout 60 docker stop --time 30 jobradar-compact || failed=1
    psql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='jobradar-compaction' AND pid<>pg_backend_pid()" >/dev/null || failed=1
  fi
  after=$(date +%s)
  if [ "$listener_resume" = 1 ]; then
    docker compose up -d --no-deps listener </dev/null || failed=1
  fi
  if [ "$resume" = 1 ]; then
    systemctl start --no-block jobradar-workers.service || failed=1
  fi
  # Release the maintenance lock before verifying worker progress.
  flock --unlock 8 2>/dev/null || true
  python3 /opt/jobradar/recovery.py verify --worker "$resume" --listener "$listener_resume" --after "$after" || failed=1
  if [ "$failed" = 1 ]; then
    log "ERROR: maintenance recovery failed; local recovery will retry"
    exit 1
  fi
  log "recovery verified; listener=$listener_resume workers=$resume"
  exit "$code"
}
trap restart EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

log "stopping workers and listener"
systemctl stop jobradar-workers.service
docker compose stop listener </dev/null
for i in $(seq 1 90); do
  n=$(psql "select count(*) from pg_stat_activity where datname = 'jobradar' and pid <> pg_backend_pid()
            and state in ('active', 'idle in transaction', 'idle in transaction (aborted)')")
  [ "$n" = 0 ] && break
  [ "$i" = 90 ] && { log "database still busy after 15 min; giving up"; exit 1; }
  sleep 10
done
need_kb=$(( $(psql "select pg_total_relation_size('jobs')") / 1024 ))
free_kb=$(df --output=avail -k /var/lib/jobradar | tail -1)
log "database idle; jobs $(( need_kb / 1024 )) MB, free disk $(( free_kb / 1024 )) MB"
# The new copy is written before the old one is dropped.
[ "$free_kb" -gt $(( need_kb + 2 * 1024 * 1024 )) ] || { log "not enough free disk"; exit 1; }

hours=$(awk "BEGIN { printf \"%.2f\", $MAX_MINUTES / 60 }")
exec 8>/var/lib/jobradar/maintenance.lock
flock --exclusive --wait 600 8
docker compose run --rm --no-deps -T --name jobradar-compact \
  -e PGAPPNAME=jobradar-compaction \
  --entrypoint timeout poll --kill-after=30s "$(( MAX_MINUTES * 60 + 1800 ))s" \
  jobradar prune-content --rewrite --yes --max-hours "$hours" </dev/null
log "compaction finished; free disk $(df -h --output=avail /var/lib/jobradar | tail -1)"
