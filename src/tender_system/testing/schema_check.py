"""Integrity schema drift check confined to an ephemeral TEST_DATABASE_URL schema."""

import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.schema import CreateSchema, DropSchema

from tender_system.core.environment import PROJECT_ROOT
from tender_system.persistence.storage import (
    build_engine,
    database_url_from_environment,
    validate_test_database,
)


def main() -> None:
    url = database_url_from_environment("TEST_DATABASE_URL")
    validate_test_database(url.get_secret_value(), os.environ.get("DATABASE_URL"))
    admin = build_engine(url)
    with admin.connect() as connection:
        actual = connection.scalar(text("select current_database()"))
        if actual != admin.url.database or not actual or "test" not in actual.lower():
            admin.dispose()
            raise ValueError("Connected database is not the selected test database")
    schema = f"integrity_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = build_engine(
        SecretStr(
            admin.url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
                hide_password=False
            )
        )
    )
    try:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            command.check(config)
            command.current(config)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


if __name__ == "__main__":
    main()
