#!/bin/bash
# Cloud Shell side of the reliability release (docs/28, docs/29). From a full
# (not shallow) clone of the release branch, in this order:
#   bash deploy/vm/cloudshell-release.sh check    # VM == live-baseline; image == production source
#   bash deploy/vm/cloudshell-release.sh build    # package, build overlay, push, test the pushed digest
#   bash deploy/vm/cloudshell-release.sh apply    # copy to the VM, run the guarded apply (rolls back on failure)
#   bash deploy/vm/cloudshell-release.sh verify   # services, status files, scoring, budget
# State lives in ~/jobradar-release. Nothing here prints a secret.
set -euo pipefail
PROJECT=jobradar-510801 ZONE=us-east1-b VM=jobradar-vm
REGISTRY=us-east1-docker.pkg.dev
REPO=$REGISTRY/$PROJECT/jobradar/worker
ANTHROPIC_VERSION=${ANTHROPIC_VERSION:-3}
SRC=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
STATE=$HOME/jobradar-release
BASELINE=(compose.yaml compose.workers.yaml cloud_io.py compact-jobs.sh)
mkdir -p "$STATE"

on_vm() {  # stdin: a script run as root on the VM; arguments become $1, $2, ...
  gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap --quiet \
    --command "sudo bash -s -- $*"
}

check() {
  local live base ref cid
  live=$(on_vm <<'VM'
cd /opt/jobradar
sha256sum compose.yaml compose.workers.yaml cloud_io.py compact-jobs.sh
echo "image $(sed -n 's/^JOBRADAR_IMAGE=//p' .env)"
echo "units workers=$(systemctl is-active jobradar-workers) backup=$(systemctl is-active jobradar-backup.service) poll=$(systemctl is-active jobradar-poll.timer)"
VM
)
  echo "$live"
  if diff <(grep -E '^[0-9a-f]{64} ' <<<"$live") \
          <(cd "$SRC/deploy/vm/live-baseline" && sha256sum "${BASELINE[@]}"); then
    echo "OK: VM host files match deploy/vm/live-baseline"
  else
    echo "STOP: VM host files differ from deploy/vm/live-baseline"; exit 1
  fi
  base=$(sed -n 's/^image //p' <<<"$live")
  [[ "$base" == *@sha256:* ]] || { echo "STOP: live image is not a digest: $base"; exit 1; }
  echo "$base" > "$STATE/base-image"
  ref=$(sed -n 's/^PRODUCTION_SOURCE = "\([0-9a-f]*\)".*/\1/p' "$SRC/deploy/vm/package-reliability.py")
  gcloud auth configure-docker "$REGISTRY" --quiet >/dev/null 2>&1
  docker pull -q "$base" >/dev/null
  rm -rf "$STATE/base-src" "$STATE/ref-src"
  mkdir -p "$STATE/base-src" "$STATE/ref-src"
  cid=$(docker create "$base")
  docker cp -q "$cid:/app/.venv/lib/python3.12/site-packages/jobradar" "$STATE/base-src/"
  docker rm "$cid" >/dev/null
  git -C "$SRC" archive "$ref" src/jobradar | tar -x -C "$STATE/ref-src"
  manifest() { (cd "$1" && find jobradar \( -name '*.py' -o -name '*.mako' \) | LC_ALL=C sort | xargs sha256sum); }
  if diff <(manifest "$STATE/base-src") <(manifest "$STATE/ref-src/src") > "$STATE/source-diff.txt"; then
    echo "OK: production image source matches $ref"
  else
    echo "STOP: production image differs from $ref:"; head -20 "$STATE/source-diff.txt"; exit 1
  fi
}

build() {
  local base stamp rel digest ref
  base=$(cat "$STATE/base-image")
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  rel=$STATE/release-$stamp
  python3 "$SRC/deploy/vm/package-reliability.py" "$rel"
  cat "$rel/OVERLAY"
  cd "$rel"
  docker build -q -f reliability.Dockerfile --build-arg "BASE_IMAGE=$base" -t "$REPO:reliability-$stamp" . >/dev/null
  docker push -q "$REPO:reliability-$stamp" >/dev/null
  digest=$(gcloud artifacts docker images describe "$REPO:reliability-$stamp" --format='value(image_summary.digest)')
  ref=$REPO@$digest
  docker pull -q "$ref" >/dev/null
  # Docker Hub can stall from Cloud Shell; Google's mirror serves the same image.
  docker image inspect postgres:16-bookworm >/dev/null 2>&1 || {
    docker pull -q mirror.gcr.io/library/postgres:16-bookworm >/dev/null
    docker tag mirror.gcr.io/library/postgres:16-bookworm postgres:16-bookworm
  }
  echo "Testing $ref (full suite, PostgreSQL 16, 512 MiB worker / 256 MiB database)..."
  if ! bash test-reliability.sh "$ref" > test-output.txt 2>&1; then
    tail -40 test-output.txt; echo "STOP: candidate failed its tests; nothing was deployed"; exit 1
  fi
  grep -E '[0-9]+ passed' test-output.txt | tail -1
  echo "$ref" > "$STATE/candidate"
  echo "$rel" > "$STATE/release-dir"
  echo "OK: candidate ready: $ref"
}

apply() {
  local ref base rel name
  ref=$(cat "$STATE/candidate"); base=$(cat "$STATE/base-image"); rel=$(cat "$STATE/release-dir")
  name=$(basename "$rel")
  tar -C "$(dirname "$rel")" -czf "$STATE/$name.tgz" "$name"
  gcloud compute scp "$STATE/$name.tgz" "$VM:/tmp/$name.tgz" --zone "$ZONE" --project "$PROJECT" \
    --tunnel-through-iap --quiet
  on_vm "$ref" "$base" "$ANTHROPIC_VERSION" "$name" <<'VM'
set -euo pipefail
ref=$1 base=$2 version=$3 name=$4
install -d -m 700 /opt/jobradar/releases
tar -xzf "/tmp/$name.tgz" -C /opt/jobradar/releases
rm -f "/tmp/$name.tgz"
curl -fsS -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])' \
  | docker login -u oauth2accesstoken --password-stdin us-east1-docker.pkg.dev >/dev/null
docker pull -q "$ref" >/dev/null
cd "/opt/jobradar/releases/$name"
bash apply-reliability.sh "$ref" "$base" "$version"
VM
}

verify() {
  on_vm <<'VM'
cd /opt/jobradar
echo "image $(sed -n 's/^JOBRADAR_IMAGE=//p' .env)"
echo "anthropic secret version $(cat anthropic-secret-version 2>/dev/null || echo unset)"
echo "budget lines in runtime.env: $(grep -c '^JOBRADAR_AI_DAILY_BUDGET_USD=' runtime.env)"
for u in jobradar-workers jobradar-recovery.timer jobradar-backup.timer jobradar-poll.timer; do
  echo "$u $(systemctl is-active "$u")"
done
docker compose ps --format '{{.Service}} {{.State}} {{.Status}}'
python3 - <<'PY'
import json, time
for name in ("worker", "listener"):
    try:
        s = json.load(open(f"/run/jobradar/status/{name}.json"))
        print(f"{name} status: {s.get('state', 'ok')}, {time.time() - s.get('checked_at', 0):.0f}s old, error={s.get('error')}")
    except Exception as exc:
        print(f"{name} status: missing ({type(exc).__name__})")
PY
journalctl -u jobradar-workers --since '-20min' --no-pager -o cat \
  | grep -E 'api.anthropic.com|rejected the credentials| ERROR ' | tail -4
journalctl -u jobradar-workers --since '-20min' --no-pager -o cat \
  | grep 'jobradar.worker: metrics' | tail -1 | grep -o '"ai": {[^}]*}' || echo "no metrics line yet (the first takes ~6 min)"
VM
}

case "${1:-}" in
  check|build|apply|verify) "$1" ;;
  *) echo "usage: $0 check|build|apply|verify"; exit 2 ;;
esac
