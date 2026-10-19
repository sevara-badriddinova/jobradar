"""Alembic environment. Used by `jobradar init-db` and by the `alembic` CLI."""

from alembic import context

from jobradar.db import make_engine, metadata

config = context.config


def run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True,
                      transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


if (connection := config.attributes.get("connection")) is not None:
    run(connection)  # called from jobradar.db.migrate with an open connection
else:
    with make_engine(config.get_main_option("sqlalchemy.url") or None).connect() as connection:
        run(connection)
