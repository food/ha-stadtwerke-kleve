"""Config flow for Stadtwerke Kleve."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import AuthError, Portal, PortalError
from .const import CONF_CONSUMPTION_GAS, CONF_CONSUMPTION_POWER, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
)
STEP_REAUTH_DATA_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class StadtwerkeKleveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return StadtwerkeKleveOptionsFlow()

    async def _validate_login(self, username: str, password: str) -> None:
        """Try a login with a short-lived session."""
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            await Portal(session, username, password).login()
        finally:
            await session.close()

    async def _try_login(self, username: str, password: str) -> dict[str, str]:
        """Check the credentials and map failures to form errors."""
        try:
            await self._validate_login(username, password)
        except AuthError:
            return {"base": "invalid_auth"}
        except PortalError:
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error during login")
            return {"base": "unknown"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for username and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
            self._abort_if_unique_id_configured()
            errors = await self._try_login(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            if not errors:
                return self.async_create_entry(title="Stadtwerke Kleve", data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after the portal rejected the login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        username = entry.data[CONF_USERNAME]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._try_login(username, user_input[CONF_PASSWORD])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={"username": username},
            errors=errors,
        )


class StadtwerkeKleveOptionsFlow(OptionsFlow):
    """Options for the price lookup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit the yearly consumption used for the prices."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        consumption = vol.All(vol.Coerce(int), vol.Range(min=0, max=1_000_000))
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CONSUMPTION_POWER,
                    default=options.get(CONF_CONSUMPTION_POWER, 0),
                ): consumption,
                vol.Required(
                    CONF_CONSUMPTION_GAS,
                    default=options.get(CONF_CONSUMPTION_GAS, 0),
                ): consumption,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
