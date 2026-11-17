"""Explicit local administrator bootstrap. Password stays off command line and logs."""

import argparse
import getpass
from uuid import uuid4

from sqlalchemy import select

from tender_system.api.auth import Credentials, password_hash
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import load_configuration
from tender_system.persistence.storage import (
    UserRow,
    UserSessionRow,
    build_engine,
    database_url_from_environment,
    session_factory,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or promote a Quarry administrator")
    parser.add_argument("email")
    args = parser.parse_args()
    body = Credentials(email=args.email, password=getpass.getpass("Administrator password: "))
    if body.password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match")
    config = load_configuration(validate_models=False)
    engine = build_engine(database_url_from_environment(config.global_settings.database_url_env))
    try:
        with session_factory(engine).begin() as session:
            from sqlalchemy import delete

            row = session.scalar(select(UserRow).where(UserRow.email == body.email))
            if row is None:
                row = UserRow(
                    id=uuid4(), email=body.email, created_at=SystemClock().now(), failed_logins=0
                )
                session.add(row)
            else:
                session.execute(delete(UserSessionRow).where(UserSessionRow.user_id == row.id))
            row.password_hash, row.role, row.active = password_hash(body.password), "admin", True
            row.failed_logins, row.locked_until = 0, None
        print("Administrator account ready")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
