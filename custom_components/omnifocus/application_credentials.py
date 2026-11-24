"""Application credentials for the OmniFocus (Omni Sync Server) account.

Home Assistant's *Application Credentials* store holds a ``client_id`` and a
``client_secret``. Omni Sync Server does not use OAuth, so we reuse the store
purely as a credential vault: **client ID = sync username**, **client secret =
sync password**. The secret therefore lives in Home Assistant's credential
storage and never in the config entry.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.application_credentials import ClientCredential
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow


class OmniFocusCredentials(config_entry_oauth2_flow.AbstractOAuth2Implementation):
    """An Omni Sync Server username/password pair."""

    def __init__(self, auth_domain: str, credential: ClientCredential) -> None:
        self._auth_domain = auth_domain
        self.username = credential.client_id
        self.password = credential.client_secret
        self._name = credential.name or credential.client_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def domain(self) -> str:
        return self._auth_domain

    # The OAuth hooks are never used: there is no browser step for this service.
    async def async_generate_authorize_url(self, flow_id: str) -> str:
        raise NotImplementedError("Omni Sync Server does not use OAuth")

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        raise NotImplementedError("Omni Sync Server does not use OAuth")

    async def _async_refresh_token(self, token: dict) -> dict:
        raise NotImplementedError("Omni Sync Server does not use OAuth")


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> OmniFocusCredentials:
    """Return the credential holder for a stored application credential."""
    return OmniFocusCredentials(auth_domain, credential)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Placeholders for the 'Add application credential' dialog."""
    return {
        "more_info_url": "https://github.com/conallob/homeassistant-omnifocus#credentials",
    }
