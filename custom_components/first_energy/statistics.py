"""Import 1st Energy history into Home Assistant's long-term statistics.

This is the Home Assistant boundary: it takes hourly buckets produced by
`services.statistics` (which knows nothing of HA) and writes them to the
recorder.

Why external statistics rather than sensors: the data arrives roughly a day
late. A normal sensor with `state_class: total_increasing` records whatever
value it holds at the moment the recorder samples it, which would file
Thursday's consumption under Friday and skew every Energy dashboard total.
External statistics are written with explicit historical timestamps, so each
kilowatt-hour lands in the hour it was actually used.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STAT_COST, STAT_ENERGY, statistic_id
from .services.statistics import HourlyBucket

_LOGGER = logging.getLogger(__name__)


# How far back to look for the row before an import. Covers any ordinary gap
# (a missed poll, an HA outage); a longer one falls back to a full search.
BASELINE_LOOKBACK = timedelta(days=30)

# Earlier than any meter data; statistics_during_period needs a start.
_BEGINNING = datetime(2000, 1, 1, tzinfo=UTC)


async def _async_rows(
    hass: HomeAssistant, stat_id: str, start: datetime, end: datetime | None
) -> list[StatisticsRow]:
    """Stored hourly rows with `start <= row start < end`, oldest first."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass, start, end, {stat_id}, "hour", None, {"state", "sum"},
    )
    return rows.get(stat_id) or []


async def _async_last_row(hass: HomeAssistant, stat_id: str) -> StatisticsRow | None:
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"state", "sum"}
    )
    rows = (last or {}).get(stat_id) or []
    return rows[0] if rows else None


async def _async_sum_before(hass: HomeAssistant, stat_id: str, hour: datetime) -> float:
    """The running total as at the last stored row before `hour`.

    This is the baseline an import starting at `hour` continues from. It is
    the last row *strictly before* `hour`, however far back: taking only the
    hour immediately before would reset the total to zero after any gap,
    which the Energy dashboard shows as negative consumption.

    Zero only when nothing at all is stored before `hour`, which is the
    true start of the series.
    """
    last = await _async_last_row(hass, stat_id)
    if last is None:
        return 0.0
    if dt_util.utc_from_timestamp(last["start"]) < hour:
        return float(last.get("sum") or 0.0)

    rows = await _async_rows(hass, stat_id, hour - BASELINE_LOOKBACK, hour)
    if not rows:
        rows = await _async_rows(hass, stat_id, _BEGINNING, hour)
    return float(rows[-1].get("sum") or 0.0) if rows else 0.0


async def _async_write_series(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    points: Sequence[tuple[datetime, float]],
) -> None:
    """Write one statistic's hourly values and keep every later row consistent.

    `points` are (hour, value) pairs, oldest first. The running sum starts
    from whatever is stored before the first hour. Any rows stored *after*
    the last hour were summed from the old values of the hours being
    rewritten, so they are shifted by the difference; without that, filling
    a gap or correcting a day would leave the series dropping where the
    import ends.
    """
    if not points:
        return
    stat_id = metadata["statistic_id"]
    first_hour, last_hour = points[0][0], points[-1][0]

    running = await _async_sum_before(hass, stat_id, first_hour)
    old_end = await _async_sum_before(hass, stat_id, last_hour + timedelta(hours=1))

    rows: list[StatisticData] = []
    for hour, value in points:
        running += value
        rows.append(StatisticData(start=hour, state=value, sum=running))

    later: list[StatisticsRow] = []
    last = await _async_last_row(hass, stat_id)
    if last is not None and dt_util.utc_from_timestamp(last["start"]) > last_hour:
        later = await _async_rows(hass, stat_id, last_hour + timedelta(hours=1), None)

    delta = running - old_end
    if later and abs(delta) > 1e-9:
        _LOGGER.debug(
            "Shifting %d later %s rows by %.6f after rewriting %s .. %s",
            len(later), stat_id, delta, first_hour.isoformat(), last_hour.isoformat(),
        )
        for row in later:
            shifted = StatisticData(
                start=dt_util.utc_from_timestamp(row["start"]),
                sum=float(row.get("sum") or 0.0) + delta,
            )
            if (state := row.get("state")) is not None:
                shifted["state"] = state
            rows.append(shifted)

    async_add_external_statistics(hass, metadata, rows)


def _metadata(
    stat_id: str, name: str, unit: str | None, unit_class: str | None
) -> StatisticMetaData:
    return StatisticMetaData(
        has_mean=False,
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=stat_id,
        unit_of_measurement=unit,
        unit_class=unit_class,
    )


async def async_import_buckets(
    hass: HomeAssistant,
    nmi: str,
    buckets: Sequence[HourlyBucket],
    *,
    display_name: str,
) -> int:
    """Write energy and cost statistics for one meter. Returns hours written.

    Re-importing an hour already stored is safe and intentional — the recorder
    replaces rows matching a statistic id and start time. That is what lets the
    coordinator re-request a rolling window every poll and quietly repair gaps
    left by a failed run, without any explicit reconciliation logic. Imports
    can arrive in any order: each continues from the stored row before it and
    carries later rows along.

    Waits for the recorder to commit before returning, so the next import's
    baseline reads what this one wrote.
    """
    if not buckets:
        return 0

    await _async_write_series(
        hass,
        _metadata(statistic_id(nmi, STAT_ENERGY), f"{display_name} energy",
                  UnitOfEnergy.KILO_WATT_HOUR, "energy"),
        [(b.start, b.energy_kwh) for b in buckets],
    )
    # No unit, as core's opower does: the Energy dashboard shows costs in
    # the user's configured currency and ignores a statistic's own unit.
    # Hours without a cost are left out rather than written as free.
    await _async_write_series(
        hass,
        _metadata(statistic_id(nmi, STAT_COST), f"{display_name} cost", None, None),
        [(b.start, b.cost_aud) for b in buckets if b.cost_aud is not None],
    )
    await get_instance(hass).async_block_till_done()

    _LOGGER.debug(
        "Imported %d hourly buckets for %s (%s .. %s)",
        len(buckets), nmi, buckets[0].start.isoformat(), buckets[-1].start.isoformat(),
    )
    return len(buckets)
