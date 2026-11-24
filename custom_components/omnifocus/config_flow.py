"""Config flow: pick credentials, then add one subentry per mapped to-do list."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    DEFAULT_DATABASE,
    DEFAULT_SERVER,
    OmniFocusAuthError,
    OmniFocusClient,
    OmniFocusConnectionError,
    OmniFocusError,
    OmniFocusPassphraseError,
)
from .api.crypto import create_id
from .application_credentials import OmniFocusCredentials
from .const import (
    CONF_CLIENT_ID,
    CONF_DATABASE,
    CONF_HOST_ID,
    CONF_IMPLEMENTATION,
    CONF_MODE,
    CONF_NAME,
    CONF_PASSPHRASE,
    CONF_PERSPECTIVE,
    CONF_PROJECT,
    CONF_REGISTERED,
    CONF_SCAN_INTERVAL,
    CONF_SERVER_URL,
    CONF_TARGET_TYPE,
    DEFAULT_MODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    MODES,
    SUBENTRY_TYPE_LIST,
    TARGET_INBOX,
    TARGET_PROJECT,
    TARGET_TYPES,
)
from .coordinator import list_subentries

_LOGGER = logging.getLogger(__name__)


def _select(options: list[str] | list[SelectOptionDict], key: str | None = None) -> SelectSelector:
    config = SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    if key:
        config["translation_key"] = key
    return SelectSelector(config)


class OmniFocusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OmniFocus."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OmniFocusOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_LIST: TodoListSubentryFlow}

    async def _credentials(self) -> dict[str, OmniFocusCredentials]:
        impls = await config_entry_oauth2_flow.async_get_implementations(self.hass, DOMAIN)
        return {k: v for k, v in impls.items() if isinstance(v, OmniFocusCredentials)}

    async def _validate(
        self, creds: OmniFocusCredentials, server_url: str, database: str, passphrase: str
    ) -> str | None:
        """Try to connect and unlock; return an error key or ``None``."""
        client = OmniFocusClient(
            async_get_clientsession(self.hass),
            username=creds.username,
            password=creds.password,
            passphrase=passphrase or None,
            run_in_executor=self.hass.async_add_executor_job,
            client_id="validate",
            host_id="validate",
            client_name="validate",
            registration_date=datetime.now(UTC),
            base_url=server_url,
            database=database,
        )
        try:
            await client.async_validate()
        except OmniFocusAuthError:
            return "invalid_auth"
        except OmniFocusPassphraseError:
            return "invalid_passphrase"
        except OmniFocusConnectionError:
            return "cannot_connect"
        except OmniFocusError:
            _LOGGER.exception("Unexpected OmniFocus error during validation")
            return "unknown"
        except Exception:
            _LOGGER.exception("Unexpected exception during validation")
            return "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose credentials and (optionally) the encryption passphrase."""
        credentials = await self._credentials()
        if not credentials:
            return self.async_abort(reason="missing_credentials")
        errors: dict[str, str] = {}

        if user_input is not None:
            key = user_input.get(CONF_IMPLEMENTATION) or next(iter(credentials))
            creds = credentials[key]
            server_url = user_input.get(CONF_SERVER_URL, DEFAULT_SERVER).rstrip("/")
            database = user_input.get(CONF_DATABASE, DEFAULT_DATABASE)
            passphrase = user_input.get(CONF_PASSPHRASE, "")
            host = urlsplit(server_url).netloc or server_url
            await self.async_set_unique_id(f"{creds.username.lower()}@{host}/{database}")
            self._abort_if_unique_id_configured()
            if not (error := await self._validate(creds, server_url, database, passphrase)):
                return self.async_create_entry(
                    title=creds.username,
                    data={
                        "auth_implementation": key,
                        CONF_SERVER_URL: server_url,
                        CONF_DATABASE: database,
                        CONF_PASSPHRASE: passphrase,
                        CONF_CLIENT_ID: create_id(),
                        CONF_HOST_ID: str(uuid.uuid4()).upper(),
                        CONF_REGISTERED: datetime.now(UTC).isoformat(),
                    },
                )
            errors["base"] = error

        schema: dict[Any, Any] = {}
        if len(credentials) > 1:
            schema[vol.Required(CONF_IMPLEMENTATION)] = _select(
                [SelectOptionDict(value=k, label=v.name) for k, v in credentials.items()]
            )
        schema[vol.Optional(CONF_PASSPHRASE, default="")] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
        schema[vol.Optional(CONF_SERVER_URL, default=DEFAULT_SERVER)] = str
        schema[vol.Optional(CONF_DATABASE, default=DEFAULT_DATABASE)] = str
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Credentials or passphrase stopped working."""
        self._reauth_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._reauth_entry or self._get_reauth_entry()
        credentials = await self._credentials()
        if not credentials:
            return self.async_abort(reason="missing_credentials")
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input.get(CONF_IMPLEMENTATION) or next(iter(credentials))
            passphrase = user_input.get(CONF_PASSPHRASE, "")
            error = await self._validate(
                credentials[key],
                entry.data[CONF_SERVER_URL],
                entry.data[CONF_DATABASE],
                passphrase,
            )
            if not error:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        "auth_implementation": key,
                        CONF_PASSPHRASE: passphrase,
                    },
                )
            errors["base"] = error
        schema: dict[Any, Any] = {}
        if len(credentials) > 1:
            current = entry.data.get("auth_implementation")
            schema[vol.Required(CONF_IMPLEMENTATION, default=current)] = _select(
                [SelectOptionDict(value=k, label=v.name) for k, v in credentials.items()]
            )
        schema[vol.Optional(CONF_PASSPHRASE, default=entry.data.get(CONF_PASSPHRASE, ""))] = (
            TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema(schema), errors=errors
        )


class OmniFocusOptionsFlow(OptionsFlow):
    """Polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=30,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )


class TodoListSubentryFlow(ConfigSubentryFlow):
    """Map one Home Assistant to-do list to exactly one OmniFocus target."""

    def __init__(self) -> None:
        self._draft: dict[str, Any] = {}

    # ---- helpers ---------------------------------------------------------------

    def _existing(self) -> dict[str, dict[str, Any]]:
        """Other mappings of this account, keyed by subentry id."""
        entry = self._get_entry()
        own = self._get_reconfigure_subentry().subentry_id if self.source == "reconfigure" else None
        return {sid: dict(s.data) for sid, s in list_subentries(entry).items() if sid != own}

    def _taken(self, kind: str, ref: str | None) -> bool:
        for data in self._existing().values():
            if data[CONF_TARGET_TYPE] != kind:
                continue
            if kind == TARGET_INBOX or data.get(kind) == ref:
                return True
        return False

    def _snapshot(self):
        coordinator = getattr(self._get_entry(), "runtime_data", None)
        return coordinator.data if coordinator is not None else None

    def _defaults(self) -> dict[str, Any]:
        if self.source == "reconfigure":
            return dict(self._get_reconfigure_subentry().data)
        return {}

    # ---- steps -----------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._snapshot() is None:
            return self.async_abort(reason="entry_not_loaded")
        defaults = self._defaults()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._draft = dict(user_input)
            if user_input[CONF_TARGET_TYPE] == TARGET_INBOX:
                if self._taken(TARGET_INBOX, None):
                    errors["base"] = "already_mapped"
                else:
                    return self._finish()
            elif user_input[CONF_TARGET_TYPE] == TARGET_PROJECT:
                return await self.async_step_project()
            else:
                return await self.async_step_perspective()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "OmniFocus")): str,
                    vol.Required(
                        CONF_TARGET_TYPE, default=defaults.get(CONF_TARGET_TYPE, TARGET_INBOX)
                    ): _select(TARGET_TYPES, "target_type"),
                    vol.Required(CONF_MODE, default=defaults.get(CONF_MODE, DEFAULT_MODE)): _select(
                        MODES, "mode"
                    ),
                }
            ),
            errors=errors,
        )

    async def _pick(
        self, step_id: str, field: str, choices: dict[str, str], user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if not choices:
            return self.async_abort(reason=f"no_{field}s")
        if user_input is not None:
            ref = user_input[field]
            if self._taken(field, ref):
                errors["base"] = "already_mapped"
            else:
                self._draft[field] = ref
                return self._finish()
        options = [
            SelectOptionDict(value=k, label=v)
            for k, v in sorted(choices.items(), key=lambda kv: kv[1].lower())
        ]
        default = self._defaults().get(field)
        key = vol.Required(field, default=default) if default in choices else vol.Required(field)
        return self.async_show_form(
            step_id=step_id, data_schema=vol.Schema({key: _select(options)}), errors=errors
        )

    async def async_step_project(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        snap = self._snapshot()
        return await self._pick("project", CONF_PROJECT, snap.projects if snap else {}, user_input)

    async def async_step_perspective(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        snap = self._snapshot()
        return await self._pick(
            "perspective", CONF_PERSPECTIVE, snap.perspectives if snap else {}, user_input
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self.async_step_user(user_input)

    def _finish(self) -> SubentryFlowResult:
        data = {
            CONF_NAME: self._draft[CONF_NAME],
            CONF_TARGET_TYPE: self._draft[CONF_TARGET_TYPE],
            CONF_MODE: self._draft[CONF_MODE],
        }
        for field in (CONF_PROJECT, CONF_PERSPECTIVE):
            if field in self._draft and self._draft[CONF_TARGET_TYPE] == field:
                data[field] = self._draft[field]
        if self.source == "reconfigure":
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=data[CONF_NAME],
                data=data,
            )
        return self.async_create_entry(title=data[CONF_NAME], data=data)
