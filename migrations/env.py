"""No credentials in ini files. Tests may inject their own isolated connection."""

from alembic import context
from sqlalchemy import Connection

from tender_system.persistence.storage import Base, build_engine, database_url_from_environment


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run() -> None:
    injected = context.config.attributes.get("connection")
    if injected is not None:
        migrate(injected)
        return
    url = database_url_from_environment()
    if context.is_offline_mode():
        context.configure(
            url=url.get_secret_value(),
            target_metadata=Base.metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = build_engine(url)
        try:
            with engine.connect() as connection:
                migrate(connection)
        finally:
            engine.dispose()


run()
