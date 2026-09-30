"""Collapse interval reads into the hourly buckets Home Assistant stores.

No Home Assistant imports — this is pure arithmetic over domain objects, so it
can be tested without spinning up a recorder.

Two decisions worth stating, because both are easy to get subtly wrong:

**Buckets are keyed on UTC hours.** Home Assistant's recorder stores long-term
statistics on UTC hour boundaries, so that is what we produce. It also sidesteps
two traps. On a daylight-saving "fall back" day the local hour 02:00 happens
twice; bucketing locally would merge two distinct hours into one and lose an
hour of consumption. And 1st Energy sells into South Australia, which sits at
UTC+9:30 — local hour boundaries there do not align with UTC ones at all.
Bucketing in UTC is correct in every jurisdiction; a 5-minute slot never
straddles a UTC hour boundary because 30 minutes divides evenly by 5.

**Timestamps advance in absolute time from local midnight.** Slot *i* starts at
`local_midnight + i * interval` measured as elapsed time, not as a naive clock
increment. On a transition day the clock jumps but elapsed time does not, so
the slots stay pinned to the right instants.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, tzinfo

from ..domain import UsageDay


@dataclass(frozen=True, slots=True)
class HourlyBucket:
    """One UTC hour of consumption for one register."""

    start: datetime
    register_id: str
    energy_kwh: float = 0.0
    # None when the reads carried no cost for this hour, which is not the
    # same as it costing nothing.
    cost_aud: float | None = 0.0
    energy_by_tou: dict[str, float] = field(default_factory=dict)
    # The share of a fixed daily charge included in `cost_aud`.
    supply_aud: float = 0.0

    @property
    def dominant_tou(self) -> str | None:
        """The time-of-use band accounting for most energy in this hour."""
        if not self.energy_by_tou:
            return None
        return max(self.energy_by_tou.items(), key=lambda kv: kv[1])[0]


@dataclass(frozen=True, slots=True)
class BucketResult:
    buckets: tuple[HourlyBucket, ...]
    warnings: tuple[str, ...] = ()


def bucket_hourly(
    days: Iterable[UsageDay],
    local_tz: tzinfo,
    *,
    register_id: str | None = None,
    registers: Collection[str] | None = None,
) -> BucketResult:
    """Collapse daily interval reads into hourly buckets, oldest first.

    `local_tz` is the service point's jurisdiction timezone, used only to
    resolve each `read_date` to an instant. Buckets are per register: a
    meter with two registers yields two buckets for each hour. Pass
    `register_id` to keep a single register, or `registers` to keep only
    those (the active ones), and `combine_registers` to sum what remains.

    Days without populated intervals are skipped — a request made without
    `interval-reads` still returns 288 zero slots and a `readIntervalLength`
    of 0, which would otherwise produce a day of phantom zeroes.
    """
    energy: dict[tuple[datetime, str], float] = {}
    cost: dict[tuple[datetime, str], float] = {}
    tou_split: dict[tuple[datetime, str], dict[str, float]] = {}
    warnings: list[str] = []

    for day in days:
        if register_id is not None and day.register_id != register_id:
            continue
        if registers is not None and day.register_id not in registers:
            continue
        if not day.has_intervals:
            continue

        expected = day.expected_slots
        if expected is not None and len(day.intervals) != expected:
            # Not an error: this is what a DST transition looks like. Surface
            # it so an unexpected one is visible in the log rather than silent.
            warnings.append(
                f"{day.read_date} ({day.register_id}): {len(day.intervals)} slots, "
                f"expected {expected} — daylight saving transition?")

        midnight_local = datetime.combine(
            day.read_date, datetime.min.time(), tzinfo=local_tz)
        base = midnight_local.astimezone(UTC)
        step = timedelta(minutes=day.interval_minutes)

        for i, kwh in enumerate(day.intervals):
            hour = (base + step * i).replace(minute=0, second=0, microsecond=0)
            key = (hour, day.register_id)
            energy[key] = energy.get(key, 0.0) + kwh
            if i < len(day.costings):
                cost[key] = cost.get(key, 0.0) + day.costings[i]
            if i < len(day.tou):
                band = tou_split.setdefault(key, {})
                band[day.tou[i]] = band.get(day.tou[i], 0.0) + kwh

    buckets = tuple(
        HourlyBucket(
            start=hour,
            register_id=reg,
            energy_kwh=round(energy[(hour, reg)], 6),
            cost_aud=round(cost[(hour, reg)], 6) if (hour, reg) in cost else None,
            energy_by_tou={k: round(v, 6) for k, v in
                           sorted(tou_split.get((hour, reg), {}).items())},
        )
        for hour, reg in sorted(energy, key=lambda k: (k[0], k[1]))
    )
    return BucketResult(buckets=buckets, warnings=tuple(warnings))


def _sum_known(values: Iterable[float | None]) -> float | None:
    """Total of the values that are known; None if none of them are."""
    known = [v for v in values if v is not None]
    return round(sum(known), 6) if known else None


def combine_registers(buckets: Iterable[HourlyBucket]) -> tuple[HourlyBucket, ...]:
    """Sum every register's bucket for the same hour into one, oldest first.

    A statistic holds one row per hour, so a meter with a controlled-load
    register beside its general one must be written as their total. Writing
    both under one statistic ID would keep only the last one's `state` while
    the running `sum` counted both.
    """
    by_hour: dict[datetime, list[HourlyBucket]] = {}
    for bucket in buckets:
        by_hour.setdefault(bucket.start, []).append(bucket)

    combined = []
    for hour in sorted(by_hour):
        parts = by_hour[hour]
        if len(parts) == 1:
            combined.append(parts[0])
            continue
        tou: dict[str, float] = {}
        for part in parts:
            for band, kwh in part.energy_by_tou.items():
                tou[band] = tou.get(band, 0.0) + kwh
        combined.append(HourlyBucket(
            start=hour,
            register_id="+".join(sorted(p.register_id for p in parts)),
            energy_kwh=round(sum(p.energy_kwh for p in parts), 6),
            cost_aud=_sum_known(p.cost_aud for p in parts),
            energy_by_tou={k: round(v, 6) for k, v in sorted(tou.items())},
        ))
    return tuple(combined)


def add_daily_charge(
    buckets: Sequence[HourlyBucket],
    local_tz: tzinfo,
    charge_for: Callable[[date], float | None],
) -> tuple[HourlyBucket, ...]:
    """Spread each local day's fixed charge evenly over its hours.

    The reads price only the energy; the daily supply charge is on the
    bill but in none of them. Each hour of a day carries an equal share,
    so the day adds up to the charge whether it has 23, 24 or 25 hours.

    Hours without a cost are left alone: the reads didn't price that
    hour, and adding the charge would make it look priced. Days are
    assigned by the local date an hour starts on.
    """
    by_day: dict[date, list[int]] = {}
    for i, bucket in enumerate(buckets):
        if bucket.cost_aud is not None:
            by_day.setdefault(bucket.start.astimezone(local_tz).date(), []).append(i)

    out = list(buckets)
    for day, hours in by_day.items():
        charge = charge_for(day)
        if not charge:
            continue
        share = charge / len(hours)
        for i in hours:
            bucket = out[i]
            assert bucket.cost_aud is not None
            # Unrounded, so the day's shares add back up to the charge.
            out[i] = replace(
                bucket,
                cost_aud=bucket.cost_aud + share,
                supply_aud=bucket.supply_aud + share,
            )
    return tuple(out)
