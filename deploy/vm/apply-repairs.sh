#!/bin/bash
# Run on the existing VM as root after a fresh repaired backup succeeds.
set -euo pipefail
[[ $EUID == 0 ]]
cd /opt/jobradar
PREVIOUS_IMAGE=us-east1-docker.pkg.dev/jobradar-510801/jobradar/worker@sha256:50cc1e2e81318efb414400aab3366230e1cf3ca0721e3a321fe2f534ca807e60
REPAIR_IMAGE=us-east1-docker.pkg.dev/jobradar-510801/jobradar/worker@sha256:5e4668b4c9649a7d69b889f388bd643090a03786f13b0b6d8220d6301c292148
exec 9>/var/lib/jobradar/maintenance.lock
flock --exclusive 9
[[ $(systemctl show jobradar-backup.service -p Result --value) == success ]]
[[ $(systemctl show jobradar-backup.service -p ExecMainStatus --value) == 0 ]]
[[ $(stat -c %Y /var/lib/jobradar/backup.dump) -ge $(date -u -d '2026-10-19 13:30:00' +%s) ]]
[[ $(systemctl show jobradar-backup.service -p SubState --value) == dead ]]
rollback=/opt/jobradar/repairs-20261019
mkdir -p "$rollback"
chmod 700 "$rollback"
# The first attempt (failed image, rolled back) left the original setting in
# worker.env. Reuse it only when the live .env is still exactly that rolled-back
# original on the previous image; never overwrite it.
if [[ -f "$rollback/worker.env" ]]; then
  grep -qx "JOBRADAR_IMAGE=$PREVIOUS_IMAGE" "$rollback/worker.env" \
    || { echo 'worker.env does not pin the previous image; inspect before rerunning.' >&2; exit 1; }
  cmp -s .env "$rollback/worker.env" \
    || { echo '.env differs from the retained rollback copy; inspect before rerunning.' >&2; exit 1; }
  echo 'Reusing retained rollback copy from the earlier attempt.'
else
  grep -qx "JOBRADAR_IMAGE=$PREVIOUS_IMAGE" .env \
    || { echo '.env does not pin the expected previous image.' >&2; exit 1; }
  cp -p .env "$rollback/worker.env"
fi
cp -p .env "$rollback/worker.env.$(date -u +%Y%m%dT%H%M%SZ)"
pre_invalid=$(docker compose exec -T postgres psql -U jobradar -d jobradar -Atc "SELECT count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready")
[[ $pre_invalid == 0 ]] || { echo "Found $pre_invalid invalid indexes before migration; inspect first." >&2; exit 1; }
systemctl stop jobradar-poll.timer
repair_committed=0
finish() {
  docker rm -f jobradar-repair-migration >/dev/null 2>&1 || true
  if [[ $repair_committed == 0 ]]; then
    cp -p "$rollback/worker.env" .env
    docker compose up -d --no-deps listener || true
  fi
  systemctl start jobradar-poll.timer
}
trap finish EXIT
python3 - <<'PY'
import subprocess
from cloud_io import token
subprocess.run(['docker','login','-u','oauth2accesstoken','--password-stdin',
                'us-east1-docker.pkg.dev'],input=token().encode(),
               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
PY
docker pull "$REPAIR_IMAGE"
python3 - "$REPAIR_IMAGE" <<'PY'
import os,sys
from pathlib import Path
p=Path('.env')
lines=p.read_text().splitlines()
assert sum(line.startswith('JOBRADAR_IMAGE=') for line in lines)==1
with os.fdopen(os.open('.env.repair',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as f:
    f.write('\n'.join('JOBRADAR_IMAGE='+sys.argv[1] if line.startswith('JOBRADAR_IMAGE=') else line for line in lines)+'\n')
Path('.env.repair').replace(p)
PY
timeout 1800 docker compose run --rm --no-deps --name jobradar-repair-migration poll init-db
docker compose exec -T postgres psql -U jobradar -d jobradar -v ON_ERROR_STOP=1 -c 'ANALYZE;'
invalid=$(docker compose exec -T postgres psql -U jobradar -d jobradar -Atc "SELECT count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready")
[[ $invalid == 0 ]] || { echo "Found $invalid invalid indexes after migration." >&2; exit 1; }
version=$(docker compose exec -T postgres psql -U jobradar -d jobradar -Atc 'SELECT version_num FROM alembic_version')
[[ $version == 0009* ]] || { echo "Unexpected schema version: $version" >&2; exit 1; }
docker compose up -d --no-deps --wait listener
repair_committed=1
echo 'Repair image and concurrent indexes deployed; polling resumes after the maintenance lock is released.'
