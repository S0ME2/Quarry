import pytest
from pydantic import JsonValue

from tender_system.core.ports import ModelClient, Transform
from tender_system.core.registry import Registry
from tender_system.testing.fakes import FunctionModelClient


def test_typed_plugins_selected_by_configuration_name() -> None:
    registry = Registry[ModelClient]()
    first = FunctionModelClient(lambda request: (_ for _ in ()).throw(AssertionError("unused")))
    second = FunctionModelClient(lambda request: (_ for _ in ()).throw(AssertionError("unused")))
    registry.register("local", first)
    registry.register("remote", second)
    assert registry.get("local") is first
    assert registry.get("remote") is second
    assert registry.names() == ("local", "remote")


def test_duplicate_unknown_and_blank_plugins_fail_explicitly() -> None:
    registry = Registry[int]()
    registry.register("one", 1)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("one", 2)
    assert registry.get("one") == 1
    with pytest.raises(KeyError, match="unknown plugin"):
        registry.get("missing")
    for name in ("", " ", " untrimmed "):
        with pytest.raises(ValueError):
            registry.register(name, 2)


def test_registry_instances_do_not_share_state() -> None:
    first = Registry[int]()
    first.register("one", 1)
    assert Registry[int]().names() == ()


def test_transform_registration_obeys_common_contract() -> None:
    from collections.abc import Mapping

    def prefix(value: JsonValue, parameters: Mapping[str, JsonValue]) -> JsonValue:
        return f"{parameters['prefix']}{value}"

    transforms = Registry[Transform]()
    transforms.register("prefix", prefix)
    assert transforms.get("prefix")("title", {"prefix": "Tender: "}) == "Tender: title"
