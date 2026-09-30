"""A series per time-of-use band, beside the totals.

Every 5-minute read is labelled with its band ("Peak", "Off Peak"). The
integration used to work the split out per hour and throw it away; now each
band gets energy and cost statistics of its own, and the supply charge a
cost series, so that the bands add back up to the totals.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from conftest import load
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
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
    DOMAIN,
    STAT_COST,
    STAT_ENERGY,
    SUPPLY_CHARGE_BAND,
    band_slug,
    band_statistic_id,
    statistic_id,
)
from custom_components.first_energy.coordinator import FirstEnergyCoordinator
from custom_components.first_energy.domain import Account, UsageDay
from custom_components.first_energy.services.statistics import (
    bucket_hourly,
    combine_registers,
)

SYDNEY = ZoneInfo("Australia/Sydney")
NMI = "9999990001"
BANDS = ("Off Peak", "Peak", "Shoulder")


@pytest.fixture
def days() -> tuple[UsageDay, ...]:
    return parse_usage(load("usage_recent_7d"))


class TestBucketing:
    def test_each_hour_splits_its_cost_by_band(self, days):
        for bucket in bucket_hourly(days, SYDNEY).buckets:
            assert sum(bucket.cost_by_tou.values()) == pytest.approx(bucket.cost_aud)
            assert sum(bucket.energy_by_tou.values()) == pytest.approx(bucket.energy_kwh)

    def test_the_bands_are_the_ones_the_reads_name(self, days):
        buckets = bucket_hourly(days, SYDNEY).buckets
        assert {band for b in buckets for band in b.cost_by_tou} == set(BANDS)

    def test_a_slot_without_a_band_is_in_no_band(self):
        day = UsageDay(
            service_point_id="1", register_id="E1", read_date=date(2026, 8, 12),
            unit_of_measure="kWh", controlled_load=False, interval_minutes=5,
            energy_kwh=28.8, cost_aud=2.88, intervals=tuple([0.1] * 288),
            costings=tuple([0.01] * 288), tou=tuple(["None"] * 144 + ["Peak"] * 144))
        buckets = bucket_hourly([day], SYDNEY).buckets
        assert {band for b in buckets for band in b.energy_by_tou} == {"Peak"}

    def test_combined_registers_add_their_band_costs(self, days):
        second = [UsageDay(**{**{f: getattr(d, f) for f in d.__slots__},
                              "register_id": "E2"}) for d in days]
        one = bucket_hourly(days, SYDNEY).buckets
        both = combine_registers(bucket_hourly([*days, *second], SYDNEY).buckets)
        for single, double in zip(one, both, strict=True):
            for band, aud in single.cost_by_tou.items():
                assert double.cost_by_tou[band] == pytest.approx(2 * aud)


class TestIds:
    @pytest.mark.parametrize(("band", "slug"), [
        ("Peak", "peak"), ("Off Peak", "off_peak"), ("Off-Peak", "off_peak"),
        ("  Solar Sponge ", "solar_sponge"), ("supply_charge", "supply_charge"),
    ])
    def test_band_names_become_id_safe(self, band, slug):
        assert band_slug(band) == slug

    def test_band_ids_sit_beside_the_total(self):
        assert statistic_id(NMI, STAT_ENERGY) == f"first_energy:energy_{NMI}"
        assert band_statistic_id(NMI, STAT_ENERGY, "Off Peak") == (
            f"first_energy:energy_off_peak_{NMI}")


async def totals(hass: HomeAssistant, *stat_ids: str) -> dict[str, list[dict]]:
    await async_wait_recording_done(hass)
    rows = await hass.async_add_executor_job(
        statistics_during_period, hass, datetime(2026, 8, 1, tzinfo=SYDNEY), None,
        set(stat_ids), "hour", None, {"state", "sum"})
    return {stat_id: rows.get(stat_id, []) for stat_id in stat_ids}


@pytest.fixture
def coordinator(hass: HomeAssistant) -> FirstEnergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="638594",
                            data={CONF_ACCOUNT_ID: "638594"})
    entry.add_to_hass(hass)
    account = Account(account_id="638594", account_number="516645", open_status="OPEN",
                      creation_date=None, plan_name=None, service_point_ids=("663701",))
    return FirstEnergyCoordinator(hass, entry, MagicMock(), account)


class TestImport:
    async def test_band_energy_adds_up_to_the_total(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant,
        coordinator, days
    ):
        await coordinator._async_import(parse_service_point(load("servicepoint")), days)

        ids = [band_statistic_id(NMI, STAT_ENERGY, band) for band in BANDS]
        rows = await totals(hass, statistic_id(NMI, STAT_ENERGY), *ids)
        total = rows.pop(statistic_id(NMI, STAT_ENERGY))
        assert sum(r[-1]["sum"] for r in rows.values()) == pytest.approx(total[-1]["sum"])

    async def test_every_band_has_a_row_for_every_hour(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant,
        coordinator, days
    ):
        await coordinator._async_import(parse_service_point(load("servicepoint")), days)

        peak = band_statistic_id(NMI, STAT_ENERGY, "Peak")
        rows = await totals(hass, statistic_id(NMI, STAT_ENERGY), peak)
        assert len(rows[peak]) == len(rows[statistic_id(NMI, STAT_ENERGY)])
        assert any(r["state"] == 0 for r in rows[peak])

    async def test_band_costs_and_the_supply_charge_add_up_to_the_total(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant,
        coordinator, days
    ):
        coordinator._plans = parse_plans(load("account_detail"))
        await coordinator._async_import(parse_service_point(load("servicepoint")), days)

        parts = [band_statistic_id(NMI, STAT_COST, band)
                 for band in (*BANDS, SUPPLY_CHARGE_BAND)]
        rows = await totals(hass, statistic_id(NMI, STAT_COST), *parts)
        total = rows.pop(statistic_id(NMI, STAT_COST))
        assert sum(r[-1]["sum"] for r in rows.values()) == pytest.approx(total[-1]["sum"])
        supply = rows[band_statistic_id(NMI, STAT_COST, SUPPLY_CHARGE_BAND)]
        assert supply[-1]["sum"] == pytest.approx(1.10 * len(days))

    async def test_no_supply_series_without_a_plan(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant,
        coordinator, days
    ):
        await coordinator._async_import(parse_service_point(load("servicepoint")), days)

        supply = band_statistic_id(NMI, STAT_COST, SUPPLY_CHARGE_BAND)
        assert (await totals(hass, supply))[supply] == []
