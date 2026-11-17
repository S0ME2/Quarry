"""Manual command parsing, safety overrides and credential-safe output; no network."""

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tender_system.core.configuration import load_configuration
from tender_system.operator import manual_cli as cli


def test_operation_list_is_concise_unless_json_requested(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    operation = {
        "id": "fixture",
        "result": {"validation_passed": True, "proposal": {"large": "recipe"}},
    }
    monkeypatch.setattr(cli, "_DETAILS", False)
    cli.emit([operation])
    assert json.loads(capsys.readouterr().out) == [
        {"id": "fixture", "result": {"validation_passed": True}}
    ]
    monkeypatch.setattr(cli, "_DETAILS", True)
    cli.emit([operation])
    assert json.loads(capsys.readouterr().out) == [operation]


def test_doctor_database_unavailable_is_actionable_and_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy.exc import OperationalError

    def unavailable(*_: object) -> Any:
        raise OperationalError(None, None, Exception("private-database-password"))

    monkeypatch.setattr(cli, "build_engine", unavailable)
    args = cli.parser().parse_args(["doctor"])
    assert cli.doctor(args, load_configuration(environ={}, validate_models=False)) == 1
    output = capsys.readouterr().out
    assert "database_unavailable" in output and "private-database-password" not in output


def test_every_command_parses() -> None:
    key = str(uuid4())
    commands = [
        ["config"],
        ["doctor", "--expected-database", "quarry_clean_dev", "--browser"],
        ["validate", "recipe.json"],
        ["recipe-import", "source/recipe.json"],
        ["recipe-import", "recipe.json", "--replace", key],
        ["recipe-reject", key],
        ["recipe-validate", key],
        ["recipe-validate", key, "--max-tenders", "100", "--max-requests", "120"],
        ["recipe-smoke", key],
        ["recipe-smoke", key, "--max-tenders", "10"],
        ["evidence", "reference"],
        ["collect", "source"],
        ["collect", "source", "--max-tenders", "100"],
        ["source-enable", "source"],
        ["sources"],
        ["runs", "--source", "source"],
        ["tenders", "--limit", "2"],
        ["campaign-create", "campaign.json"],
        ["campaigns"],
        ["results", key],
        ["jobs", key],
        *[[name, key] for name in ("operation", "status", "recipe", "approve", "execute")],
        *[[name, "--pair", f"{key}:{key}"] for name in ("rules", "qwen")],
    ]
    for command in commands:
        assert cli.parser().parse_args(command).command == command[0]


@pytest.mark.parametrize("size,maximum", [(2, 1), (6, 5)])
def test_scope_rejected_before_environment_or_database(
    size: int, maximum: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("invalid scope reached environment loader")

    monkeypatch.setattr(cli, "manual_environment", forbidden)
    args = ["qwen", "--max-evaluations", str(maximum)]
    for _ in range(size):
        args += ["--pair", f"{uuid4()}:{uuid4()}"]
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


def test_file_precedence_and_database_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("QUARRY_TEST_VALUE=root\nDATABASE_URL=postgresql://u/dev\n")
    file = tmp_path / "db.env"
    file.write_text("QUARRY_TEST_VALUE=selected\nDATABASE_URL=postgresql://u/selected_dev\n")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("QUARRY_TEST_VALUE", "shell")
    cli.manual_environment(file)
    assert os.environ["QUARRY_TEST_VALUE"] == "shell"
    assert os.environ["DATABASE_URL"] == "postgresql://u/selected_dev"
    monkeypatch.setenv("DATABASE_URL", "postgresql://u/wrong_dev")
    with pytest.raises(ValueError, match="conflicts"):
        cli.manual_environment(file)
    with pytest.raises(ValueError, match="missing"):
        cli.manual_environment(tmp_path / "absent")


def test_defaults_and_scoped_policy_preserved_manual_ai_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "offline-fixture-key")
    config = load_configuration(
        environ={
            "QUARRY_MATCHER__MAX_ATTEMPTS": "5",
            "QUARRY_MATCHER__PROVIDER": "groq",
            "QUARRY_MATCHER__MODEL": "qwen/qwen3.8-27b",
            "QUARRY_MATCHER__API_KEY_ENV": "GROQ_API_KEY",
            "QUARRY_MATCHER__MAX_OUTPUT_TOKENS": "900",
            "QUARRY_CAMPAIGN__MATCH_THRESHOLD": "0.8",
            "QUARRY_SOURCE__TIMEZONE": "Asia/Tashkent",
        }
    )
    original = config.model_dump()
    campaign = uuid4()
    effective = cli.selected_config(cli.manual_config(config), ((uuid4(), campaign),), True)
    settings = effective.for_campaign(str(campaign))
    assert settings.model.max_attempts == settings.retry.max_attempts == 1
    assert settings.matcher_version == "3"
    assert settings.routing.max_model_calls == 1
    assert settings.model.max_output_tokens == 600
    assert not settings.routing.escalation_enabled
    assert settings.match_threshold == 0.8
    assert effective.for_source("source").timezone == "Asia/Tashkent"
    assert not effective.runtime.operator_worker_enabled
    assert settings.manual_evaluation_id is None
    assert config.model_dump() == original


def test_manual_qwen_can_schedule_a_fresh_evaluation_without_campaign_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "offline-fixture-key")
    config = load_configuration(
        environ={
            "QUARRY_MATCHER__PROVIDER": "groq",
            "QUARRY_MATCHER__MODEL": "qwen/qwen3.8-27b",
            "QUARRY_MATCHER__API_KEY_ENV": "GROQ_API_KEY",
        }
    )
    original = config.model_dump()
    campaign = uuid4()
    pairs = ((uuid4(), campaign),)
    first = cli.selected_config(cli.manual_config(config), pairs, True, new_evaluation=True)
    second = cli.selected_config(cli.manual_config(config), pairs, True, new_evaluation=True)
    first_settings = first.for_campaign(str(campaign))
    second_settings = second.for_campaign(str(campaign))
    assert first_settings.manual_evaluation_id is not None
    assert first_settings.manual_evaluation_id != second_settings.manual_evaluation_id
    assert first_settings.model.model == "qwen/qwen3.8-27b"
    assert first_settings.matcher_version == "3"
    assert config.model_dump() == original


def test_token_usage_visible_secrets_hidden(capsys: pytest.CaptureFixture[str]) -> None:
    cli.emit(
        {
            "input_tokens": 42,
            "output_tokens": 7,
            "max_output_tokens": 500,
            "input_byte_limit_is_not_a_token_cap": True,
            "api_key": "private",
            "model_calls": [{"input_tokens": None, "output_tokens": 9}],
        }
    )
    output = json.loads(capsys.readouterr().out)
    assert output["input_tokens"] == 42 and output["output_tokens"] == 7
    assert output["max_output_tokens"] == 500
    assert output["input_byte_limit_is_not_a_token_cap"] is True
    assert output["model_calls"][0]["output_tokens"] == 9
    assert output["api_key"] == "[redacted]"


def test_recipe_export_preserves_query_and_refuses_credentials(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = {"url": "https://example.test/api?page=2"}
    cli.export_recipe(recipe)
    assert json.loads(capsys.readouterr().out) == recipe
    with pytest.raises(ValueError):
        cli.export_recipe({"url": "https://example.test/api?token=private"})
    with pytest.raises(ValueError):
        cli.export_recipe({"headers": {"Authorization": "private"}})
    monkeypatch.setenv("GROQ_API_KEY", "private-test-secret")
    with pytest.raises(ValueError):
        cli.export_recipe({"value": "private-test-secret"})


def test_duplicates_and_invalid_batch_maximum_fail() -> None:
    selection = f"{uuid4()}:{uuid4()}"
    with pytest.raises(SystemExit):
        cli.main(["qwen", "--pair", selection, "--pair", selection, "--max-evaluations", "2"])
    with pytest.raises(SystemExit):
        cli.main(["qwen", "--pair", selection, "--max-evaluations", "6"])
    with pytest.raises(SystemExit):
        cli.main(
            [
                "qwen",
                "--pair",
                selection,
                "--pair",
                f"{uuid4()}:{uuid4()}",
                "--max-evaluations",
                "2",
            ]
        )


@pytest.mark.parametrize("command", ["investigate", "repair", "repair-approve", "repairs"])
def test_retired_commands_are_unavailable(command: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args([command])
    assert error.value.code == 2
