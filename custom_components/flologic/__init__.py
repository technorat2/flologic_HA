"""FloLogic Home Assistant integration."""

from __future__ import annotations

import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import FloLogicClient
from .const import (
    CONF_DEVICE_CODE,
    CONF_DEVICE_IDENTITY_VERSION,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TOKEN,
    CONF_HIDDEN_ENTITY_DEFAULTS_VERSION,
    CONF_HUB_URL,
    CONF_KEEP_SESSION_ALIVE,
    CONF_OPTIONS_DEFAULTS_VERSION,
    CONF_POLL_INTERVAL,
    DEFAULT_DEVICE_CODE,
    DEFAULT_DEVICE_NAME,
    DEFAULT_DEVICE_TOKEN,
    DEFAULT_HUB_URL,
    DEFAULT_KEEP_SESSION_ALIVE,
    DEFAULT_POLL_INTERVAL,
    DEVICE_IDENTITY_VERSION,
    DOMAIN,
    HIDDEN_ENTITY_DEFAULTS_VERSION,
    OPTIONS_DEFAULTS_VERSION,
    PLATFORMS,
)
from .coordinator import FloLogicCoordinator
from .device_identity import build_device_identity
from .exceptions import FloLogicError

SERVICE_SET_FLOW_SENSITIVITY = "set_flow_sensitivity"
SERVICE_SET_HOME_LIMIT = "set_home_limit"
SERVICE_SET_AWAY_LIMIT = "set_away_limit"
SERVICE_SET_BYPASS_TIME = "set_bypass_time"
SERVICE_SET_AUTO_AWAY = "set_auto_away"
SERVICE_SET_TEMP_ALERT = "set_temp_alert"
SERVICE_SET_TEMP_SHUTOFF = "set_temp_shutoff"
SERVICE_SET_PRE_ALERT = "set_pre_alert_notice"
SERVICE_SET_NO_FLOW_NOTICE = "set_no_flow_notice"

ATTR_VALUE = "value"
ATTR_MINUTES = "minutes"
ATTR_HOURS = "hours"
ATTR_SECONDS = "seconds"
ATTR_TEMPERATURE = "temperature"

HIDDEN_BY_DEFAULT_UNIQUE_ID_SUFFIXES = {
    "active_scheduler_events",
    "flow_started_at",
    "notification_always",
    "notification_auto_away",
    "notification_auto_shutoff",
    "notification_critical_error",
    "notification_delay_away",
    "notification_general_alert",
    "notification_guest_mode",
    "notification_history_count",
    "notification_never",
    "notification_no_flow",
    "signal_strength",
}

_LOGGER = logging.getLogger(__name__)

# These deliberately generous bounds catch unit mistakes and obviously invalid
# writes without claiming to model undocumented cloud-side limits exactly.
_MAX_MINUTES = 10080  # one week
_MAX_HOURS = 8760  # one year
_MAX_SECONDS = 604800  # one week
_MAX_FLOW_SENSITIVITY = 1000.0  # oz/min
_MIN_TEMPERATURE_F = -50
_MAX_TEMPERATURE_F = 150

_MINUTES = vol.All(cv.positive_int, vol.Range(max=_MAX_MINUTES))
_MINUTES_FLOAT = vol.All(vol.Coerce(float), vol.Range(min=0, max=_MAX_MINUTES))
_HOURS = vol.All(cv.positive_int, vol.Range(max=_MAX_HOURS))
_SECONDS = vol.All(cv.positive_int, vol.Range(max=_MAX_SECONDS))
_TEMPERATURE_F = vol.All(
    vol.Coerce(int), vol.Range(min=_MIN_TEMPERATURE_F, max=_MAX_TEMPERATURE_F)
)
_FLOW_SENSITIVITY = vol.All(
    vol.Coerce(float), vol.Range(min=0, max=_MAX_FLOW_SENSITIVITY)
)

WRITE_SERVICE_SCHEMAS = {
    SERVICE_SET_FLOW_SENSITIVITY: vol.Schema(
        {vol.Required(ATTR_VALUE): _FLOW_SENSITIVITY}
    ),
    SERVICE_SET_HOME_LIMIT: vol.Schema({vol.Required(ATTR_MINUTES): _MINUTES}),
    SERVICE_SET_AWAY_LIMIT: vol.Schema({vol.Required(ATTR_MINUTES): _MINUTES_FLOAT}),
    SERVICE_SET_BYPASS_TIME: vol.Schema({vol.Required(ATTR_MINUTES): _MINUTES}),
    SERVICE_SET_AUTO_AWAY: vol.Schema({vol.Required(ATTR_HOURS): _HOURS}),
    SERVICE_SET_TEMP_ALERT: vol.Schema(
        {vol.Required(ATTR_TEMPERATURE): _TEMPERATURE_F}
    ),
    SERVICE_SET_TEMP_SHUTOFF: vol.Schema(
        {vol.Required(ATTR_TEMPERATURE): _TEMPERATURE_F}
    ),
    SERVICE_SET_PRE_ALERT: vol.Schema({vol.Required(ATTR_MINUTES): _MINUTES}),
    SERVICE_SET_NO_FLOW_NOTICE: vol.Schema({vol.Required(ATTR_SECONDS): _SECONDS}),
}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions before a config entry is loaded."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FloLogic from a config entry."""
    _async_migrate_device_identity(hass, entry)
    _async_migrate_options_defaults(hass, entry)
    session = async_get_clientsession(hass)
    client = FloLogicClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        hub_url=entry.data.get(CONF_HUB_URL, DEFAULT_HUB_URL),
        device_name=entry.data.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME),
        device_code=entry.data.get(CONF_DEVICE_CODE, DEFAULT_DEVICE_CODE),
        device_token=entry.data.get(CONF_DEVICE_TOKEN, DEFAULT_DEVICE_TOKEN),
        session_factory=lambda: session,
        keep_session_alive=entry.options.get(
            CONF_KEEP_SESSION_ALIVE, DEFAULT_KEEP_SESSION_ALIVE
        ),
    )
    coordinator = FloLogicCoordinator(
        hass,
        client,
        entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_migrate_hidden_entity_defaults(hass, entry, coordinator)

    # Remain defensive for direct setup-entry tests and unusual loaders.
    _async_register_services(hass)
    return True


def _async_migrate_device_identity(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Replace old user-editable client-device identity values once."""
    if entry.data.get(CONF_DEVICE_IDENTITY_VERSION) == DEVICE_IDENTITY_VERSION:
        return
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            **build_device_identity(hass),
        },
    )


def _async_migrate_options_defaults(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply updated integration option defaults once for existing installs."""
    if entry.data.get(CONF_OPTIONS_DEFAULTS_VERSION) == OPTIONS_DEFAULTS_VERSION:
        return

    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_OPTIONS_DEFAULTS_VERSION: OPTIONS_DEFAULTS_VERSION,
        },
        options={
            **entry.options,
            CONF_KEEP_SESSION_ALIVE: DEFAULT_KEEP_SESSION_ALIVE,
        },
    )


def _async_migrate_hidden_entity_defaults(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: FloLogicCoordinator,
) -> None:
    """Disable newly hidden default entities for existing installs once."""
    if (
        entry.data.get(CONF_HIDDEN_ENTITY_DEFAULTS_VERSION)
        == HIDDEN_ENTITY_DEFAULTS_VERSION
    ):
        return

    registry = er.async_get(hass)
    unique_id_prefix = f"{coordinator.data.unique_id_prefix}_"
    for entity_id, entity_entry in list(registry.entities.items()):
        if (
            entity_entry.platform != DOMAIN
            or entity_entry.config_entry_id != entry.entry_id
            or not entity_entry.unique_id.startswith(unique_id_prefix)
        ):
            continue

        unique_id_suffix = entity_entry.unique_id.removeprefix(unique_id_prefix)
        if unique_id_suffix not in HIDDEN_BY_DEFAULT_UNIQUE_ID_SUFFIXES:
            continue

        registry.async_update_entity(
            entity_id,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )

    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_HIDDEN_ENTITY_DEFAULTS_VERSION: HIDDEN_ENTITY_DEFAULTS_VERSION,
        },
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a FloLogic config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.client.async_close()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register FloLogic services once."""
    if hass.data.setdefault(DOMAIN, {}).get("_services_registered"):
        return

    async def handle_service(call: ServiceCall) -> None:
        coordinator = _get_first_coordinator(hass)
        fields = _service_call_to_command(call)
        try:
            await coordinator.client.async_request_state_change(fields)
        except (FloLogicError, TimeoutError, ClientError) as err:
            message = f"FloLogic action {call.service} failed: {err}"
            _LOGGER.error("%s", message)
            raise HomeAssistantError(message) from err
        await coordinator.async_request_refresh()

    for service, schema in WRITE_SERVICE_SCHEMAS.items():
        hass.services.async_register(DOMAIN, service, handle_service, schema=schema)

    hass.data[DOMAIN]["_services_registered"] = True


def _get_first_coordinator(hass: HomeAssistant) -> FloLogicCoordinator:
    """Return the first configured FloLogic coordinator."""
    for key, value in hass.data.get(DOMAIN, {}).items():
        if key != "_services_registered":
            return value
    raise ServiceValidationError("No FloLogic config entry is loaded")


def _service_call_to_command(call: ServiceCall) -> dict[str, Any]:
    """Translate a Home Assistant service call to a FloLogic command."""
    data = call.data
    if call.service == SERVICE_SET_FLOW_SENSITIVITY:
        return {"dripRate": data[ATTR_VALUE]}
    if call.service == SERVICE_SET_HOME_LIMIT:
        return {"homeIntervalTime": data[ATTR_MINUTES]}
    if call.service == SERVICE_SET_AWAY_LIMIT:
        return {"awayIntervalTime": data[ATTR_MINUTES]}
    if call.service == SERVICE_SET_BYPASS_TIME:
        return {"bypassTime": data[ATTR_MINUTES]}
    if call.service == SERVICE_SET_AUTO_AWAY:
        return {"autoAwayTime": data[ATTR_HOURS]}
    if call.service == SERVICE_SET_TEMP_ALERT:
        return {"lowTemperatureAlert": data[ATTR_TEMPERATURE]}
    if call.service == SERVICE_SET_TEMP_SHUTOFF:
        return {"lowTemperatureLimit": data[ATTR_TEMPERATURE]}
    if call.service == SERVICE_SET_PRE_ALERT:
        return {"preAlertNoticeInterval": data[ATTR_MINUTES]}
    if call.service == SERVICE_SET_NO_FLOW_NOTICE:
        return {"noFlowNoticeInterval": data[ATTR_SECONDS]}
    raise ServiceValidationError(f"Unsupported FloLogic service: {call.service}")
