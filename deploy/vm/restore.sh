#!/bin/bash
# Restore only into a new, empty database. Run as root after prepare.sh.
set -euo pipefail
[[ $EUID == 0 ]]
bundle=$(realpath "${1:?Pass the extracted migration bundle directory}")
cd "$bundle"
sha256sum --check SHA256SUMS
cd /opt/jobradar
tables=$(docker compose exec -T postgres psql -XAt -U jobradar -d jobradar -c "SELECT count(*) FROM pg_tables WHERE schemaname='public'" </dev/null)
[[ $tables == 0 ]] || { echo 'Refusing to restore into a nonempty database.' >&2; exit 1; }
docker compose exec -T postgres pg_restore -U jobradar -d jobradar \
  --no-owner --no-acl --single-transaction --exit-on-error < "$bundle/database.dump"
docker compose exec -T postgres psql -Xq -U jobradar -d jobradar \
  < "$bundle/manifest.sql" > "$bundle/cloud-manifest.txt"
diff -u "$bundle/source-manifest.txt" "$bundle/cloud-manifest.txt"
docker compose exec -T postgres psql -Xq -U jobradar -d jobradar -c ANALYZE </dev/null
echo 'RESTORE_VERIFIED: all table counts and migration version match.'
