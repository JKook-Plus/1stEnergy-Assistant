"""Constants for the 1st Energy integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "first_energy"

CONF_ACCOUNT_ID: Final = "account_id"
CONF_ACCOUNT_NUMBER: Final = "account_number"
CONF_BACKFILL_DONE: Final = "backfill_complete"
# First day the backfill has not yet imported, as an ISO date. Written after
# every chunk, so a restart resumes rather than starting over.
CONF_BACKFILL_CURSOR: Final = "backfill_next_day"

# Meter data lags roughly a day, so there is nothing to gain from frequent
# polling. Six hours is a compromise: a once-daily poll could add almost another
# 24 hours of latency depending on when the retailer's overnight load lands,
# while four requests a day stays gentle on an undocumented endpoint.
UPDATE_INTERVAL: Final = timedelta(hours=6)

# Days re-requested on every poll. Statistics writes are idempotent on
# timestamp, so overlapping repeatedly is free and repairs any gap left by a
# failed poll or an HA outage without special-case recovery code.
ROLLING_WINDOW_DAYS: Final = 5

# How far back a first-time backfill reaches. It starts at the account's
# creation date when that is known, so this only caps long-standing accounts
# and bounds the walk when the date is missing.
MAX_BACKFILL_DAYS: Final = 365 * 5

# After a failed backfill, skip it for 2^(failures - 1) polls, at most this
# many (a day, at six-hourly polling), and raise a repair issue once it has
# failed this many times in a row.
BACKFILL_MAX_SKIPPED_POLLS: Final = 4
BACKFILL_FAILURES_BEFORE_ISSUE: Final = 5

# Statistic id suffixes. These are permanent: changing one orphans every
# existing user's recorded history.
STAT_ENERGY: Final = "energy"
STAT_COST: Final = "cost"
STAT_ENERGY_EXPORT: Final = "energy_export"


def statistic_id(nmi: str, kind: str) -> str:
    """External statistic id for a meter.

    Keyed on the NMI rather than the retailer's service point id: the NMI
    identifies the physical connection and survives account changes, so history
    stays attached to the meter rather than to a billing record.

    The colon marks this as an external statistic, meaning no entity in Home
    Assistant produces it and the recorder will not try to match one.
    """
    return f"{DOMAIN}:{kind}_{nmi.lower()}"
