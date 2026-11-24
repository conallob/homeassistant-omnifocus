"""Data update coordinator for OmniFocus."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    OmniFocusAuthError,
    OmniFocusClient,
    OmniFocusConnectionError,
    OmniFocusCryptoError,
    OmniFocusError,
    OmniFocusPassphraseError,
    Snapshot,
    Target,
)
from .const import (
    CONF_MODE,
    CONF_PERSPECTIVE,
    CONF_PROJECT,
    CONF_SCAN_INTERVAL,
    CONF_TARGET_TYPE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MODE_READ_WRITE,
    SUBENTRY_TYPE_LIST,
    TARGET_INBOX,
    TARGET_PERSPECTIVE,
    TARGET_PROJECT,
)

_LOGGER = logging.getLogger(__name__)

type OmniFocusConfigEntry = ConfigEntry[OmniFocusCoordinator]


def target_for_subentry(subentry: ConfigSubentry) -> Target:
    """The OmniFocus target a to-do list subentry maps to."""
    kind = subentry.data[CONF_TARGET_TYPE]
    if kind == TARGET_PROJECT:
        return Target(TARGET_PROJECT, subentry.data[CONF_PROJECT])
    if kind == TARGET_PERSPECTIVE:
        return Target(TARGET_PERSPECTIVE, subentry.data[CONF_PERSPECTIVE])
    return Target(TARGET_INBOX)


def list_subentries(entry: ConfigEntry) -> dict[str, ConfigSubentry]:
    """The to-do list mappings of an entry."""
    return {
        sid: sub for sid, sub in entry.subentries.items() if sub.subentry_type == SUBENTRY_TYPE_LIST
    }


def any_writable(entry: ConfigEntry) -> bool:
    """True if at least one mapped list is in read-write mode."""
    return any(s.data.get(CONF_MODE) == MODE_READ_WRITE for s in list_subentries(entry).values())


class OmniFocusCoordinator(DataUpdateCoordinator[Snapshot]):
    """Polls the sync server and exposes a snapshot of every mapped target."""

    config_entry: OmniFocusConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: OmniFocusConfigEntry, client: OmniFocusClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client

    @property
    def targets(self) -> dict[str, Target]:
        """Subentry id -> target."""
        return {
            sid: target_for_subentry(s) for sid, s in list_subentries(self.config_entry).items()
        }

    async def _async_update_data(self) -> Snapshot:
        targets = list(set(self.targets.values()))
        try:
            return await self.client.async_refresh(targets, dt_util.get_default_time_zone())
        except (OmniFocusAuthError, OmniFocusPassphraseError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OmniFocusConnectionError as err:
            raise UpdateFailed(f"Cannot reach the OmniFocus sync server: {err}") from err
        except OmniFocusCryptoError as err:
            raise UpdateFailed(f"Could not decrypt the OmniFocus database: {err}") from err
        except OmniFocusError as err:
            raise UpdateFailed(str(err)) from err
