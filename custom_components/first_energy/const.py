"""Constants for the 1st Energy integration."""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from typing import Final

DOMAIN: Final = "first_energy"

# 1st Energy bills in Australian dollars only. Not hass.config.currency: a
# Home Assistant set to another currency would label these amounts wrongly.
CURRENCY: Final = "AUD"

CONF_ACCOUNT_ID: Final = "account_id"
CONF_ACCOUNT_NUMBER: Final = "account_number"
CONF_BACKFILL_DONE: Final = "backfill_complete"
# First day the backfill has not yet imported, as an ISO date. Written after
# every chunk, so a restart resumes rather than starting over.
CONF_BACKFILL_CURSOR: Final = "backfill_next_day"

# Option: add GST to every cost and price. The API's prices exclude it, as
# the Consumer Data Right standard requires, while the bill includes it.
# Off by default, which is how the integration has always stored costs.
CONF_INCLUDE_GST: Final = "include_gst"
GST_MULTIPLIER: Final = Decimal("1.1")

# Meter data lags roughly a day, so there is nothing to gain from frequent
# polling. Six hours is a compromise: a once-daily poll could add almost another
# 24 hours of latency depending on when the retailer's overnight load lands,
# while four requests a day stays gentle on an undocumented endpoint.
UPDATE_INTERVAL: Final = timedelta(hours=6)

# Days re-requested on every poll, ending yesterday, inclusive. Statistics
# writes are idempotent on timestamp, so overlapping repeatedly is free and
# repairs any gap left by a failed poll or an HA outage without special-case
# recovery code.
ROLLING_WINDOW_DAYS: Final = 6

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
# The supply charge's share of the cost, beside the per-band costs.
SUPPLY_CHARGE_BAND: Final = "supply_charge"


def band_slug(band: str) -> str:
    """A time-of-use band as the API names it ("Off Peak"), made id-safe."""
    return re.sub(r"[^a-z0-9]+", "_", band.lower()).strip("_")


def band_statistic_id(nmi: str, kind: str, band: str) -> str:
    """External statistic id for one time-of-use band of a meter.

    `energy_off_peak_<nmi>` sits beside `energy_<nmi>` rather than
    replacing it: the bands are a breakdown of the total, not a second
    copy to add to it.
    """
    return statistic_id(nmi, f"{kind}_{band_slug(band)}")


def statistic_id(nmi: str, kind: str) -> str:
    """External statistic id for a meter.

    Keyed on the NMI rather than the retailer's service point id: the NMI
    identifies the physical connection and survives account changes, so history
    stays attached to the meter rather than to a billing record.

    The colon marks this as an external statistic, meaning no entity in Home
    Assistant produces it and the recorder will not try to match one.
    """
    return f"{DOMAIN}:{kind}_{nmi.lower()}"
