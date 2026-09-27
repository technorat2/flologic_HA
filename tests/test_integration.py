"""Exercise single-valve services with Home Assistant's real registries."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.flologic.api import FloLogicClient
from custom_components.flologic.const import DOMAIN
from custom_components.flologic.exceptions import FloLogicError

from .test_api_reliability import make_account, make_valve


async def call_action(hass, service: str = "set_home_limit", **data):
    """Call a FloLogic action through Home Assistant's service registry."""
    payload = {"minutes": 10} if service == "set_home_limit" else {}
    payload.update(data)
    await hass.services.async_call(DOMAIN, service, payload, blocking=True)


@pytest.fixture
async def loaded_entry(hass):
    """Load the existing one-entry, one-valve integration model."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="11",
        data={"email": "user@example.com", "password": "secret"},
    )
    entry.add_to_hass(hass)
    account = make_account(make_valve())
    client = MagicMock(spec=FloLogicClient)
    client.async_fetch_account.return_value = account
    with patch("custom_components.flologic.FloLogicClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    yield entry, client
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_services_registered_without_loaded_entry(hass) -> None:
    """Actions remain discoverable even while no config entry is loaded."""
    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "set_home_limit")
    with pytest.raises(ServiceValidationError, match="No FloLogic config entry"):
        await call_action(hass)


async def test_successful_action_refreshes_once(hass, loaded_entry) -> None:
    """A successful write is followed by one coordinator refresh."""
    _entry, client = loaded_entry
    fetches_before = client.async_fetch_account.await_count

    await call_action(hass)

    client.async_request_state_change.assert_awaited_once_with({"homeIntervalTime": 10})
    assert client.async_fetch_account.await_count == fetches_before + 1


async def test_failed_action_is_reported_and_not_refreshed(hass, loaded_entry) -> None:
    """A failed cloud write must not look successful to an automation."""
    _entry, client = loaded_entry
    client.async_request_state_change.side_effect = FloLogicError("offline")
    fetches_before = client.async_fetch_account.await_count

    with pytest.raises(HomeAssistantError, match="offline"):
        await call_action(hass)

    client.async_request_state_change.assert_awaited_once()
    assert client.async_fetch_account.await_count == fetches_before


@pytest.mark.parametrize(
    ("service", "payload"),
    [
        ("set_flow_sensitivity", {"value": -1}),
        ("set_flow_sensitivity", {"value": 1001}),
        ("set_home_limit", {"minutes": -1}),
        ("set_home_limit", {"minutes": 10081}),
        ("set_away_limit", {"minutes": -1}),
        ("set_away_limit", {"minutes": 10081}),
        ("set_auto_away", {"hours": 8761}),
        ("set_temp_alert", {"temperature": -51}),
        ("set_temp_shutoff", {"temperature": 151}),
        ("set_no_flow_notice", {"seconds": 604801}),
    ],
)
async def test_invalid_service_values_never_reach_cloud(
    hass, loaded_entry, service: str, payload: dict
) -> None:
    """Schema validation rejects unsafe values before any cloud command."""
    _entry, client = loaded_entry

    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(DOMAIN, service, payload, blocking=True)

    client.async_request_state_change.assert_not_awaited()


async def test_release_preserves_entry_and_entity_identity(hass, loaded_entry) -> None:
    """The hardening release does not adopt multi-valve identity migrations."""
    entry, _client = loaded_entry
    registry = er.async_get(hass)
    unique_ids = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }

    assert entry.unique_id == "11"
    assert "monitored_valves" not in entry.options
    assert "uuid-1_mode" in unique_ids
    assert "uuid-1_valve_mode" in unique_ids
