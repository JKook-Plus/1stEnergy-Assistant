"""The history backfill: streamed oldest first, resumable, and backing off.

Before, every chunk was collected in memory and imported at the end, so a
failure on chunk 55 discarded the 54 before it, and the next poll started
the whole walk again.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.first_energy.api.exceptions import ApiError
from custom_components.first_energy.const import (
    CONF_ACCOUNT_ID,
    CONF_BACKFILL_CURSOR,
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

NMI = "9999990001"
KWH_PER_SLOT = 0.01
HOUR_KWH = 12 * KWH_PER_SLOT
HISTORY_DAYS = 90

SERVICE_POINT = ServicePoint(
    service_point_id="663701", nmi=NMI, status="ACTIVE", jurisdiction_code="NSW",
    is_generator=False,
    meters=(Meter(meter_id="M1", status=None, registers=(
        Register(register_id="E1", status="CURRENT", unit_of_measure="KWH"),)),),
)


def usage_day(day: date) -> UsageDay:
    return UsageDay(
        service_point_id="663701", register_id="E1", read_date=day,
        unit_of_measure="kWh", controlled_load=False, interval_minutes=5,
        energy_kwh=KWH_PER_SLOT * 288, cost_aud=0.001 * 288,
        intervals=tuple([KWH_PER_SLOT] * 288),
        costings=tuple([0.001] * 288),
        tou=tuple(["Off Peak"] * 288),
    )


def days_between(oldest: date, newest: date) -> tuple[UsageDay, ...]:
    return tuple(usage_day(oldest + timedelta(days=i))
                 for i in range((newest - oldest).days + 1))


class FakeClient:
    """Serves one reading per five minutes, and fails on request."""

    def __init__(self) -> None:
        self.windows: list[tuple[date, date]] = []
        self.fail_from: date | None = None
        self.always_fail = False

    async def async_iter_usage_range(self, service_point_id, oldest, newest, **kwargs):
        start = oldest
        while start <= newest:
            end = min(newest, start + timedelta(days=29))
            self.windows.append((start, end))
            if self.always_fail or (self.fail_from is not None and start >= self.fail_from):
                raise ApiError(503, "upstream unavailable")
            yield start, end, days_between(start, end)
            start = end + timedelta(days=1)


@pytest.fixture
def newest() -> date:
    """The coordinator backfills up to yesterday."""
    return dt_util.now().date() - timedelta(days=1)


@pytest.fixture
def third_chunk(newest) -> date:
    """First day of the third 30-day window of the 90-day history."""
    return newest - timedelta(days=HISTORY_DAYS - 1) + timedelta(days=60)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="638594",
                            data={CONF_ACCOUNT_ID: "638594"})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def coordinator(hass, entry, client, newest) -> FirstEnergyCoordinator:
    account = Account(
        account_id="638594", account_number="516645", open_status="OPEN",
        creation_date=newest - timedelta(days=HISTORY_DAYS - 1), plan_name=None,
        service_point_ids=("663701",),
    )
    return FirstEnergyCoordinator(hass, entry, client, account)


async def stored(hass: HomeAssistant) -> list[dict]:
    await async_wait_recording_done(hass)
    stat_id = statistic_id(NMI, STAT_ENERGY)
    rows = await hass.async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None,
        {stat_id}, "hour", None, {"state", "sum"},
    )
    return rows.get(stat_id, [])


def assert_monotonic(rows: list[dict]) -> None:
    sums = [r["sum"] for r in rows]
    assert all(b >= a - 1e-9 for a, b in pairwise(sums))


class TestStreaming:
    async def test_a_complete_backfill_imports_all_history(
        self, recorder_mock, enable_custom_integrations, hass, coordinator, entry
    ):
        await coordinator._async_backfill(SERVICE_POINT)

        rows = await stored(hass)
        assert len(rows) == HISTORY_DAYS * 24
        assert rows[-1]["sum"] == pytest.approx(HISTORY_DAYS * 24 * HOUR_KWH)
        assert entry.data[CONF_BACKFILL_DONE] is True
        assert CONF_BACKFILL_CURSOR not in entry.data

    async def test_starts_at_the_account_creation_date(
        self, recorder_mock, enable_custom_integrations, hass, coordinator, client, newest
    ):
        await coordinator._async_backfill(SERVICE_POINT)
        assert client.windows[0][0] == newest - timedelta(days=HISTORY_DAYS - 1)
        assert [w[0] for w in client.windows] == sorted(w[0] for w in client.windows)

    async def test_a_failure_keeps_the_chunks_already_written(
        self, recorder_mock, enable_custom_integrations, hass, coordinator, client, entry,
        third_chunk,
    ):
        client.fail_from = third_chunk
        await coordinator._async_backfill(SERVICE_POINT)

        rows = await stored(hass)
        assert len(rows) == 60 * 24
        assert_monotonic(rows)
        assert entry.data[CONF_BACKFILL_CURSOR] == third_chunk.isoformat()
        assert not entry.data.get(CONF_BACKFILL_DONE)

    async def test_the_next_attempt_resumes_after_them(
        self, recorder_mock, enable_custom_integrations, hass, coordinator, client, entry,
        third_chunk,
    ):
        third = third_chunk
        client.fail_from = third
        await coordinator._async_backfill(SERVICE_POINT)

        client.fail_from = None
        client.windows.clear()
        await coordinator._async_backfill(SERVICE_POINT)

        assert client.windows[0][0] == third
        rows = await stored(hass)
        assert len(rows) == HISTORY_DAYS * 24
        assert rows[-1]["sum"] == pytest.approx(HISTORY_DAYS * 24 * HOUR_KWH)
        assert_monotonic(rows)
        assert entry.data[CONF_BACKFILL_DONE] is True

    async def test_meeting_the_rolling_window_never_decreases_the_sum(
        self, recorder_mock, enable_custom_integrations, hass, coordinator, newest
    ):
        """The rolling poll writes the last few days before the backfill starts."""
        await coordinator._async_import(
            SERVICE_POINT, days_between(newest - timedelta(days=5), newest)
        )
        await coordinator._async_backfill(SERVICE_POINT)

        rows = await stored(hass)
        assert len(rows) == HISTORY_DAYS * 24
        assert_monotonic(rows)
        assert rows[-1]["sum"] == pytest.approx(HISTORY_DAYS * 24 * HOUR_KWH)


class TestBackoff:
    async def test_repeated_failures_are_retried_less_often(
        self, recorder_mock, enable_custom_integrations, hass, coordinator, client
    ):
        client.always_fail = True
        polls = 16
        for _ in range(polls):
            coordinator._maybe_start_backfill(SERVICE_POINT)
            await hass.async_block_till_done(wait_background_tasks=True)

        # Attempts on polls 1, 3, 6, 11 and 16: skipping 1, 2, 4, 4 in between.
        assert len(client.windows) == 5

    async def test_a_repair_issue_is_raised_after_repeated_failures(
        self, recorder_mock, enable_custom_integrations, hass, coordinator, client, entry
    ):
        client.always_fail = True
        issue_id = f"backfill_failing_{entry.entry_id}"
        registry = ir.async_get(hass)

        for attempt in range(1, 6):
            await coordinator._async_backfill(SERVICE_POINT)
            raised = registry.async_get_issue(DOMAIN, issue_id) is not None
            assert raised == (attempt >= 5)

        client.always_fail = False
        await coordinator._async_backfill(SERVICE_POINT)
        assert registry.async_get_issue(DOMAIN, issue_id) is None
