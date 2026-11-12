#!/bin/bash
# Linux/Docker gate, from the packaged release directory. No production secrets.
set -euo pipefail
image=${1:?tested release image required}
sha256sum -c SHA256SUMS
rm -f linux-pg16-tests-passed.txt
image_id=$(docker image inspect -f '{{.Id}}' "$image")
id=jobradar-reliability-test-$$
cleanup() {
  docker rm -fv "$id-app" "$id-db" "$id-seed" >/dev/null 2>&1 || true
  docker network rm "$id" >/dev/null 2>&1 || true
  docker volume rm "$id-tools" >/dev/null 2>&1 || true
  docker image rm "$id-tools" "$id-base" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker tag "$image_id" "$id-base"
docker build -f reliability-test.Dockerfile --build-arg "TEST_IMAGE=$id-base" -t "$id-tools" .
docker volume create "$id-tools" >/dev/null
docker run --name "$id-seed" --network none -v "$id-tools:/test-tools" --entrypoint true "$id-tools"
docker network create --internal "$id" >/dev/null
docker run -d --name "$id-db" --network "$id" --network-alias db \
  --memory 256m --memory-swap 512m --shm-size 64m \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=jobradar -e POSTGRES_DB=jobradar_reliability_test \
  postgres:16-bookworm postgres -c shared_buffers=64MB -c work_mem=2MB -c max_connections=30 >/dev/null
for i in $(seq 1 60); do
  docker exec "$id-db" pg_isready -h 127.0.0.1 -U jobradar -d jobradar_reliability_test >/dev/null && break
  [[ "$i" == 60 ]] && { echo 'Test Postgres did not start'; exit 1; }
  sleep 1
done
[[ $(docker exec "$id-db" psql -U jobradar -d jobradar_reliability_test -Atc 'SHOW server_version_num') =~ ^16[0-9]{4}$ ]]
docker run --name "$id-app" --network "$id" --memory 512m --memory-swap 1024m \
  -v "$id-tools:/test-tools:ro" -w /test-tools/suite \
  -e JOBRADAR_TEST_DATABASE_URL=postgresql+psycopg://jobradar@db/jobradar_reliability_test \
  --entrypoint python "$image_id" -c 'import sys, jobradar; assert jobradar.__file__.startswith("/app/.venv/"), jobradar.__file__; sys.path.append("/test-tools"); import pytest; sys.exit(pytest.main(["-p", "no:cacheprovider", "-q"]))' | tee linux-pg16-tests.log
[[ $(docker inspect -f '{{.Image}}' "$id-app") == "$image_id" ]]
[[ $(docker inspect -f '{{.State.OOMKilled}}' "$id-app") == false ]]
[[ $(docker inspect -f '{{.State.OOMKilled}}' "$id-db") == false ]]
[[ $(docker inspect -f '{{.State.Running}}' "$id-db") == true ]]
{
  sha256sum SHA256SUMS
  echo "$image_id"
  date -u +%FT%TZ
  # Line 4: the pushed digest reference, when tested by one. Image IDs differ
  # between Docker's classic and containerd image stores; a digest does not.
  case "$image" in *@sha256:*) echo "$image";; esac
} > linux-pg16-tests-passed.txt
echo 'Linux/PostgreSQL 16 suite passed under worker/database memory limits.'
