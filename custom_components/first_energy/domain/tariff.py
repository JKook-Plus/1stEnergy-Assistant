"""Plan and tariff models: what each kilowatt-hour costs, and when.

`GET /v1/accounts/{id}` carries the account's plans in the Consumer Data
Right shape. A plan has tariff periods, each covering a span of the calendar
year ("09-11" to "12-31"), and each period has time-of-use rates with the
windows they apply in. On the live account, 1st Energy puts each rate in a
period of its own, both periods covering the same dates, and hangs the
daily supply charge on only one of them.

CDR prices exclude GST, so everything here does too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

# How far ahead `next_change` looks. A week covers every weekly pattern.
_LOOKAHEAD_DAYS = 8


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Part of a day a rate applies in. Both ends inclusive, to the second.

    The API writes an end as "06:59:59" rather than "07:00:00". An end
    before the start wraps past midnight.
    """

    start: time
    end: time
    days: frozenset[str]

    def contains(self, moment: datetime) -> bool:
        clock = moment.time().replace(microsecond=0)
        today = WEEKDAYS[moment.weekday()]
        if self.start <= self.end:
            return today in self.days and self.start <= clock <= self.end
        if clock >= self.start:
            return today in self.days
        # After midnight in a window that began the day before.
        yesterday = WEEKDAYS[(moment.weekday() - 1) % 7]
        return yesterday in self.days and clock <= self.end


@dataclass(frozen=True, slots=True)
class Rate:
    """One time-of-use rate: a price per kWh and when it applies."""

    name: str
    # The CDR type, lower case: "peak", "off_peak", "shoulder" or
    # "solar_sponge". Stable, unlike the display name.
    band: str
    unit_price: Decimal
    windows: tuple[TimeWindow, ...] = ()

    def applies_at(self, moment: datetime) -> bool:
        return any(w.contains(moment) for w in self.windows)


@dataclass(frozen=True, slots=True)
class TariffPeriod:
    """Rates for a span of the year, given as "MM-DD" on both ends."""

    start: str
    end: str
    daily_supply_charge: Decimal | None = None
    rates: tuple[Rate, ...] = ()

    def covers(self, day: date) -> bool:
        key = day.strftime("%m-%d")
        if self.start <= self.end:
            return self.start <= key <= self.end
        return key >= self.start or key <= self.end


@dataclass(frozen=True, slots=True)
class Plan:
    """A retail plan and its tariff, for the dates it is in force."""

    name: str
    start_date: date | None
    end_date: date | None
    pricing_model: str | None = None
    periods: tuple[TariffPeriod, ...] = field(default_factory=tuple)

    def covers(self, day: date) -> bool:
        return ((self.start_date is None or self.start_date <= day)
                and (self.end_date is None or day <= self.end_date))

    def daily_supply_charge(self, day: date) -> Decimal | None:
        """The day's supply charge, or None if the plan doesn't say.

        Taken from the first period covering the day that states one, not
        summed: 1st Energy repeats a date range across periods, and a
        charge repeated on each would otherwise be counted twice.
        """
        if not self.covers(day):
            return None
        for period in self.periods:
            if period.covers(day) and period.daily_supply_charge is not None:
                return period.daily_supply_charge
        return None

    def rate_at(self, moment: datetime) -> Rate | None:
        """The rate in force at a local time, or None if none applies."""
        day = moment.date()
        if not self.covers(day):
            return None
        for period in self.periods:
            if not period.covers(day):
                continue
            for rate in period.rates:
                if rate.applies_at(moment):
                    return rate
        return None

    def next_change(self, moment: datetime) -> datetime | None:
        """When the rate next differs from the one at `moment`, or None.

        Windows are specified to the second and start on the minute, so
        stepping through minutes finds every change.
        """
        current = self.rate_at(moment)
        probe = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = moment + timedelta(days=_LOOKAHEAD_DAYS)
        while probe <= limit:
            if self.rate_at(probe) != current:
                return probe
            probe += timedelta(minutes=1)
        return None


def plan_on(plans: tuple[Plan, ...], day: date) -> Plan | None:
    """The plan in force on a day, if the account has one."""
    return next((p for p in plans if p.covers(day)), None)
