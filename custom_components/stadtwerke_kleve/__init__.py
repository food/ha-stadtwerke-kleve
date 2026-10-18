"""The Stadtwerke Kleve integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthError, Portal, PortalData, PortalError
from .const import CONF_CONSUMPTION_GAS, CONF_CONSUMPTION_POWER, DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

type StadtwerkeKleveConfigEntry = ConfigEntry[DataUpdateCoordinator[PortalData]]


async def async_setup_entry(hass: HomeAssistant, entry: StadtwerkeKleveConfigEntry) -> bool:
    """Set up Stadtwerke Kleve from a config entry."""
    # A session of its own, so the portal cookies stay out of the shared one.
    session = async_create_clientsession(hass)
    portal = Portal(session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    # 0 or unset means: estimate from the meter readings.
    consumption = {
        "POWER": entry.options.get(CONF_CONSUMPTION_POWER, 0),
        "GAS": entry.options.get(CONF_CONSUMPTION_GAS, 0),
    }

    async def async_update_data() -> PortalData:
        """Fetch everything from the portal."""
        try:
            return await portal.fetch(consumption)
        except AuthError as err:
            raise ConfigEntryAuthFailed("Login rejected by the portal") from err
        except PortalError as err:
            raise UpdateFailed(str(err)) from err

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=DOMAIN,
        update_method=async_update_data,
        update_interval=UPDATE_INTERVAL,
        always_update=False,
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: StadtwerkeKleveConfigEntry) -> bool:
    """Migrate old config entries."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        # Up to 0.2.x the options form was prefilled with 3000 kWh, so a stored
        # 3000 is that default and not a value the user chose. 0 now means
        # "estimate from the meter readings".
        options = {
            key: 0
            if key in (CONF_CONSUMPTION_POWER, CONF_CONSUMPTION_GAS) and value == 3000
            else value
            for key, value in entry.options.items()
        }
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: StadtwerkeKleveConfigEntry) -> None:
    """Reload the entry after the options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: StadtwerkeKleveConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
