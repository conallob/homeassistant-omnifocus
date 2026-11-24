"""The OmniFocus integration: sync Home Assistant to-do lists with OmniFocus."""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import DEFAULT_DATABASE, DEFAULT_SERVER, OmniFocusClient
from .application_credentials import OmniFocusCredentials
from .const import (
    CONF_CLIENT_ID,
    CONF_DATABASE,
    CONF_HOST_ID,
    CONF_PASSPHRASE,
    CONF_REGISTERED,
    CONF_SERVER_URL,
)
from .coordinator import OmniFocusConfigEntry, OmniFocusCoordinator, any_writable

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.TODO]


async def async_setup_entry(hass: HomeAssistant, entry: OmniFocusConfigEntry) -> bool:
    """Set up OmniFocus from a config entry."""
    try:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    except config_entry_oauth2_flow.ImplementationUnavailableError as err:
        raise ConfigEntryNotReady("Application credentials are not available yet") from err
    except ValueError as err:
        raise ConfigEntryAuthFailed(
            "The OmniFocus application credential was removed; add it again"
        ) from err
    if not isinstance(implementation, OmniFocusCredentials):
        raise ConfigEntryAuthFailed("Unexpected credential type")

    client = OmniFocusClient(
        async_get_clientsession(hass),
        username=implementation.username,
        password=implementation.password,
        passphrase=entry.data.get(CONF_PASSPHRASE) or None,
        run_in_executor=hass.async_add_executor_job,
        client_id=entry.data[CONF_CLIENT_ID],
        host_id=entry.data[CONF_HOST_ID],
        client_name=f"Home Assistant ({hass.config.location_name})",
        registration_date=datetime.fromisoformat(entry.data[CONF_REGISTERED])
        if CONF_REGISTERED in entry.data
        else dt_util.utcnow(),
        base_url=entry.data.get(CONF_SERVER_URL, DEFAULT_SERVER),
        database=entry.data.get(CONF_DATABASE, DEFAULT_DATABASE),
        # An account with no read-write list never writes anything at all.
        read_only=not any_writable(entry),
    )

    coordinator = OmniFocusCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: OmniFocusConfigEntry) -> None:
    """Reload when options or list mappings change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OmniFocusConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
