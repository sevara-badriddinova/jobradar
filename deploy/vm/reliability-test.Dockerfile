ARG TEST_IMAGE
FROM ${TEST_IMAGE}
USER root
# This image only seeds the test-tools volume. It never runs the test suite.
RUN uv pip install --python /app/.venv/bin/python --target /test-tools pytest==9.1.1
COPY --chown=10001:10001 tests/ /test-tools/suite/tests/
COPY --chown=10001:10001 src/ /test-tools/suite/src/
COPY --chown=10001:10001 deploy/ /test-tools/suite/deploy/
COPY --chown=10001:10001 pyproject.toml uv.lock README.md companies.toml profile.toml /test-tools/suite/
