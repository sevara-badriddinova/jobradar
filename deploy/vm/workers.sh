#!/bin/bash
# Switch between the legacy five-minute poll and the continuous workers.
#   trial    stop the poll timer (stays enabled for reboot) and start workers (not enabled)
#   enable   cut over permanently: poll timer disabled, workers enabled
#   rollback stop/disable workers and restore the poll timer
#   status   show both pipelines and the latest worker metrics
set -euo pipefail
[[ $EUID == 0 ]]
cd /opt/jobradar
wait_poll() {
  systemctl stop jobradar-poll.timer
  for _ in $(seq 1 360); do
    # A running oneshot cycle reports "activating", which is-active treats as not active.
    case $(systemctl show -p ActiveState --value jobradar-poll.service) in
      active|activating|deactivating|reloading) ;;
      *) return 0 ;;
    esac
    sleep 10   # never interrupt a running legacy cycle
  done
  echo 'legacy cycle still running after an hour; aborting' >&2; exit 1
}
case "${1:-status}" in
  trial)    wait_poll; systemctl start jobradar-workers.service ;;
  enable)   wait_poll; systemctl disable jobradar-poll.timer; systemctl enable --now jobradar-workers.service ;;
  rollback) systemctl disable --now jobradar-workers.service || true
            systemctl enable --now jobradar-poll.timer ;;
  status)   systemctl --no-pager --lines=0 status jobradar-workers.service jobradar-poll.timer || true
            docker compose exec -T postgres psql -U jobradar -d jobradar -Atc \
              "SELECT value FROM app_state WHERE key = 'worker_metrics'" </dev/null || true ;;
  *) echo "usage: $0 trial|enable|rollback|status" >&2; exit 2 ;;
esac
