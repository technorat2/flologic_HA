"""Regression tests for single-valve cloud-client reliability."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.flologic.api import (
    FloLogicAccount,
    FloLogicClient,
    FloLogicConnection,
)
from custom_components.flologic.exceptions import FloLogicError, FloLogicTimeoutError


def make_valve(**overrides: Any) -> dict[str, Any]:
    """Build a representative single-valve cloud payload."""
    valve: dict[str, Any] = {
        "id": 11,
        "uuid": "uuid-1",
        "isZConnect": True,
        "isZGateway": False,
        "mode": 1,
        "online": True,
        "flowState": 1,
        "deviceTypeName": "Connect",
    }
    valve.update(overrides)
    return valve


def make_account(valve: dict[str, Any], **overrides: Any) -> FloLogicAccount:
    """Build a single-valve account snapshot."""
    values: dict[str, Any] = {"user": {"id": 7}, "valve": valve}
    values.update(overrides)
    return FloLogicAccount(**values)


def make_client(*, keep_session_alive: bool = True) -> FloLogicClient:
    """Build a client without opening network resources."""
    return FloLogicClient(
        email="user@example.com",
        password="secret",
        hub_url="https://example.test",
        device_name="test",
        device_code="code",
        device_token="token",
        keep_session_alive=keep_session_alive,
    )


def make_connection() -> FloLogicConnection:
    """Build a connection whose network methods will be mocked."""
    return FloLogicConnection(
        session=AsyncMock(),
        hub_url="https://example.test/signalr",
        headers={},
    )


async def test_invoke_failure_removes_waiter() -> None:
    """A failed send must not leave a waiter to consume a later response."""
    connection = make_connection()
    connection.invoke = AsyncMock(side_effect=FloLogicError("send failed"))

    with pytest.raises(FloLogicError, match="send failed"):
        await connection.invoke_and_wait("Request", "Response")

    assert connection._events.get("Response") == []


async def test_timeout_removes_waiter() -> None:
    """A timed-out request must be removed from its event queue."""
    connection = make_connection()
    connection.invoke = AsyncMock()

    with pytest.raises(TimeoutError):
        await connection.invoke_and_wait("Request", "Response", timeout=0)

    assert connection._events.get("Response") == []


async def test_login_invoke_failure_cancels_both_waiters() -> None:
    """Login setup failures must clean up user and valve waiters."""
    client = make_client()
    waiters: list[asyncio.Future[list[Any]]] = []

    def wait_for(_event: str) -> asyncio.Future[list[Any]]:
        waiter = asyncio.get_running_loop().create_future()
        waiters.append(waiter)
        return waiter

    connection = SimpleNamespace(
        wait_for=wait_for,
        invoke=AsyncMock(side_effect=aiohttp.ClientConnectionError("offline")),
    )

    with pytest.raises(aiohttp.ClientConnectionError):
        await client._login(connection)

    assert len(waiters) == 2
    assert all(waiter.cancelled() for waiter in waiters)


async def test_push_during_poll_preserves_latest_valve_state() -> None:
    """A slower metadata poll must not roll back a newer valve push."""
    client = make_client()
    user = {"id": 7}
    stale = make_valve(mode=1)
    pushed = make_valve(mode=2, flowState=4)
    client._persistent_user = user
    client._persistent_valve = stale
    client._persistent_devices = [stale]
    client._last_account = make_account(stale)
    received: list[FloLogicAccount] = []
    client.set_push_callback(received.append)
    client._refresh_persistent_valve = AsyncMock(return_value=(user, stale, [stale]))

    async def fetch_access(*_args: Any) -> dict[str, Any]:
        client._handle_pushed_valves([pushed])
        return {"valveId": 11, "notificationsList": 64}

    client._fetch_access = AsyncMock(side_effect=fetch_access)
    client._fetch_scheduler = AsyncMock(return_value=[{"action": "mode"}])
    client._fetch_notifications = AsyncMock(return_value=[{"id": 1}])

    account = await client._async_fetch_account_persistent(SimpleNamespace())

    assert received[-1].valve["mode"] == 2
    assert account.valve["mode"] == 2
    assert account.access == {"valveId": 11, "notificationsList": 64}
    assert account.scheduler == [{"action": "mode"}]
    assert account.notifications == [{"id": 1}]
    assert account.update_source == "push"


async def test_mixed_inventory_uses_valid_valve() -> None:
    """A usable valve is accepted even when extra inventory entries are malformed."""
    client = make_client()
    valve = make_valve()
    client._persistent_user = {"id": 7}
    client._persistent_valve = valve
    client._persistent_devices = [valve]
    payload = [valve, "invalid"]
    connection = SimpleNamespace(
        invoke_and_wait=AsyncMock(return_value=[payload]),
    )

    user, selected, devices = await client._refresh_persistent_valve(connection)

    assert user == {"id": 7}
    assert selected == valve
    assert devices == payload
    assert client._persistent_valve == selected
    assert client._persistent_devices == payload


async def test_empty_inventory_fails_poll_without_clearing_cache() -> None:
    """An empty solicited inventory marks the poll failed but retains cache."""
    client = make_client()
    valve = make_valve()
    client._persistent_user = {"id": 7}
    client._persistent_valve = valve
    client._persistent_devices = [valve]
    connection = SimpleNamespace(invoke_and_wait=AsyncMock(return_value=[[]]))

    with pytest.raises(FloLogicTimeoutError, match="did not return a valve"):
        await client._refresh_persistent_valve(connection)

    assert client._persistent_valve == valve
    assert client._persistent_devices == [valve]


def test_empty_push_is_ignored() -> None:
    """One unsolicited empty array does not replace the cached valve."""
    client = make_client()
    valve = make_valve()
    client._persistent_user = {"id": 7}
    client._persistent_valve = valve
    client._persistent_devices = [valve]
    client._last_account = make_account(valve)
    received: list[FloLogicAccount] = []
    client.set_push_callback(received.append)

    client._handle_persistent_event("ValveArraySent", [[]])

    assert received == []
    assert client._persistent_valve == valve
    assert client._persistent_devices == [valve]


def test_mixed_push_filters_invalid_entries() -> None:
    """Unsolicited arrays retain usable valves and discard malformed entries."""
    client = make_client()
    valve = make_valve(mode=2)
    client._persistent_user = {"id": 7}
    client._persistent_valve = make_valve()
    client._persistent_devices = [client._persistent_valve]
    client._last_account = make_account(client._persistent_valve)
    received: list[FloLogicAccount] = []
    client.set_push_callback(received.append)

    client._handle_persistent_event("ValveArraySent", [[valve, "invalid"]])

    assert client._persistent_valve == valve
    assert client._persistent_devices == [valve]
    assert received[-1].valve == valve


@pytest.mark.parametrize(
    "error", [TimeoutError(), aiohttp.ClientConnectionError("offline")]
)
async def test_temporary_session_normalizes_transport_errors(error: Exception) -> None:
    """Temporary-session transport failures use the integration error contract."""
    client = make_client(keep_session_alive=False)
    operation = AsyncMock(side_effect=error)

    with pytest.raises(FloLogicError) as raised:
        await client._with_session(operation)

    assert raised.value.__cause__ is error
