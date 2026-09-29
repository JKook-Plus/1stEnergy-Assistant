"""1st Energy integration for Home Assistant.

Unofficial. Reads an Australian 1st Energy account through the private API
behind their customer portal, and feeds hourly energy and cost statistics into
the Energy dashboard.

Meter data lags roughly a day, so this integration is historical by nature:
statistics are backdated to the hours they belong to rather than published as
live sensor states.
"""

from __future__ import annotations

import logging

from homeassistant.components.recorder.statistics import async_update_statistics_metadata
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, AuthenticationError, FirstEnergyClient, FirstEnergyError
from .const import CONF_ACCOUNT_ID, CONF_BACKFILL_CURSOR, CONF_BACKFILL_DONE, CURRENCY, DOMAIN
from .coordinator import FirstEnergyCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type FirstEnergyConfigEntry = ConfigEntry[FirstEnergyCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: FirstEnergyConfigEntry) -> bool:
    client = FirstEnergyClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )

    try:
        accounts = await client.async_get_accounts()
    except AuthenticationError as err:
        from homeassistant.exceptions import ConfigEntryAuthFailed

        raise ConfigEntryAuthFailed("1st Energy rejected the stored credentials") from err
    except (ApiError, FirstEnergyError) as err:
        raise ConfigEntryNotReady(f"Cannot reach 1st Energy: {err}") from err

    account_id = entry.data[CONF_ACCOUNT_ID]
    account = next((a for a in accounts if a.account_id == account_id), None)
    if account is None:
        raise ConfigEntryNotReady(
            f"Account {account_id} is no longer visible on this login")

    coordinator = FirstEnergyCoordinator(hass, entry, client, account)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: FirstEnergyConfigEntry) -> bool:
    """Bring an entry's stored data up to the current version."""
    if entry.version > 1:
        # A newer major version this code doesn't understand.
        return False

    if entry.minor_version < 2:
        # Versions before 1.2 bucketed every register a meter reported, live
        # or removed, and a two-register meter wrote two rows per hour. The
        # fixed backfill rewrites the whole series, so run it once more.
        data = {**entry.data}
        data.pop(CONF_BACKFILL_DONE, None)
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
        _LOGGER.info("Re-importing 1st Energy history to correct earlier imports")

    if entry.minor_version < 3:
        # The balance sensor's unit changed from "$" to "AUD". Its long-term
        # statistics still say "$", and the recorder stops compiling them on
        # a unit mismatch until someone fixes it by hand; relabel them. The
        # amounts were always AUD, so the values themselves are unchanged.
        entity_id = er.async_get(hass).async_get_entity_id(
            SENSOR_DOMAIN, DOMAIN, f"{entry.data[CONF_ACCOUNT_ID]}_balance"
        )
        if entity_id is not None:
            async_update_statistics_metadata(
                hass, entity_id, new_unit_class=None, new_unit_of_measurement=CURRENCY
            )
        hass.config_entries.async_update_entry(entry, minor_version=3)

    if entry.minor_version < 4:
        # Versions before 1.4 sized each usage page by days, not records, so
        # a meter with several registers lost whole days, and they added
        # reactive-energy registers to consumption. Re-import the history.
        data = {**entry.data}
        data.pop(CONF_BACKFILL_DONE, None)
        data.pop(CONF_BACKFILL_CURSOR, None)
        hass.config_entries.async_update_entry(entry, data=data, minor_version=4)
        _LOGGER.info("Re-importing 1st Energy history to fill missing days")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: FirstEnergyConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
