from typing import Any, Dict, Tuple

import pytest

# TODO: update after resolution in https://github.com/pytest-dev/pytest/issues/7469
from _pytest.fixtures import SubRequest

from chia.util.streamable import Streamable
from tests.util.network_protocol_data import module_to_name_to_instance


@pytest.fixture(
    name="name_and_instance",
    params=[
        (name, instance)
        for module, name_to_instance in module_to_name_to_instance.items()
        for name, instance in name_to_instance.items()
    ],
    ids=lambda param: param[0],  # type: ignore[no-any-return]
)
def name_and_instance_fixture(request: SubRequest) -> Tuple[str, Streamable]:
    return request.param  # type: ignore[no-any-return]


@pytest.fixture(name="name")
def name_fixture(name_and_instance: Tuple[str, Streamable]) -> str:
    name, instance = name_and_instance
    return name


@pytest.fixture(name="instance")
def instance_fixture(name_and_instance: Tuple[str, Streamable]) -> Streamable:
    name, instance = name_and_instance
    return instance


def test_protocol_json_to_dict_str_matches(
    protocol_messages: Dict[str, Dict[str, Any]],
    name: str,
    instance: Streamable,
) -> None:
    assert str(protocol_messages[name]) == str(instance.to_json_dict())


def test_protocol_json_from_json_instance_matches(
    protocol_messages: Dict[str, Dict[str, Any]],
    name: str,
    instance: Streamable,
) -> None:
    assert type(instance).from_json_dict(protocol_messages[name]) == instance


def test_protocol_from_bytes_matches_instance(
    name: str, instance: Streamable, protocol_messages_bytes: Dict[str, bytes]
) -> None:
    message_bytes = protocol_messages_bytes[name]
    message = type(instance).from_bytes(message_bytes)
    assert message == instance


def test_protocol_to_bytes_matches(name: str, instance: Streamable, protocol_messages_bytes: Dict[str, bytes]) -> None:
    message_bytes = protocol_messages_bytes[name]
    assert message_bytes == bytes(instance)