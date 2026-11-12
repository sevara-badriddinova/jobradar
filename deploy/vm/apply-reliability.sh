#!/bin/bash
# Run on the VM from a verified release directory, after Linux/PG16 validation:
#   sudo bash apply-reliability.sh NEW_IMMUTABLE_IMAGE EXPECTED_CURRENT_IMAGE ANTHROPIC_VERSION
# No schema changes, VM resizing, paid resources or AI budget changes.
set -euo pipefail
[[ $EUID == 0 ]]
release=$(pwd)
new_image=${1:?new immutable image required}
expected=${2:?expected current image required}
version=${3:?explicit replacement Anthropic secret version required}
[[ "$version" =~ ^[1-9][0-9]*$ && "$version" != 1 ]] || { echo 'Expected a replacement version number'; exit 1; }
case "$new_image" in *@sha256:*|sha256:*) ;; *) echo 'Immutable image required'; exit 1;; esac
sha256sum -c SHA256SUMS
# Evidence is prepared only after running the bundled source against PG16/Linux.
test -s linux-pg16-tests-passed.txt
head -n 1 linux-pg16-tests-passed.txt | sha256sum -c -
[[ $(sed -n '2p' linux-pg16-tests-passed.txt) == $(docker image inspect -f '{{.Id}}' "$new_image") ]] || {
  echo 'Test evidence belongs to a different image'; exit 1;
}
for f in compose.yaml compose.workers.yaml cloud_io.py compact-jobs.sh; do
  cmp -s "$release/baseline/$f" "/opt/jobradar/$f" || {
    echo "Live $f differs from the reviewed baseline. Reconcile and retest first."; exit 1;
  }
done
cd /opt/jobradar
current=$(python3 -c "from pathlib import Path; print(next(x.split('=',1)[1] for x in Path('.env').read_text().splitlines() if x.startswith('JOBRADAR_IMAGE=')))")
[[ "$current" == "$expected" ]] || { echo 'Live image changed; inspect before deploying'; exit 1; }
docker image inspect "$new_image" >/dev/null
[[ $(docker image inspect -f '{{index .Config.Labels "org.jobradar.base-image"}}' "$new_image") == "$expected" ]] || {
  echo 'Candidate was not built on the expected production base'; exit 1;
}
exec 9>/var/lib/jobradar/operations.lock
flock --exclusive --nonblock 9 || { echo 'Maintenance operation already active'; exit 1; }
case "$(systemctl show -p ActiveState --value jobradar-backup.service)" in
  active|activating|deactivating) echo 'Backup active; retry after completion'; exit 1;;
esac
for unit in jobradar-compact jobradar-prune; do
  case "$(systemctl show -p ActiveState --value "$unit.service")" in
    active|activating|deactivating) echo "$unit active; retry after completion"; exit 1;;
  esac
done
systemctl is-enabled --quiet jobradar-workers
if systemctl is-active --quiet jobradar-poll.timer; then echo 'Legacy polling active; inspect first'; exit 1; fi

saved=/opt/jobradar/reliability-rollback-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -m 700 "$saved"
python3 "$release/cloud_io.py" prepare-rotated-env "$version" "$saved/staged-runtime.env"
install -m 600 runtime.env "$saved/previous-runtime.env"
if [[ -f anthropic-secret-version ]]; then cp -p anthropic-secret-version "$saved/"; fi
cp -p .env compose.yaml compose.workers.yaml cloud_io.py compact-jobs.sh workers.sh "$saved/"
cp -p /etc/systemd/system/jobradar-workers.service "$saved/"
cp -p /etc/systemd/system/jobradar-backup.service.d/10-workers.conf "$saved/"
committed=0
finish() {
  code=$?
  trap - EXIT
  if [[ $committed == 0 ]]; then
    systemctl stop jobradar-recovery.timer 2>/dev/null || true
    systemctl stop jobradar-workers || true
    cp -p "$saved/"{.env,compose.yaml,compose.workers.yaml,cloud_io.py,compact-jobs.sh,workers.sh} /opt/jobradar/
    mv -f "$saved/previous-runtime.env" /opt/jobradar/runtime.env
    if [[ -f "$saved/anthropic-secret-version" ]]; then
      cp -p "$saved/anthropic-secret-version" /opt/jobradar/
    else
      rm -f /opt/jobradar/anthropic-secret-version
    fi
    rm -f "$saved/staged-runtime.env"
    cp -p "$saved/jobradar-workers.service" /etc/systemd/system/
    cp -p "$saved/10-workers.conf" /etc/systemd/system/jobradar-backup.service.d/
    systemctl daemon-reload
    systemctl reset-failed jobradar-workers
    systemctl start --no-block jobradar-workers
    docker compose up -d --no-deps listener
    echo "Deployment failed; previous image/configuration restored from $saved. Verify previous services in the journal."
  fi
  exit "$code"
}
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

systemctl stop jobradar-workers
docker compose stop listener
# Old server-side queries can outlive the old Docker client. Drain before starting new code.
for i in $(seq 1 60); do
  busy=$(docker compose exec -T postgres psql -U jobradar -d jobradar -Atc \
    "SELECT count(*) FROM pg_stat_activity WHERE datname='jobradar' AND pid<>pg_backend_pid() AND state IN ('active','idle in transaction','idle in transaction (aborted)')")
  [[ "$busy" == 0 ]] && break
  [[ "$i" == 60 ]] && { echo 'Database did not drain; rolling back'; exit 1; }
  sleep 5
done
cp -p "$release/"{compose.yaml,cloud_io.py,compact-jobs.sh} /opt/jobradar/
cd "$release"
bash install-workers.sh
cd /opt/jobradar
printf '%s\n' "$version" > "$saved/new-version"
mv -f "$saved/new-version" anthropic-secret-version
mv -f "$saved/staged-runtime.env" runtime.env
python3 - "$new_image" <<'PY'
import os, sys
from pathlib import Path
p=Path('.env')
lines=p.read_text().splitlines()
assert sum(s.startswith('JOBRADAR_IMAGE=') for s in lines)==1
with os.fdopen(os.open('.env.reliability',os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600),'w') as f:
    f.write('\n'.join('JOBRADAR_IMAGE='+sys.argv[1] if s.startswith('JOBRADAR_IMAGE=') else s for s in lines)+'\n')
Path('.env.reliability').replace(p)
PY
after=$(date +%s)
systemctl reset-failed jobradar-workers
systemctl start --no-block jobradar-workers
docker compose up -d --no-deps listener
python3 recovery.py verify --after "$after"
systemctl enable --now jobradar-recovery.timer
committed=1
rm -f "$saved/previous-runtime.env"
echo "Reliability release active and recovery verified. Rollback files: $saved"
echo 'Run and observe one online backup next; cleanup remains gated until it succeeds.'
