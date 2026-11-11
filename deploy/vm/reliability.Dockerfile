# Build using the currently deployed immutable base. No dependency, profile,
# registry, model or budget changes. Build locally/Cloud Shell, not Cloud Build.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
LABEL org.jobradar.base-image="${BASE_IMAGE}"
COPY --chown=10001:10001 jobradar/ /app/.venv/lib/python3.12/site-packages/jobradar/
USER 10001
RUN python -c "from jobradar import worker, runtime, prune, health, cli; from alembic.config import Config; from alembic.script import ScriptDirectory; c=Config(); c.set_main_option('script_location','jobradar:migrations'); assert ScriptDirectory.from_config(c).get_current_head() == '0016'; print('Reliability imports and unchanged schema verified')"
