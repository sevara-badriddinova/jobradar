#!/bin/bash
# Run as root from this directory after bootstrap. Does not start workers.
set -euo pipefail
[[ $EUID == 0 ]]
test -f /var/lib/jobradar/bootstrap-complete
bash install-services.sh
printf '%s\n' 'JOBRADAR_IMAGE=us-east1-docker.pkg.dev/jobradar-510801/jobradar/worker@sha256:50cc1e2e81318efb414400aab3366230e1cf3ca0721e3a321fe2f534ca807e60' > /opt/jobradar/.env
chmod 600 /opt/jobradar/.env
python3 /opt/jobradar/cloud_io.py configure
cd /opt/jobradar
docker compose --profile manual pull
docker compose up -d --wait postgres
docker compose run --rm --no-deps poll --help >/dev/null
echo 'Database and worker image ready; application workers remain stopped.'
