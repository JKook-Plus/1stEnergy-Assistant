"""The plan and tariff: supply charge in the cost statistic, and the tariff sensors.

The usage reads price each kilowatt-hour but leave out the daily supply
charge, so the Energy dashboard's cost fell short of the bill by that much
every day. The charge comes from the plan in the account detail, which also
gives the rate and time-of-use period in force at any moment.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from conftest import load
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.first_energy.api.parsers import (
    ParseError,
    parse_accounts,
    parse_plans,
    parse_service_point,
)
from custom_components.first_energy.const import (
    CONF_ACCOUNT_ID,
    CONF_BACKFILL_DONE,
    DOMAIN,
    STAT_COST,
    STAT_ENERGY,
    statistic_id,
)
from custom_components.first_energy.coordinator import FirstEnergyCoordinator
from custom_components.first_energy.domain import (
    Account,
    Meter,
    Plan,
    Rate,
    Register,
    ServicePoint,
    TariffPeriod,
    TimeWindow,
    UsageDay,
)
from custom_components.first_energy.services.statistics import (
    HourlyBucket,
    add_daily_charge,
    bucket_hourly,
)

SYDNEY = ZoneInfo("Australia/Sydney")
NMI = "9999990001"
# A Wednesday, inside the fixture plan's dates.
DAY = date(2026, 8, 12)
EVERY_DAY = frozenset(("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"))


def at(clock: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=SYDNEY)


@pytest.fixture
def plan() -> Plan:
    return parse_plans(load("account_detail"))[0]


def flat_plan(*windows: TimeWindow, supply: str | None = None) -> Plan:
    return Plan(
        name="Test", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        periods=(TariffPeriod(start="01-01", end="12-31",
                              daily_supply_charge=Decimal(supply) if supply else None,
                              rates=(Rate("Peak", "peak", Decimal("0.5"), windows),)),),
    )


class TestParsing:
    def test_reads_the_plan(self, plan):
        assert plan.name == "Residential Time of Use"
        assert plan.start_date == date(2026, 6, 26)
        assert plan.end_date == date(2027, 6, 25)
        assert plan.pricing_model == "TIME_OF_USE"

    def test_reads_every_rate_with_its_windows(self, plan):
        rates = {r.band: r for p in plan.periods for r in p.rates}
        assert set(rates) == {"peak", "shoulder", "off_peak"}
        assert rates["peak"].unit_price == Decimal("0.55")
        assert rates["peak"].name == "Peak Usage"
        assert rates["off_peak"].windows[0].start == time(0, 0)
        assert rates["off_peak"].windows[0].end == time(6, 59, 59)

    def test_the_supply_charge_is_found_on_whichever_period_carries_it(self, plan):
        assert plan.daily_supply_charge(DAY) == Decimal("1.10")

    def test_a_rate_without_a_price_is_dropped(self):
        payload = {"data": {"plans": [{"planDetail": {"electricityContract": {
            "tariffPeriod": [{
                "dailySupplyCharge": "0.9",
                "timeOfUseRates": [{"type": "PEAK", "rates": []}],
            }]}}}]}}
        (plan,) = parse_plans(payload)
        assert plan.periods[0].rates == ()
        assert plan.daily_supply_charge(DAY) == Decimal("0.9")

    def test_an_account_without_plans_has_none(self):
        assert parse_plans({"data": {"accountId": "1"}}) == ()

    def test_a_malformed_plan_is_a_parse_error(self):
        with pytest.raises(ParseError):
            parse_plans({"data": {"plans": ["nope"]}})


class TestRates:
    @pytest.mark.parametrize(("clock", "band"), [
        ("00:00:00", "off_peak"),
        ("06:59:59", "off_peak"),
        ("07:00:00", "shoulder"),
        ("15:59:59", "shoulder"),
        ("16:00:00", "peak"),
        ("19:59:59", "peak"),
        ("20:00:00", "shoulder"),
        ("22:00:00", "off_peak"),
        ("23:59:59", "off_peak"),
    ])
    def test_the_rate_follows_the_clock(self, plan, clock, band):
        rate = plan.rate_at(at(clock))
        assert rate is not None
        assert rate.band == band

    def test_no_rate_outside_the_plan_dates(self, plan):
        assert plan.rate_at(at("12:00:00", date(2026, 6, 25))) is None
        assert plan.daily_supply_charge(date(2027, 6, 26)) is None

    def test_a_window_can_run_past_midnight(self):
        window = TimeWindow(start=time(22), end=time(6, 59, 59), days=frozenset({"WED"}))
        assert window.contains(at("23:00:00"))                         # Wednesday night
        assert window.contains(at("03:00:00", DAY + timedelta(days=1)))  # into Thursday
        assert not window.contains(at("03:00:00"))                     # Tuesday's night
        assert not window.contains(at("12:00:00"))

    def test_a_rate_applies_only_on_its_days(self):
        weekdays = frozenset(("MON", "TUE", "WED", "THU", "FRI"))
        plan = flat_plan(TimeWindow(time(0), time(23, 59, 59), weekdays))
        assert plan.rate_at(at("12:00:00")) is not None
        assert plan.rate_at(at("12:00:00", date(2026, 8, 15))) is None  # Saturday

    def test_a_period_can_span_the_new_year(self):
        period = TariffPeriod(start="11-01", end="02-28")
        assert period.covers(date(2026, 12, 25))
        assert period.covers(date(2027, 1, 15))
        assert not period.covers(date(2026, 6, 1))

    def test_a_charge_repeated_on_several_periods_is_counted_once(self):
        periods = tuple(TariffPeriod("01-01", "12-31", Decimal("1.00")) for _ in range(2))
        plan = Plan(name="x", start_date=None, end_date=None, periods=periods)
        assert plan.daily_supply_charge(DAY) == Decimal("1.00")

    @pytest.mark.parametrize(("clock", "expected"), [
        ("15:30:00", "16:00:00"),
        ("15:59:59", "16:00:00"),
        ("21:00:00", "22:00:00"),
    ])
    def test_the_next_change_is_the_next_window_boundary(self, plan, clock, expected):
        assert plan.next_change(at(clock)) == at(expected)

    def test_a_flat_rate_never_changes(self):
        plan = Plan(name="x", start_date=None, end_date=None, periods=(
            TariffPeriod("01-01", "12-31", rates=(
                Rate("Anytime", "peak", Decimal("0.3"),
                     (TimeWindow(time(0), time(23, 59, 59), EVERY_DAY),)),)),))
        assert plan.next_change(at("12:00:00")) is None


def hours(day: date, count: int, *, cost: float | None = 0.1) -> list[HourlyBucket]:
    start = datetime.combine(day, time(), tzinfo=SYDNEY)
    return [HourlyBucket(start=start + timedelta(hours=i), register_id="E1",
                         energy_kwh=1.0, cost_aud=cost) for i in range(count)]


class TestSupplyCharge:
    def test_each_hour_carries_an_equal_share(self):
        charged = add_daily_charge(hours(DAY, 24), SYDNEY, lambda d: 2.4)
        assert all(b.cost_aud == pytest.approx(0.2) for b in charged)
        assert all(b.supply_aud == pytest.approx(0.1) for b in charged)

    def test_the_day_adds_up_to_the_charge(self):
        charged = add_daily_charge(hours(DAY, 24), SYDNEY, lambda d: 2.103)
        assert sum(b.supply_aud for b in charged) == pytest.approx(2.103)

    def test_a_short_daylight_saving_day_still_adds_up(self):
        """4 October 2026 has 23 hours in Sydney."""
        dst = date(2026, 10, 4)
        day = UsageDay(
            service_point_id="1", register_id="E1", read_date=dst, unit_of_measure="kWh",
            controlled_load=False, interval_minutes=5, energy_kwh=27.6, cost_aud=2.76,
            intervals=tuple([0.1] * 276), costings=tuple([0.01] * 276))
        buckets = bucket_hourly([day], SYDNEY).buckets
        assert len(buckets) == 23
        charged = add_daily_charge(buckets, SYDNEY, lambda d: 2.3)
        assert sum(b.supply_aud for b in charged) == pytest.approx(2.3)
        assert sum(b.cost_aud or 0 for b in charged) == pytest.approx(2.76 + 2.3)

    def test_each_day_gets_its_own_charge(self):
        buckets = hours(DAY, 24) + hours(DAY + timedelta(days=1), 24)
        charged = add_daily_charge(buckets, SYDNEY,
                                   lambda d: 1.2 if d == DAY else 2.4)
        assert sum(b.supply_aud for b in charged[:24]) == pytest.approx(1.2)
        assert sum(b.supply_aud for b in charged[24:]) == pytest.approx(2.4)

    def test_no_charge_leaves_the_costs_alone(self):
        buckets = hours(DAY, 24)
        assert add_daily_charge(buckets, SYDNEY, lambda d: None) == tuple(buckets)

    def test_hours_without_a_cost_stay_without_one(self):
        charged = add_daily_charge(hours(DAY, 24, cost=None), SYDNEY, lambda d: 2.4)
        assert all(b.cost_aud is None for b in charged)


def usage_day(kwh: float) -> UsageDay:
    return UsageDay(
        service_point_id="663701", register_id="E1", read_date=DAY, unit_of_measure="kWh",
        controlled_load=False, interval_minutes=5, energy_kwh=kwh * 288, cost_aud=2.88,
        intervals=tuple([kwh] * 288), costings=tuple([0.01] * 288),
        tou=tuple(["Off Peak"] * 288))


def service_point() -> ServicePoint:
    return ServicePoint(
        service_point_id="663701", nmi=NMI, status="ACTIVE", jurisdiction_code="NSW",
        is_generator=False, meters=(Meter("M1", None, (Register("E1", "CURRENT", "KWH"),)),))


async def stored(hass: HomeAssistant, kind: str) -> list[dict]:
    await async_wait_recording_done(hass)
    stat_id = statistic_id(NMI, kind)
    rows = await hass.async_add_executor_job(
        statistics_during_period, hass, datetime(2026, 8, 1, tzinfo=SYDNEY), None,
        {stat_id}, "hour", None, {"state", "sum"})
    return rows.get(stat_id, [])


class TestImport:
    async def test_the_cost_statistic_includes_the_supply_charge(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant, plan
    ):
        entry = MockConfigEntry(domain=DOMAIN, unique_id="638594",
                                data={CONF_ACCOUNT_ID: "638594"})
        entry.add_to_hass(hass)
        account = Account(account_id="638594", account_number="516645", open_status="OPEN",
                          creation_date=None, plan_name=None, service_point_ids=("663701",))
        coordinator = FirstEnergyCoordinator(hass, entry, MagicMock(), account)
        coordinator._plans = (plan,)

        await coordinator._async_import(service_point(), [usage_day(0.1)])

        cost = await stored(hass, STAT_COST)
        assert cost[-1]["sum"] == pytest.approx(2.88 + 1.10)
        # Energy is untouched by it.
        energy = await stored(hass, STAT_ENERGY)
        assert energy[-1]["sum"] == pytest.approx(28.8)


class FakeClient:
    """Enough of the client for a full setup, with the fixture plan."""

    def __init__(self, *args, **kwargs) -> None:
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


@pytest.fixture
async def loaded(recorder_mock, enable_custom_integrations, hass: HomeAssistant, freezer):
    freezer.move_to(at("15:59:00"))
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=6, unique_id="638594",
        data={"username": "user@example.com", "password": "hunter2",
              CONF_ACCOUNT_ID: "638594", CONF_BACKFILL_DONE: True},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.first_energy.FirstEnergyClient", FakeClient):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def state(hass: HomeAssistant, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"638594_{key}")
    assert entity_id is not None
    return hass.states.get(entity_id)


class TestSensors:
    async def test_the_current_price_and_period(self, loaded, hass: HomeAssistant):
        assert float(state(hass, "current_price").state) == pytest.approx(0.30)
        assert state(hass, "current_price").attributes["unit_of_measurement"] == "AUD/kWh"
        assert state(hass, "current_period").state == "shoulder"

    async def test_they_change_when_the_period_does(
        self, loaded, hass: HomeAssistant, freezer
    ):
        freezer.move_to(at("16:00:00"))
        async_fire_time_changed(hass, at("16:00:00"))
        await hass.async_block_till_done()

        assert float(state(hass, "current_price").state) == pytest.approx(0.55)
        assert state(hass, "current_period").state == "peak"

    async def test_the_plan_end_date(self, loaded, hass: HomeAssistant):
        plan_end = state(hass, "plan_end")
        assert plan_end.state == "2027-06-25"
        assert plan_end.attributes["plan"] == "Residential Time of Use"
