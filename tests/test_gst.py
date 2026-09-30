"""The option to add GST to costs and prices.

The API's prices and interval costs exclude GST, as the Consumer Data Right
standard requires, but the bill includes it. With the option on, every cost
statistic and the current price are 10% higher; energy is untouched.
"""

from __future__ import annotations

from datetime import date, datetime, time
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from conftest import load
from fake_client import FakeClient, async_setup_integration, sensor_state
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.first_energy.api.parsers import (
    parse_plans,
    parse_service_point,
    parse_usage,
)
from custom_components.first_energy.const import (
    CONF_ACCOUNT_ID,
    CONF_BACKFILL_CURSOR,
    CONF_BACKFILL_DONE,
    CONF_INCLUDE_GST,
    DOMAIN,
    STAT_COST,
    STAT_ENERGY,
    SUPPLY_CHARGE_BAND,
    band_statistic_id,
    statistic_id,
)
from custom_components.first_energy.coordinator import FirstEnergyCoordinator
from custom_components.first_energy.domain import Account
from custom_components.first_energy.services.statistics import HourlyBucket, scale_costs

SYDNEY = ZoneInfo("Australia/Sydney")
NMI = "9999990001"
# A Wednesday inside the fixture plan, at a time the shoulder rate applies.
SHOULDER = datetime.combine(date(2026, 8, 12), time(15, 59), tzinfo=SYDNEY)


class TestScaling:
    def test_every_cost_is_scaled_and_energy_is_not(self):
        bucket = HourlyBucket(
            start=SHOULDER, register_id="E1", energy_kwh=2.0, cost_aud=1.0,
            energy_by_tou={"Peak": 2.0}, cost_by_tou={"Peak": 0.8}, supply_aud=0.2)
        [scaled] = scale_costs([bucket], 1.1)
        assert scaled.cost_aud == pytest.approx(1.1)
        assert scaled.cost_by_tou == {"Peak": pytest.approx(0.88)}
        assert scaled.supply_aud == pytest.approx(0.22)
        assert scaled.energy_kwh == 2.0
        assert scaled.energy_by_tou == {"Peak": 2.0}

    def test_an_unpriced_hour_stays_unpriced(self):
        bucket = HourlyBucket(start=SHOULDER, register_id="E1", cost_aud=None)
        assert scale_costs([bucket], 1.1)[0].cost_aud is None


async def import_week(hass: HomeAssistant, options: dict) -> dict[str, float]:
    """Import the fixture week and return each series' final running total."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="638594",
                            data={CONF_ACCOUNT_ID: "638594"}, options=options)
    entry.add_to_hass(hass)
    account = Account(account_id="638594", account_number="516645", open_status="OPEN",
                      creation_date=None, plan_name=None, service_point_ids=("663701",))
    coordinator = FirstEnergyCoordinator(hass, entry, MagicMock(), account)
    coordinator._plans = parse_plans(load("account_detail"))
    await coordinator._async_import(
        parse_service_point(load("servicepoint")), parse_usage(load("usage_recent_7d")))

    await async_wait_recording_done(hass)
    ids = {
        "energy": statistic_id(NMI, STAT_ENERGY),
        "cost": statistic_id(NMI, STAT_COST),
        "peak": band_statistic_id(NMI, STAT_COST, "Peak"),
        "supply": band_statistic_id(NMI, STAT_COST, SUPPLY_CHARGE_BAND),
    }
    rows = await hass.async_add_executor_job(
        statistics_during_period, hass, datetime(2026, 8, 1, tzinfo=SYDNEY), None,
        set(ids.values()), "hour", None, {"sum"})
    return {name: rows[stat_id][-1]["sum"] for name, stat_id in ids.items()}


def days_in_week() -> int:
    return len({d.read_date for d in parse_usage(load("usage_recent_7d"))})


class TestImport:
    async def test_off_by_default(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        totals = await import_week(hass, {})
        assert totals["supply"] == pytest.approx(1.10 * days_in_week())

    async def test_on_adds_gst_to_every_cost(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        totals = await import_week(hass, {CONF_INCLUDE_GST: True})
        assert totals["supply"] == pytest.approx(1.21 * days_in_week())
        # Still adds up, because every part was scaled alike.
        usage = parse_usage(load("usage_recent_7d"))
        assert totals["cost"] == pytest.approx(
            1.1 * sum(d.cost_aud for d in usage) + totals["supply"])

    async def test_energy_is_the_same_either_way(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        totals = await import_week(hass, {CONF_INCLUDE_GST: True})
        usage = parse_usage(load("usage_recent_7d"))
        assert totals["energy"] == pytest.approx(sum(d.energy_kwh for d in usage))


class TestPriceSensor:
    @pytest.mark.parametrize(("options", "price", "includes_gst"), [
        ({}, 0.30, False),
        ({CONF_INCLUDE_GST: False}, 0.30, False),
        ({CONF_INCLUDE_GST: True}, 0.33, True),
    ])
    async def test_the_price_follows_the_option(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant, freezer,
        options, price, includes_gst,
    ):
        freezer.move_to(SHOULDER)
        await async_setup_integration(hass, options)
        state = sensor_state(hass, "current_price")
        assert float(state.state) == pytest.approx(price)
        assert state.attributes["includes_gst"] is includes_gst


class TestOptionsFlow:
    @pytest.fixture
    async def entry(self, recorder_mock, enable_custom_integrations, hass: HomeAssistant):
        entry = await async_setup_integration(hass)
        FakeClient.backfills.clear()
        return entry

    async def change(self, hass: HomeAssistant, entry: MockConfigEntry, include_gst: bool):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        with patch("custom_components.first_energy.FirstEnergyClient", FakeClient):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_INCLUDE_GST: include_gst})
            await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY

    async def test_changing_it_imports_the_history_again(
        self, entry, hass: HomeAssistant
    ):
        await self.change(hass, entry, True)

        assert entry.options == {CONF_INCLUDE_GST: True}
        assert len(FakeClient.backfills) == 1
        assert entry.data[CONF_BACKFILL_DONE] is True
        assert sensor_state(hass, "current_price").attributes["includes_gst"] is True

    async def test_a_half_done_import_starts_again_from_the_beginning(
        self, entry, hass: HomeAssistant
    ):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_BACKFILL_DONE: False,
                         CONF_BACKFILL_CURSOR: "2026-08-01"})

        await self.change(hass, entry, True)

        [(_, oldest)] = FakeClient.backfills
        assert oldest < date(2026, 8, 1)

    async def test_saving_it_unchanged_does_not(self, entry, hass: HomeAssistant):
        await self.change(hass, entry, False)

        assert FakeClient.backfills == []
        assert entry.data[CONF_BACKFILL_DONE] is True
