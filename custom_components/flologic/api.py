"""Minimal FloLogic SignalR cloud client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import (
    MODE_FLAG_NAMES,
    MODE_NAMES,
    MODE_STATUS_PRIORITY,
    NOTIFICATION_FLAGS,
    VALVE_MODES,
    WATER_OFF_MODE_FLAGS,
)
from .exceptions import FloLogicAuthError, FloLogicError, FloLogicTimeoutError

_LOGGER = logging.getLogger(__name__)

_RECORD_SEPARATOR = "\x1e"


@dataclass(slots=True)
class FloLogicAccount:
    """FloLogic account/device snapshot."""

    user: dict[str, Any]
    valve: dict[str, Any]
    devices: list[dict[str, Any]] = field(default_factory=list)
    access: dict[str, Any] | None = None
    scheduler: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    update_source: str = "poll"

    @property
    def valve_name(self) -> str:
        """Return a friendly valve name."""
        return (
            self.valve.get("valveFriendlyName")
            or self.valve.get("combinedName")
            or self.valve.get("name")
            or self.valve.get("uuid")
            or "FloLogic"
        )

    @property
    def unique_id_prefix(self) -> str:
        """Return a stable unique ID prefix."""
        return str(self.valve.get("uuid") or self.valve.get("id"))

    @property
    def mode_name(self) -> str | None:
        """Return the current controllable mode name."""
        mode = self.valve.get("mode")
        mode_value = self._int_or_none(mode)
        if mode_value is None:
            return None
        exact = MODE_NAMES.get(mode_value)
        if exact is not None:
            return exact
        if self._has_any_mode_flag(mode_value, WATER_OFF_MODE_FLAGS):
            return "shutoff"
        if mode_value & VALVE_MODES["bypass"]:
            return "bypass"
        if mode_value & VALVE_MODES["away"]:
            return "away"
        if mode_value & VALVE_MODES["home"]:
            return "home"
        if mode_value & VALVE_MODES["disabled"]:
            return "disabled"
        return None

    @property
    def mode_status_name(self) -> str:
        """Return the most specific current mode/status name."""
        mode_value = self._int_or_none(self.valve.get("mode"))
        if mode_value is None:
            return "unknown"
        exact = MODE_NAMES.get(mode_value)
        if exact is not None:
            return exact
        for flag in MODE_STATUS_PRIORITY:
            if mode_value & flag:
                return MODE_FLAG_NAMES[flag]
        return f"unknown_{mode_value}"

    @property
    def mode_flag_names(self) -> list[str]:
        """Return every known mode flag currently set."""
        mode_value = self._int_or_none(self.valve.get("mode"))
        if mode_value is None:
            return []
        return [name for flag, name in MODE_FLAG_NAMES.items() if mode_value & flag]

    @property
    def notification_flags(self) -> dict[str, bool]:
        """Return decoded notification settings."""
        raw = 0
        if self.access:
            raw = int(self.access.get("notificationsList") or 0)
        return {name: bool(raw & bit) for name, bit in NOTIFICATION_FLAGS.items()}

    @property
    def shutoff_countdown_seconds(self) -> int | None:
        """Return estimated seconds until automatic shutoff from continuous flow."""
        if not self.is_water_flowing:
            return None

        limit_minutes = self._current_flow_limit_minutes
        if limit_minutes is None or limit_minutes <= 0:
            return None

        last_new_flow = self._parse_datetime(self.valve.get("lastNewFlow"))
        if last_new_flow is None:
            return None

        shutoff_at = last_new_flow.timestamp() + (limit_minutes * 60)
        return max(0, int(shutoff_at - datetime.now(UTC).timestamp()))

    @property
    def advance_shutoff_warning(self) -> bool:
        """Return whether the valve is in the advance-shutoff warning window."""
        if not self.notification_flags.get("advance_shutoff", False):
            return False
        countdown = self.shutoff_countdown_seconds
        if countdown is None:
            return False
        pre_alert_minutes = self.valve.get("preAlertNoticeInterval") or 0
        return 0 <= countdown <= int(pre_alert_minutes * 60)

    @property
    def flow_started_at(self) -> datetime | None:
        """Return when the current flow event started."""
        if not self.is_water_flowing:
            return None
        return self._parse_datetime(self.valve.get("lastNewFlow"))

    @property
    def flow_elapsed_seconds(self) -> int | None:
        """Return locally calculated seconds since flow started."""
        started_at = self.flow_started_at
        if started_at is None:
            return None
        return max(0, int(datetime.now(UTC).timestamp() - started_at.timestamp()))

    @property
    def is_water_flowing(self) -> bool:
        """Return whether the valve reports active flow."""
        flow_state = self.valve.get("flowState")
        return bool(self.valve.get("online")) and flow_state not in (None, 1, 8)

    @property
    def _current_flow_limit_minutes(self) -> float | None:
        """Return the active mode's flow limit in minutes."""
        mode = self.mode_name
        if mode == "home":
            return self._float_or_none(self.valve.get("homeIntervalTime"))
        if mode == "away":
            return self._float_or_none(self.valve.get("awayIntervalTime"))
        if mode == "bypass":
            return self._float_or_none(self.valve.get("bypassTime"))
        return None

    @property
    def active_scheduler_events(self) -> list[dict[str, Any]]:
        """Return scheduler entries that have an action."""
        return [
            event
            for event in self.scheduler
            if event.get("action") is not None
            and event.get("actionPayload") is not None
        ]

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        """Return a float or None."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        """Return an int or None."""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _has_any_mode_flag(mode_value: int, flags: tuple[int, ...]) -> bool:
        """Return whether any mode flag is set."""
        return any(mode_value & flag for flag in flags)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        """Parse a FloLogic timestamp as UTC."""
        if not isinstance(value, str) or not value:
            return None
        text = value
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


class FloLogicConnection:
    """Short-lived SignalR connection."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        hub_url: str,
        headers: dict[str, str],
        event_callback: Callable[[str, list[Any]], None] | None = None,
        closed_callback: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the connection."""
        self._session = session
        self._hub_url = hub_url.rstrip("/")
        self._headers = headers
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task | None = None
        self._events: dict[str, list[asyncio.Future[list[Any]]]] = {}
        self._closed = asyncio.Event()
        self._event_callback = event_callback
        self._closed_callback = closed_callback

    async def __aenter__(self) -> FloLogicConnection:
        """Open the SignalR connection."""
        negotiate = await self._session.post(
            f"{self._hub_url}/negotiate",
            headers=self._headers,
        )
        if negotiate.status in (401, 403):
            raise FloLogicAuthError("FloLogic rejected the request")
        if negotiate.status >= 400:
            raise FloLogicError(f"SignalR negotiate failed: {negotiate.status}")
        payload = await negotiate.json()
        token = payload.get("connectionToken") or payload.get("connectionId")
        if not token:
            raise FloLogicError("SignalR negotiate did not return a connection token")

        websocket_hub_url = self._hub_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        ws_url = f"{websocket_hub_url}?id={quote(token, safe='')}"
        self._ws = await self._session.ws_connect(ws_url, headers=self._headers)
        await self._ws.send_str(
            json.dumps({"protocol": "json", "version": 1}) + _RECORD_SEPARATOR
        )
        self._reader_task = asyncio.create_task(self._reader())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Close the SignalR connection."""
        await self.close()

    async def close(self) -> None:
        """Close the websocket."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
        for waiters in self._events.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
        self._events.clear()

    @property
    def closed(self) -> bool:
        """Return whether the websocket is closed."""
        return self._ws is None or self._ws.closed or self._closed.is_set()

    async def invoke(self, target: str, *arguments: Any) -> None:
        """Invoke a hub method."""
        if self._ws is None:
            raise FloLogicError("SignalR connection is not open")
        if self.closed:
            raise FloLogicError("SignalR connection is closed")
        frame = {
            "type": 1,
            "target": target,
            "arguments": list(arguments),
        }
        await self._ws.send_str(
            json.dumps(frame, separators=(",", ":")) + _RECORD_SEPARATOR
        )

    async def invoke_and_wait(
        self,
        target: str,
        event_name: str,
        *arguments: Any,
        timeout: float = 30,
    ) -> list[Any]:
        """Invoke a hub method and wait for an event."""
        waiter = self.wait_for(event_name)
        try:
            await self.invoke(target, *arguments)
            return await asyncio.wait_for(waiter, timeout)
        finally:
            if not waiter.done():
                waiter.cancel()
            self._remove_waiter(event_name, waiter)

    def wait_for(self, event_name: str) -> asyncio.Future[list[Any]]:
        """Wait for a hub event."""
        future: asyncio.Future[list[Any]] = asyncio.get_running_loop().create_future()
        self._events.setdefault(event_name, []).append(future)
        future.add_done_callback(
            lambda done_future: self._remove_waiter(event_name, done_future)
        )
        return future

    def _remove_waiter(
        self, event_name: str, future: asyncio.Future[list[Any]]
    ) -> None:
        """Remove a completed event waiter."""
        waiters = self._events.get(event_name, [])
        if future in waiters:
            waiters.remove(future)

    async def _reader(self) -> None:
        """Read SignalR frames."""
        assert self._ws is not None
        async for message in self._ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                for raw_frame in message.data.split(_RECORD_SEPARATOR):
                    if not raw_frame:
                        continue
                    try:
                        frame = json.loads(raw_frame)
                    except json.JSONDecodeError:
                        _LOGGER.debug("Ignoring non-JSON FloLogic frame: %s", raw_frame)
                        continue
                    self._handle_frame(frame)
            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        self._closed.set()
        if self._closed_callback is not None:
            self._closed_callback()

    def _handle_frame(self, frame: dict[str, Any]) -> None:
        """Dispatch a SignalR frame."""
        if frame.get("type") != 1:
            return
        target = frame.get("target")
        if not target:
            return
        if target == "ErrorOccured":
            _LOGGER.warning("FloLogic error event: %s", frame.get("arguments"))
        arguments = frame.get("arguments") or []
        waiters = self._events.get(target, [])
        if waiters:
            waiter = waiters.pop(0)
            if not waiter.done():
                waiter.set_result(arguments)
        if self._event_callback is not None:
            self._event_callback(target, arguments)


class FloLogicClient:
    """FloLogic cloud API client."""

    def __init__(
        self,
        *,
        email: str,
        password: str,
        hub_url: str,
        device_name: str,
        device_code: str,
        device_token: str,
        session_factory: Callable[[], aiohttp.ClientSession] | None = None,
        keep_session_alive: bool = False,
    ) -> None:
        """Initialize the client."""
        self._email = email
        self._password = password
        self._hub_url = hub_url.rstrip("/")
        self._device_name = device_name
        self._device_code = device_code
        self._device_token = device_token
        self._session_factory = session_factory
        self._keep_session_alive = keep_session_alive
        self._relog_token = ""
        self._persistent_lock = asyncio.Lock()
        self._persistent_session: aiohttp.ClientSession | None = None
        self._persistent_session_owned = False
        self._persistent_connection: FloLogicConnection | None = None
        self._persistent_user: dict[str, Any] | None = None
        self._persistent_valve: dict[str, Any] | None = None
        self._persistent_devices: list[dict[str, Any]] = []
        self._push_callback: Callable[[FloLogicAccount], None] | None = None
        self._last_account: FloLogicAccount | None = None
        self._push_revision = 0
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False

    def set_push_callback(
        self, callback: Callable[[FloLogicAccount], None] | None
    ) -> None:
        """Set a callback for pushed persistent SignalR valve updates."""
        self._push_callback = callback

    async def async_fetch_account(self) -> FloLogicAccount:
        """Fetch the current account/device snapshot."""
        if self._keep_session_alive:
            account = await self._with_persistent_retry(
                self._async_fetch_account_persistent
            )
        else:
            account = await self._with_session(self._async_fetch_account)
        self._last_account = account
        return account

    async def async_set_mode(self, mode: str) -> None:
        """Set the valve mode."""
        mode_value = VALVE_MODES[mode]
        await self.async_request_state_change({"mode": mode_value})

    async def async_request_state_change(self, fields: dict[str, Any]) -> None:
        """Send a FloLogic state-change command."""
        if self._keep_session_alive:
            await self._with_persistent_retry(
                lambda connection: self._async_send_state_change(connection, fields)
            )
            return

        async def _send(session: aiohttp.ClientSession) -> None:
            async with self._connection(session) as connection:
                user, valve, _devices = await self._login(connection)
                await self._send_state_change(connection, user, valve, fields)

        await self._with_session(_send)

    async def async_close(self) -> None:
        """Close any persistent connection."""
        self._closing = True
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
        await self._close_persistent()

    async def _async_fetch_account(
        self, session: aiohttp.ClientSession
    ) -> FloLogicAccount:
        """Fetch a snapshot using an existing session."""
        async with self._connection(session) as connection:
            user, valve, devices = await self._login(connection)
            access = await self._fetch_access(connection, user, valve)
            scheduler = await self._fetch_scheduler(connection, user, valve)
            notifications = await self._fetch_notifications(connection, user, valve)
            return FloLogicAccount(
                user=user,
                valve=valve,
                devices=devices,
                access=access,
                scheduler=scheduler,
                notifications=notifications,
                update_source="poll",
            )

    async def _async_fetch_account_persistent(
        self,
        connection: FloLogicConnection,
    ) -> FloLogicAccount:
        """Fetch a snapshot using the persistent SignalR connection."""
        user, valve, devices = await self._refresh_persistent_valve(connection)
        push_revision = self._push_revision
        access = await self._fetch_access(connection, user, valve)
        scheduler = await self._fetch_scheduler(connection, user, valve)
        notifications = await self._fetch_notifications(connection, user, valve)
        if self._push_revision != push_revision and self._persistent_valve is not None:
            # Metadata requests yield to the websocket reader. If a newer valve
            # push arrived while they were in flight, publish that valve state
            # with the freshly fetched metadata instead of rolling it back.
            valve = self._persistent_valve
            devices = list(self._persistent_devices)
            update_source = "push"
        else:
            update_source = "poll"
        return FloLogicAccount(
            user=user,
            valve=valve,
            devices=devices,
            access=access,
            scheduler=scheduler,
            notifications=notifications,
            update_source=update_source,
        )

    async def _async_send_state_change(
        self,
        connection: FloLogicConnection,
        fields: dict[str, Any],
    ) -> None:
        """Send a state-change command on the persistent connection."""
        user, valve, _devices = await self._refresh_persistent_valve(connection)
        await self._send_state_change(connection, user, valve, fields)

    async def _send_state_change(
        self,
        connection: FloLogicConnection,
        user: dict[str, Any],
        valve: dict[str, Any],
        fields: dict[str, Any],
    ) -> None:
        """Send a FloLogic state-change command on an open connection."""
        command = {
            "active": True,
            "created": datetime.now(UTC).isoformat(),
            "userId": user["id"],
            "valveId": valve["id"],
            **fields,
        }
        await connection.invoke_and_wait(
            "RequestStateChange",
            "StateChangeResult",
            user,
            valve,
            command,
            timeout=45,
        )

    async def _login(
        self,
        connection: FloLogicConnection,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Log in and return user, selected valve, and device list."""
        login_waiter = connection.wait_for("LoggedIn")
        valve_waiter = connection.wait_for("ValveSent")
        try:
            await connection.invoke(
                "Login", self._email, self._password, self._device_name, None
            )
            try:
                user_args = await asyncio.wait_for(login_waiter, 30)
            except TimeoutError as err:
                raise FloLogicAuthError("FloLogic login did not return a user") from err
            if not user_args or not isinstance(user_args[0], dict):
                raise FloLogicError("FloLogic returned an invalid login payload")
            user = user_args[0]
            self._relog_token = user.get("relogToken") or self._relog_token

            devices: list[dict[str, Any]] = []
            valve: dict[str, Any] | None = None
            try:
                valve_args = await asyncio.wait_for(valve_waiter, 3)
                if not valve_args or not isinstance(valve_args[0], dict):
                    raise FloLogicError("FloLogic returned an invalid valve payload")
                valve = valve_args[0]
                devices = [valve]
            except TimeoutError:
                array_args = await connection.invoke_and_wait(
                    "RefreshValveArray",
                    "ValveArraySent",
                    user,
                    timeout=30,
                )
                devices = _validate_valve_inventory(
                    array_args[0] if array_args else None
                )
                valve = choose_valve(devices)

            if not valve:
                raise FloLogicTimeoutError("FloLogic login did not return a valve")
            return user, valve, devices
        finally:
            for waiter in (login_waiter, valve_waiter):
                if not waiter.done():
                    waiter.cancel()

    async def _fetch_access(
        self,
        connection: FloLogicConnection,
        user: dict[str, Any],
        valve: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Fetch the current user's access record."""
        try:
            args = await connection.invoke_and_wait(
                "RequestUserAccesses",
                "UserAccessesSent",
                user,
                timeout=30,
            )
        except TimeoutError:
            return None
        accesses = args[0] if args else []
        return next(
            (access for access in accesses if access.get("valveId") == valve.get("id")),
            None,
        )

    async def _fetch_scheduler(
        self,
        connection: FloLogicConnection,
        user: dict[str, Any],
        valve: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Fetch scheduler entries."""
        try:
            args = await connection.invoke_and_wait(
                "RequestSchedulerEvents",
                "SchedulerEventsSent",
                user["id"],
                valve["id"],
                timeout=30,
            )
        except TimeoutError:
            return []
        return args[0] if args else []

    async def _fetch_notifications(
        self,
        connection: FloLogicConnection,
        user: dict[str, Any],
        valve: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Fetch notification history, if the cloud has any rows."""
        try:
            args = await connection.invoke_and_wait(
                "RefreshValvesNotificationsHistory",
                "NotificationsHistorySent",
                user["id"],
                [valve["id"]],
                timeout=30,
            )
        except TimeoutError:
            return []
        notifications = args[0] if args else []
        if notifications:
            return notifications
        try:
            all_args = await connection.invoke_and_wait(
                "RefreshValvesNotificationsHistory",
                "NotificationsHistorySent",
                user["id"],
                [],
                timeout=30,
            )
        except TimeoutError:
            return []
        return all_args[0] if all_args else []

    async def _with_session(
        self, func: Callable[[aiohttp.ClientSession], Awaitable[Any]]
    ) -> Any:
        """Run a function with a client session."""
        try:
            if self._session_factory is not None:
                session = self._session_factory()
                return await func(session)
            async with aiohttp.ClientSession() as session:
                return await func(session)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise FloLogicError(f"Unable to communicate with FloLogic: {err}") from err

    async def _with_persistent_retry(
        self,
        func: Callable[[FloLogicConnection], Awaitable[Any]],
    ) -> Any:
        """Run a function on the persistent connection, reconnecting once if needed."""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                connection = await self._ensure_persistent_connection()
                return await func(connection)
            except (TimeoutError, FloLogicError, aiohttp.ClientError) as err:
                last_error = err
                _LOGGER.debug(
                    "FloLogic persistent connection failed on attempt %s",
                    attempt + 1,
                    exc_info=err,
                )
                await self._close_persistent()
        if last_error is not None:
            raise FloLogicError(str(last_error)) from last_error
        raise FloLogicError("FloLogic persistent connection failed")

    async def _ensure_persistent_connection(self) -> FloLogicConnection:
        """Open or return the persistent SignalR connection."""
        async with self._persistent_lock:
            if (
                self._persistent_connection is not None
                and not self._persistent_connection.closed
            ):
                return self._persistent_connection

            await self._close_persistent()
            if self._session_factory is not None:
                session = self._session_factory()
                self._persistent_session_owned = False
            else:
                session = aiohttp.ClientSession()
                self._persistent_session_owned = True
            connection = self._connection(session)
            await connection.__aenter__()
            user, valve, devices = await self._login(connection)
            self._persistent_session = session
            self._persistent_connection = connection
            self._persistent_user = user
            self._persistent_valve = valve
            self._persistent_devices = devices
            return connection

    async def _close_persistent(self) -> None:
        """Close the persistent connection and owned session."""
        connection = self._persistent_connection
        session = self._persistent_session
        owned = self._persistent_session_owned
        self._persistent_connection = None
        self._persistent_session = None
        self._persistent_session_owned = False
        self._persistent_user = None
        self._persistent_valve = None
        self._persistent_devices = []
        if connection is not None:
            await connection.close()
        if owned and session is not None and not session.closed:
            await session.close()

    def _handle_persistent_closed(self) -> None:
        """Schedule persistent reconnection after an unexpected close."""
        if self._closing or not self._keep_session_alive:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_with_backoff())

    async def _reconnect_with_backoff(self) -> None:
        """Reconnect the persistent websocket with conservative backoff."""
        for delay in (5, 15, 30, 60):
            if self._closing or not self._keep_session_alive:
                return
            await asyncio.sleep(delay)
            try:
                await self._close_persistent()
                await self._ensure_persistent_connection()
            except (TimeoutError, FloLogicError, aiohttp.ClientError):
                _LOGGER.debug(
                    "FloLogic persistent reconnect failed after %s seconds",
                    delay,
                    exc_info=True,
                )
                continue
            return

    async def _refresh_persistent_valve(
        self,
        connection: FloLogicConnection,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Refresh the persistent connection's selected valve."""
        if self._persistent_user is None:
            raise FloLogicError("FloLogic persistent connection is not logged in")
        args = await connection.invoke_and_wait(
            "RefreshValveArray",
            "ValveArraySent",
            self._persistent_user,
            timeout=30,
        )
        devices = _validate_valve_inventory(args[0] if args else None)
        valve = choose_valve(devices)
        if not valve:
            raise FloLogicTimeoutError("FloLogic refresh did not return a valve")
        self._persistent_valve = valve
        self._persistent_devices = devices
        return self._persistent_user, valve, devices

    def _handle_persistent_event(self, target: str, arguments: list[Any]) -> None:
        """Handle unsolicited hub events on the persistent connection."""
        if target == "ValveSent" and arguments:
            valve = arguments[0]
            if isinstance(valve, dict):
                self._handle_pushed_valves([valve])
        elif target == "ValveArraySent" and arguments:
            valves = arguments[0]
            if not isinstance(valves, list) or any(
                not isinstance(valve, dict) for valve in valves
            ):
                _LOGGER.warning(
                    "Ignoring malformed FloLogic ValveArraySent payload; retaining "
                    "the last known valve"
                )
                return
            if not valves:
                _LOGGER.warning(
                    "Ignoring empty FloLogic ValveArraySent payload; retaining the "
                    "last known valve until a poll confirms its state"
                )
                return
            self._handle_pushed_valves(valves)

    def _handle_pushed_valves(self, valves: list[dict[str, Any]]) -> None:
        """Update the cached account from pushed valve data."""
        if not self._keep_session_alive or self._persistent_user is None:
            return
        if not valves:
            _LOGGER.warning(
                "Ignoring empty FloLogic valve push; retaining the last known valve"
            )
            return
        valve = choose_valve(valves)
        if valve is None:
            return
        if (
            len(valves) == 1
            and valve.get("isZGateway") is True
            and self._persistent_valve is not None
            and self._persistent_valve.get("isZGateway") is not True
            and valve.get("id") != self._persistent_valve.get("id")
        ):
            return
        self._persistent_valve = valve
        self._persistent_devices = valves
        self._push_revision += 1
        if self._last_account is not None:
            account = FloLogicAccount(
                user=self._last_account.user,
                valve=valve,
                devices=valves,
                access=self._last_account.access,
                scheduler=self._last_account.scheduler,
                notifications=self._last_account.notifications,
                update_source="push",
            )
        else:
            account = FloLogicAccount(
                user=self._persistent_user,
                valve=valve,
                devices=valves,
                update_source="push",
            )
        self._last_account = account
        if self._push_callback is not None:
            self._push_callback(account)

    def _connection(self, session: aiohttp.ClientSession) -> FloLogicConnection:
        """Create a connection object."""
        hub_url = self._hub_url
        if not hub_url.lower().endswith("/signalr"):
            hub_url = f"{hub_url}/signalr"
        return FloLogicConnection(
            session=session,
            hub_url=hub_url,
            headers={
                "userDeviceCode": self._device_code,
                "userDeviceToken": self._device_token,
                "relogToken": self._relog_token,
                "OsPlatform": "Android",
                "AppVer": "homeassistant",
                "DeviceName": self._device_name,
            },
            event_callback=self._handle_persistent_event
            if self._keep_session_alive
            else None,
            closed_callback=self._handle_persistent_closed
            if self._keep_session_alive
            else None,
        )


def _validate_valve_inventory(payload: Any) -> list[dict[str, Any]]:
    """Validate a solicited valve-array response without changing cached state."""
    if not isinstance(payload, list) or any(
        not isinstance(device, dict) for device in payload
    ):
        raise FloLogicError("FloLogic returned an invalid valve inventory")
    return payload


def choose_valve(devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the controllable Connect valve from a device list."""
    if not devices:
        return None
    return (
        next((device for device in devices if device.get("isZConnect") is True), None)
        or next(
            (
                device
                for device in devices
                if device.get("isAnyConnect") is True
                and device.get("isZGateway") is not True
            ),
            None,
        )
        or next(
            (
                device
                for device in devices
                if "connect" in str(device.get("deviceTypeName") or "").lower()
            ),
            None,
        )
        or next(
            (device for device in devices if device.get("isZGateway") is not True), None
        )
        or devices[0]
    )
