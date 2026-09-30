"""A fake client for tests that set up the whole integration.

It stands in for `FirstEnergyClient` at the point `__init__` constructs it,
and answers from the synthetic fixtures, so a test can load the config entry
and read real entity states without an HTTP server.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import patch

from conftest import load
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.first_energy.api.parsers import (
    parse_accounts,
    parse_plans,
    parse_service_point,
)
from custom_components.first_energy.const import CONF_ACCOUNT_ID, CONF_BACKFILL_DONE, DOMAIN

ACCOUNT_ID = "638594"


class FakeClient:
    """Enough of the client for a full setup. Usage is always empty."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def async_get_accounts(self):
        return parse_accounts(load("accounts_electricity"))

    async def async_get_service_point(self, service_point_id):
        return parse_service_point(load("servicepoint"))

    async def async_get_plans(self, account_id):
        return parse_plans(load("account_detail"))

    async def async_get_balance(self, account_id):
        return Decimal("0")

    async def async_get_invoices(self, account_id):
        return ()

    async def async_get_usage(self, *args, **kwargs):
        return ()


async def async_setup_integration(hass: HomeAssistant) -> MockConfigEntry:
    """Load a config entry backed by `FakeClient`, backfill already done."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=6, unique_id=ACCOUNT_ID,
        data={"username": "user@example.com", "password": "hunter2",
              CONF_ACCOUNT_ID: ACCOUNT_ID, CONF_BACKFILL_DONE: True},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.first_energy.FirstEnergyClient", FakeClient):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def sensor_state(hass: HomeAssistant, key: str) -> State:
    """The state of the sensor with this description key."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{ACCOUNT_ID}_{key}")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state
