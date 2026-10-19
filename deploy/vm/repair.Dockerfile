FROM us-east1-docker.pkg.dev/jobradar-510801/jobradar/worker@sha256:50cc1e2e81318efb414400aab3366230e1cf3ca0721e3a321fe2f534ca807e60
COPY --chown=10001:10001 repository.py recency.py filters.py db.py health.py http.py telegram.py /app/.venv/lib/python3.12/site-packages/jobradar/
COPY --chown=10001:10001 env.py /app/.venv/lib/python3.12/site-packages/jobradar/migrations/env.py
COPY --chown=10001:10001 0009_scan_indexes.py /app/.venv/lib/python3.12/site-packages/jobradar/migrations/versions/
USER 10001
RUN python -c "from jobradar import repository, recency, filters, db, health, http, telegram; from alembic.config import Config; from alembic.script import ScriptDirectory; c=Config(); c.set_main_option('script_location','jobradar:migrations'); assert ScriptDirectory.from_config(c).get_current_head() == '0009'; print('Non-root imports and migration discovery passed')"
