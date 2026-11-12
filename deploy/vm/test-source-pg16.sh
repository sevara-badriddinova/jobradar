#!/bin/bash
# Run from the extracted release in Cloud Shell. This tests packaged source,
# not the production image; it deliberately does not create the deploy pass file.
set -euo pipefail
[[ $(uname -s) == Linux && $(id -u) != 0 ]]
sha256sum -c --quiet SHA256SUMS
pgbin=${PG16_BINDIR:-/usr/lib/postgresql/16/bin}
[[ $("$pgbin/postgres" -V) == *' 16.'* ]]
timeout 360 uv sync --locked --python "${TEST_PYTHON:-/usr/bin/python3}"
runpg=$(mktemp -d /tmp/jobradar-pg16.XXXXXXXX)
unset PGHOSTADDR PGSERVICE PGSERVICEFILE PGPASSWORD
export PGHOST="$runpg" PGPORT=55439
stop_test_db() {
  if [[ -f "$runpg/data/postmaster.pid" ]]; then
    "$pgbin/pg_ctl" -D "$runpg/data" -m fast -w stop || return 1
  fi
  rm -r -- "$runpg"
}
trap stop_test_db EXIT
"$pgbin/initdb" --no-locale --encoding=UTF8 -A trust -U jobradar -D "$runpg/data" > "$runpg/init.log"
"$pgbin/pg_ctl" -D "$runpg/data" -o "-c listen_addresses='' -k $runpg -p $PGPORT -c shared_buffers=64MB -c work_mem=2MB -c max_connections=30" -l "$runpg/server.log" -w start
"$pgbin/createdb" -U jobradar jobradar_reliability_test
"$pgbin/psql" -U jobradar -d jobradar_reliability_test -Atc 'SHOW server_version'
export JOBRADAR_TEST_DATABASE_URL=postgresql+psycopg://jobradar@/jobradar_reliability_test
export PATH="$PWD/.venv/bin:$PATH"
timeout 900 .venv/bin/python -m pytest -q | tee native-pg16-suite.log
