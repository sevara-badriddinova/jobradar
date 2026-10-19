# Stage the tested src/jobradar/applications.py as applications.py beside this
# Dockerfile. All other code remains at the previously deployed image digest.
FROM us-east1-docker.pkg.dev/jobradar-510801/jobradar/worker@sha256:2768d4b720f08ed7d9378c4d4344f4493476996eb77ac363867dfb3e2e373111
USER root
RUN test -f /app/.venv/lib/python3.12/site-packages/jobradar/applications.py
COPY --chown=10001:10001 applications.py /app/.venv/lib/python3.12/site-packages/jobradar/applications.py
USER 10001
