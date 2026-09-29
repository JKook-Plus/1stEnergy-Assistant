"""One test per smaller fix from the audit (F5, F6, F7 and the robustness items)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from conftest import load
from fake_api import FakeApi
from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.first_energy import async_migrate_entry
from custom_components.first_energy.api.client import FirstEnergyClient
from custom_components.first_energy.api.parsers import (
    ParseError,
    parse_accounts,
    parse_invoices,
    parse_service_point,
    parse_usage,
)
from custom_components.first_energy.const import (
    CONF_ACCOUNT_ID,
    DOMAIN,
    ROLLING_WINDOW_DAYS,
    STAT_COST,
    statistic_id,
)
from custom_components.first_energy.coordinator import FirstEnergyCoordinator
from custom_components.first_energy.domain import Account, UsageDay
from custom_components.first_energy.sensor import SENSORS
from custom_components.first_energy.services.statistics import (
    HourlyBucket,
    bucket_hourly,
)
from custom_components.first_energy.statistics import async_import_buckets

NMI = "9999990001"
START = datetime(2026, 8, 1, tzinfo=UTC)


def account(*service_point_ids: str) -> Account:
    return Account(account_id="638594", account_number="516645", open_status="OPEN",
                   creation_date=None, plan_name=None, service_point_ids=service_point_ids)


def coordinator(hass: HomeAssistant, acct: Account, client=None) -> FirstEnergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="638594", data={CONF_ACCOUNT_ID: "638594"})
    entry.add_to_hass(hass)
    return FirstEnergyCoordinator(hass, entry, client or MagicMock(), acct)


class TestServicePoints:
    """F5 and the no-connection case."""

    async def test_more_than_one_connection_is_warned_about(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant, caplog
    ):
        client = MagicMock()
        client.async_get_service_point = AsyncMock(return_value=MagicMock())
        coord = coordinator(hass, account("663701", "663702"), client)
        with caplog.at_level(logging.WARNING):
            await coord._async_setup()
        assert "2 electricity connections" in caplog.text
        client.async_get_service_point.assert_awaited_once_with("663701")

    async def test_no_connection_is_a_permanent_setup_error(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        """UpdateFailed would retry setup forever; nothing will change."""
        with pytest.raises(ConfigEntryError):
            await coordinator(hass, account())._async_setup()


class TestAuthLock:
    """F7: the rolling poll and the backfill both request headers."""

    async def test_concurrent_requests_log_in_once(self, socket_enabled):
        api = FakeApi()
        api.base_url = await api.start()
        try:
            api.stub_auth()
            api.queue("accounts", payload=load("accounts_electricity"), always=True)
            async with aiohttp.ClientSession() as session:
                client = FirstEnergyClient(
                    session, "user@example.com", "hunter2",
                    api_base=api.base_url, portal_base=api.base_url, backfill_delay=0,
                )
                await asyncio.gather(*(client.async_get_accounts() for _ in range(3)))
            assert api.count("login") == 1
            assert api.count("bff") == 1
        finally:
            await api.stop()


class TestUnits:
    """F6."""

    async def test_cost_statistic_has_no_unit(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        await async_import_buckets(
            hass, NMI, [HourlyBucket(start=START, register_id="E1", energy_kwh=1.0,
                                     cost_aud=0.3)],
            display_name="test",
        )
        await async_wait_recording_done(hass)
        stat_id = statistic_id(NMI, STAT_COST)
        metadata = await hass.async_add_executor_job(
            lambda: get_metadata(hass, statistic_ids={stat_id}))
        assert metadata[stat_id][1]["unit_of_measurement"] is None

    def test_money_sensors_are_in_aud(self):
        money = [d for d in SENSORS if d.device_class == "monetary"]
        assert money
        assert all(d.native_unit_of_measurement == "AUD" for d in money)

    async def test_upgrade_relabels_the_balance_statistics(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2,
                                unique_id="638594", data={CONF_ACCOUNT_ID: "638594"})
        entry.add_to_hass(hass)
        entity_id = er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, "638594_balance", config_entry=entry).entity_id
        # Statistics as an earlier version recorded them, in "$".
        async_import_statistics(
            hass,
            StatisticMetaData(mean_type=StatisticMeanType.NONE, has_sum=True, name=None,
                              source="recorder", statistic_id=entity_id,
                              unit_of_measurement="$", unit_class=None),
            [{"start": START, "state": 151.87, "sum": 151.87}],
        )
        await async_wait_recording_done(hass)

        assert await async_migrate_entry(hass, entry)
        await async_wait_recording_done(hass)

        metadata = await hass.async_add_executor_job(
            lambda: get_metadata(hass, statistic_ids={entity_id}))
        assert metadata[entity_id][1]["unit_of_measurement"] == "AUD"
        assert entry.minor_version == 3


class TestParserRobustness:
    """Malformed records raise ParseError, which callers handle, not KeyError."""

    def test_account_without_an_id(self):
        with pytest.raises(ParseError, match="accountId"):
            parse_accounts({"data": {"accounts": [{"accountNumber": "1"}]}})

    def test_account_that_is_not_an_object(self):
        with pytest.raises(ParseError):
            parse_accounts({"data": {"accounts": ["nope"]}})

    def test_invoice_that_is_not_an_object(self):
        with pytest.raises(ParseError):
            parse_invoices({"data": {"invoices": [None]}})

    def test_register_that_is_not_an_object(self):
        payload = {"data": {"nationalMeteringId": "1", "meters": [{"registers": [3]}]}}
        with pytest.raises(ParseError):
            parse_service_point(payload)

    def test_usage_read_that_is_not_an_object(self):
        with pytest.raises(ParseError):
            parse_usage({"data": {"reads": [[]]}})

    def test_malformed_optional_objects_count_as_absent(self):
        invoices = parse_invoices({"data": {"invoices": [
            {"invoiceNumber": "1", "period": "soon", "payOnTimeDiscount": 5}]}})
        assert invoices[0].period_start is None
        assert invoices[0].pay_on_time_discount is None


class TestRollingWindow:
    async def test_the_window_is_the_configured_number_of_days(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        client = MagicMock()
        client.async_get_balance = AsyncMock(return_value=None)
        client.async_get_invoices = AsyncMock(return_value=())
        client.async_get_usage = AsyncMock(return_value=())
        coord = coordinator(hass, account("663701"), client)
        coord._service_point = MagicMock(service_point_id="663701")
        coord._maybe_start_backfill = lambda sp: None

        await coord._async_update_data()
        _, oldest, newest = client.async_get_usage.await_args.args
        assert (newest - oldest).days + 1 == ROLLING_WINDOW_DAYS


class TestMissingCost:
    def test_hours_without_cost_have_none_not_zero(self):
        day = UsageDay(
            service_point_id="1", register_id="E1", read_date=date(2026, 8, 12),
            unit_of_measure="kWh", controlled_load=False, interval_minutes=5,
            energy_kwh=28.8, cost_aud=None, intervals=tuple([0.1] * 288),
        )
        buckets = bucket_hourly([day], ZoneInfo("Australia/Sydney")).buckets
        assert all(b.cost_aud is None for b in buckets)

    async def test_no_cost_row_is_written_for_them(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        await async_import_buckets(hass, NMI, [
            HourlyBucket(start=START, register_id="E1", energy_kwh=1.0, cost_aud=0.3),
            HourlyBucket(start=START + timedelta(hours=1), register_id="E1",
                         energy_kwh=1.0, cost_aud=None),
            HourlyBucket(start=START + timedelta(hours=2), register_id="E1",
                         energy_kwh=1.0, cost_aud=0.3),
        ], display_name="test")
        await async_wait_recording_done(hass)
        stat_id = statistic_id(NMI, STAT_COST)
        rows = (await hass.async_add_executor_job(
            statistics_during_period, hass, START, None, {stat_id}, "hour", None,
            {"state", "sum"}))[stat_id]
        assert [r["start"] for r in rows] == [
            START.timestamp(), (START + timedelta(hours=2)).timestamp()]
        assert rows[-1]["sum"] == pytest.approx(0.6)
