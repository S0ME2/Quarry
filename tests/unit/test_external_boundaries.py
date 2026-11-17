"""Dotenv/environment alone must never authorize public network/provider tests."""

import os
import runpy
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.exc import OperationalError


@pytest.mark.parametrize("command_opt_in", [False, True])
@pytest.mark.parametrize("environment_opt_in", [False, True])
def test_live_tests_require_both_explicit_command_and_environment_opt_in(
    monkeypatch: pytest.MonkeyPatch, command_opt_in: bool, environment_opt_in: bool
) -> None:
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "conftest.py"))
    monkeypatch.setenv("QUARRY_LIVE_GOLDEN", "1" if environment_opt_in else "0")

    class Options:
        def getoption(self, name: str) -> bool:
            assert name == "--live-golden"
            return command_opt_in

    namespace["pytest_configure"](cast(pytest.Config, Options()))
    assert (os.environ.get("QUARRY_LIVE_GOLDEN") == "1") is (command_opt_in and environment_opt_in)


def test_database_setup_failure_does_not_expose_connection_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "conftest.py"))
    secret = "private-test-credential-must-not-appear"

    class UnavailableEngine:
        disposed = False

        def connect(self) -> None:
            raise OperationalError(None, None, Exception(f"password={secret}"))

        def dispose(self) -> None:
            self.disposed = True

    engine = UnavailableEngine()
    monkeypatch.setitem(
        namespace["db_engine"].__wrapped__.__globals__, "build_engine", lambda _: engine
    )
    fixture = namespace["db_engine"].__wrapped__(None)
    with pytest.raises(pytest.fail.Exception, match="Credentials omitted") as error:
        next(fixture)
    assert secret not in str(error.value)
    assert engine.disposed
