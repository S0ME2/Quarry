"""The workload must leave its dedicated test database unchanged on failure too."""

from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import Connection, text

from tender_system.persistence.storage import build_engine
from tools import database_workload

pytestmark = pytest.mark.database


@pytest.mark.parametrize(("fail", "trigram"), [(False, False), (False, True), (True, False)])
def test_workload_schema_cleanup(
    postgres_url: SecretStr, monkeypatch: pytest.MonkeyPatch, fail: bool, trigram: bool
) -> None:
    fixture_id = UUID("c90912cd-415d-48aa-9e88-789650a21f78")
    monkeypatch.setattr(database_workload, "uuid4", lambda: fixture_id)
    engine = build_engine(postgres_url)
    schema = f"workload_{fixture_id.hex}"
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = :schema"),
                    {"schema": schema},
                )
                == 0
            )
        if fail:

            def broken_fixture(connection: Connection, rows: int) -> None:
                connection.execute(text("CREATE TABLE partial_fixture (id integer)"))
                raise RuntimeError("fixture interrupted")

            monkeypatch.setattr(database_workload, "fixture", broken_fixture)
            with pytest.raises(RuntimeError, match="fixture interrupted"):
                database_workload.run(rows=1000, repeats=1)
        else:
            report = database_workload.run(rows=1000, repeats=1, trigram=trigram)
            if trigram:
                assert isinstance(report["trigram_index_bytes"], int)
                assert report["trigram_index_bytes"] > 0
            assert report["fixture"]["tenders"] == 1000
            for phase in ("baseline", "indexed"):
                assert set(report[phase]) == set(database_workload.QUERIES)
                assert all(query["median_execution_ms"] >= 0 for query in report[phase].values())
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = :schema"),
                    {"schema": schema},
                )
                == 0
            )
    finally:
        engine.dispose()
