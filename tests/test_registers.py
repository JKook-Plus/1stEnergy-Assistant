"""Meters with more than one register, live or removed.

A statistic holds one row per hour. Before this was fixed, a meter with two
registers wrote two rows per hour under one statistic ID (the recorder kept
the last one's `state` while the running `sum` counted both), and a removed
register's reads were counted as if it were still live.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.first_energy import async_migrate_entry
from custom_components.first_energy.const import (
    CONF_ACCOUNT_ID,
    CONF_BACKFILL_DONE,
    DOMAIN,
    STAT_ENERGY,
    statistic_id,
)
from custom_components.first_energy.coordinator import FirstEnergyCoordinator
from custom_components.first_energy.domain import (
    Account,
    Meter,
    Register,
    ServicePoint,
    UsageDay,
)
from custom_components.first_energy.services.statistics import (
    bucket_hourly,
    combine_registers,
)

SYDNEY = ZoneInfo("Australia/Sydney")
DAY = date(2026, 8, 12)
NMI = "9999990001"


def make_day(register: str, kwh: float, *, controlled_load: bool = False) -> UsageDay:
    return UsageDay(
        service_point_id="663701", register_id=register, read_date=DAY,
        unit_of_measure="kWh", controlled_load=controlled_load, interval_minutes=5,
        energy_kwh=kwh * 288, cost_aud=0.01 * 288,
        intervals=tuple([kwh] * 288),
        costings=tuple([0.01] * 288),
        tou=tuple(["Off Peak"] * 288),
    )


def register(register_id: str, status: str, *, controlled_load: bool = False) -> Register:
    return Register(register_id=register_id, status=status, unit_of_measure="KWH",
                    controlled_load=controlled_load)


def service_point(*registers: Register) -> ServicePoint:
    return ServicePoint(
        service_point_id="663701", nmi=NMI, status="ACTIVE", jurisdiction_code="NSW",
        is_generator=False, meters=(Meter(meter_id="M1", status=None, registers=registers),),
    )


class TestBucketing:
    def test_two_active_registers_give_one_bucket_per_hour(self):
        days = [make_day("E1", 0.1), make_day("E2", 0.05, controlled_load=True)]
        combined = combine_registers(bucket_hourly(days, SYDNEY).buckets)
        starts = [b.start for b in combined]
        assert len(starts) == len(set(starts)) == 24

    def test_combined_hours_are_the_sum_of_both_registers(self):
        days = [make_day("E1", 0.1), make_day("E2", 0.05, controlled_load=True)]
        combined = combine_registers(bucket_hourly(days, SYDNEY).buckets)
        # 12 five-minute slots per hour.
        assert all(b.energy_kwh == pytest.approx(12 * 0.15) for b in combined)
        assert all(b.cost_aud == pytest.approx(12 * 0.02) for b in combined)
        assert sum(b.energy_kwh for b in combined) == pytest.approx(288 * 0.15)

    def test_removed_register_is_excluded(self):
        days = [make_day("E1", 0.1), make_day("E0", 5.0)]
        buckets = bucket_hourly(days, SYDNEY, registers={"E1"}).buckets
        assert {b.register_id for b in buckets} == {"E1"}
        assert sum(b.energy_kwh for b in buckets) == pytest.approx(28.8)

    @pytest.mark.parametrize(("register_id", "expected"), [
        ("E1", True), ("E2", True), ("e1", True), ("1", True),
        ("B1", False), ("K1", False), ("Q1", False), ("", False),
    ])
    def test_which_registers_measure_consumption(self, register_id, expected):
        assert register(register_id, "CURRENT").measures_consumption is expected

    def test_consumption_registers_are_live_imports_only(self):
        sp = service_point(register("E1", "CURRENT"), register("K1", "CURRENT"),
                           register("E0", "REMOVED"))
        assert [r.register_id for r in sp.consumption_registers] == ["E1"]

    def test_a_single_register_passes_through_unchanged(self):
        buckets = bucket_hourly([make_day("E1", 0.1)], SYDNEY).buckets
        assert combine_registers(buckets) == buckets


@pytest.fixture
def coordinator(hass: HomeAssistant) -> FirstEnergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="638594",
                            data={CONF_ACCOUNT_ID: "638594"})
    entry.add_to_hass(hass)
    account = Account(account_id="638594", account_number="516645", open_status="OPEN",
                      creation_date=None, plan_name=None, service_point_ids=("663701",))
    return FirstEnergyCoordinator(hass, entry, MagicMock(), account)


async def stored_energy(hass: HomeAssistant) -> list[dict]:
    await async_wait_recording_done(hass)
    stat_id = statistic_id(NMI, STAT_ENERGY)
    rows = await hass.async_add_executor_job(
        statistics_during_period, hass,
        datetime(2026, 8, 1, tzinfo=SYDNEY), None,
        {stat_id}, "hour", None, {"state", "sum"},
    )
    return rows.get(stat_id, [])


class TestImport:
    async def test_no_duplicate_hours_for_two_active_registers(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant, coordinator
    ):
        sp = service_point(register("E1", "CURRENT"),
                           register("E2", "CURRENT", controlled_load=True))
        written = await coordinator._async_import(
            sp, [make_day("E1", 0.1), make_day("E2", 0.05, controlled_load=True)])

        assert written == 24
        rows = await stored_energy(hass)
        assert len(rows) == len({r["start"] for r in rows}) == 24
        assert rows[-1]["sum"] == pytest.approx(288 * 0.15)
        assert all(r["state"] == pytest.approx(12 * 0.15) for r in rows)

    async def test_reactive_registers_are_not_consumption(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant, coordinator
    ):
        """A smart meter's K1 and Q1 are reported in "kWh" but aren't usage."""
        sp = service_point(register("E1", "CURRENT"), register("K1", "CURRENT"),
                           register("Q1", "CURRENT"))
        await coordinator._async_import(
            sp, [make_day("E1", 0.1), make_day("K1", 0.01), make_day("Q1", 0.02)])

        rows = await stored_energy(hass)
        assert len(rows) == 24
        assert rows[-1]["sum"] == pytest.approx(28.8)

    async def test_startup_import_waits_until_home_assistant_has_started(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant, coordinator
    ):
        """The recorder holds its queue during startup; waiting on it from setup hung."""
        sp = service_point(register("E1", "CURRENT"))
        hass.set_state(CoreState.starting)
        coordinator._import_after_start(sp, [make_day("E1", 0.1)])
        await hass.async_block_till_done()
        assert await stored_energy(hass) == []

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        rows = await stored_energy(hass)
        assert len(rows) == 24
        assert rows[-1]["sum"] == pytest.approx(28.8)

    async def test_removed_register_contributes_nothing(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant, coordinator
    ):
        sp = service_point(register("E1", "CURRENT"), register("E0", "REMOVED"))
        await coordinator._async_import(sp, [make_day("E1", 0.1), make_day("E0", 5.0)])

        rows = await stored_energy(hass)
        assert len(rows) == 24
        assert rows[-1]["sum"] == pytest.approx(28.8)


class TestUpgrade:
    async def test_upgrade_reruns_the_backfill_once(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, version=1, minor_version=1, unique_id="638594",
            data={CONF_ACCOUNT_ID: "638594", CONF_BACKFILL_DONE: True},
        )
        entry.add_to_hass(hass)

        assert await async_migrate_entry(hass, entry)
        assert CONF_BACKFILL_DONE not in entry.data
        assert entry.data[CONF_ACCOUNT_ID] == "638594"
        assert entry.minor_version == 4

    async def test_migrated_entries_are_left_alone(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, version=1, minor_version=4, unique_id="638594",
            data={CONF_ACCOUNT_ID: "638594", CONF_BACKFILL_DONE: True},
        )
        entry.add_to_hass(hass)

        assert await async_migrate_entry(hass, entry)
        assert entry.data[CONF_BACKFILL_DONE] is True
