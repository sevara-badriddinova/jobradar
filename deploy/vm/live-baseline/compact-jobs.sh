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

case "$(systemctl show -p ActiveState --value jobradar-backup.service)" in
  active|activating) log "backup running; not compacting"; exit 1 ;;
esac
resume=0
systemctl is-active --quiet jobradar-workers.service && resume=1
restart() {
  docker compose up -d --no-deps listener </dev/null >/dev/null 2>&1 || true
  [ "$resume" = 1 ] && systemctl start --no-block jobradar-workers.service || true
  log "listener restarted; workers restarted: $resume"
}
trap restart EXIT

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
flock --exclusive --wait 600 /var/lib/jobradar/maintenance.lock \
  timeout $(( MAX_MINUTES * 60 + 1800 )) \
  docker compose run --rm --no-deps -T --name jobradar-compact poll \
    prune-content --rewrite --yes --max-hours "$hours" </dev/null
log "compaction finished; free disk $(df -h --output=avail /var/lib/jobradar | tail -1)"
