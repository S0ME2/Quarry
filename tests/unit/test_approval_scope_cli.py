"""Collection scope has no arbitrary record ceiling and rejects invalid input before work."""

from uuid import uuid4

import pytest

from tender_system.operator.manual_cli import parser


@pytest.mark.parametrize("command", ["collect", "recipe-validate"])
def test_record_limit_above_ten_thousand_is_explicitly_supported(command: str) -> None:
    target = "source" if command == "collect" else str(uuid4())
    args = parser().parse_args([command, target, "--max-tenders", "15001"])
    assert args.max_tenders == 15001


@pytest.mark.parametrize("command", ["collect", "recipe-validate"])
@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_invalid_record_limit_exits_before_database_or_network(command: str, value: str) -> None:
    target = "source" if command == "collect" else str(uuid4())
    with pytest.raises(SystemExit) as error:
        parser().parse_args([command, target, "--max-tenders", value])
    assert error.value.code == 2
