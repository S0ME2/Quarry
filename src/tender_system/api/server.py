"""Local workspace composition; no identity provisioning or seeding."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from tender_system.api.api import create_app
from tender_system.core.configuration import load_configuration, validate_model_configuration
from tender_system.core.environment import PROJECT_ROOT, startup_settings
from tender_system.operator.operations import effective_configuration
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    build_engine,
    database_url_from_environment,
    session_factory,
)


def app_factory() -> FastAPI:
    settings = startup_settings()
    config = load_configuration(validate_models=False)
    engine = build_engine(database_url_from_environment(config.global_settings.database_url_env))
    sessions = session_factory(engine)
    with sessions() as session:
        validate_model_configuration(effective_configuration(session, config))
    runtime = OperatorRuntime(sessions, config)
    if settings.register_catalog and settings.source_catalog.is_dir():
        runtime.register_catalog(settings.source_catalog)
    app = create_app(
        sessions,
        config,
        runtime=runtime,
    )
    frontend = PROJECT_ROOT / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="workspace")
    return app


def main() -> None:
    import logging

    import uvicorn

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("tender_system").setLevel(logging.INFO)
    try:
        settings = startup_settings()
        app = app_factory()
    except SQLAlchemyError:
        raise SystemExit(
            "Quarry could not open its database. Start PostgreSQL and check DATABASE_URL. "
            "For a new empty database, run uv run --locked alembic upgrade head. "
            "For an older database, follow docs/operations/DATABASE_REBUILD.md; "
            "keep the original and backup."
        ) from None
    except ValueError:
        raise SystemExit(
            "Quarry configuration is invalid. Check .env and optional config.toml against "
            ".env.example and docs/operations/CONFIGURATION.md. Credentials omitted."
        ) from None
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
    )


if __name__ == "__main__":
    main()
