"""Polling coordinator and historical backfill."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiError, AuthenticationError, FirstEnergyClient, FirstEnergyError
from .const import (
    BACKFILL_FAILURES_BEFORE_ISSUE,
    BACKFILL_MAX_SKIPPED_POLLS,
    CONF_BACKFILL_CURSOR,
    CONF_BACKFILL_DONE,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    ROLLING_WINDOW_DAYS,
    UPDATE_INTERVAL,
)
from .domain import Account, Invoice, ServicePoint, UsageDay
from .services.statistics import bucket_hourly, combine_registers
from .statistics import async_import_buckets

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FirstEnergyData:
    """What the sensors read. Statistics go to the recorder, not here."""

    account: Account
    service_point: ServicePoint
    balance: Decimal | None = None
    invoices: tuple[Invoice, ...] = ()
    last_read_date: date | None = None
    hours_imported: int = 0

    @property
    def next_invoice(self) -> Invoice | None:
        unpaid = [i for i in self.invoices if not i.is_paid and i.due_date]
        return min(unpaid, key=lambda i: i.due_date or date.max) if unpaid else None


class FirstEnergyCoordinator(DataUpdateCoordinator[FirstEnergyData]):
    """Polls a rolling window and keeps the Energy dashboard fed."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: FirstEnergyClient,
        account: Account,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {account.account_number}",
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        self.account = account
        self._service_point: ServicePoint | None = None
        self._backfill_task: asyncio.Task[None] | None = None
        self._backfill_failures = 0
        self._backfill_polls_to_skip = 0
        # The rolling poll and the background backfill both write the same
        # statistics, and each write reads the stored total it continues
        # from. Interleaved, one would read a total the other is about to
        # rewrite.
        self._import_lock = asyncio.Lock()

    async def _async_setup(self) -> None:
        """One-off discovery, run before the first refresh."""
        if not self.account.service_point_ids:
            # Permanent: retrying setup would only fail the same way.
            raise ConfigEntryError(
                f"Account {self.account.account_number} has no electricity connection")
        if len(self.account.service_point_ids) > 1:
            _LOGGER.warning(
                "Account %s has %d electricity connections; only the first (%s) is "
                "imported",
                self.account.account_number,
                len(self.account.service_point_ids),
                self.account.service_point_ids[0],
            )
        self._service_point = await self._call(
            self.client.async_get_service_point(self.account.service_point_ids[0])
        )
        _LOGGER.debug(
            "Discovered NMI %s in %s (%s)",
            self._service_point.nmi,
            self._service_point.jurisdiction_code,
            self._service_point.timezone_name,
        )

    async def _async_update_data(self) -> FirstEnergyData:
        service_point = self._service_point
        assert service_point is not None  # _async_setup guarantees this

        balance = await self._call(self.client.async_get_balance(self.account.account_id))
        invoices = await self._call(self.client.async_get_invoices(self.account.account_id))

        # Meter data lags about a day, so "today" is never available. Ask for a
        # rolling window rather than a single day: re-importing hours we already
        # hold is free, and it silently repairs anything a failed poll missed.
        newest = dt_util.now().date() - timedelta(days=1)
        oldest = newest - timedelta(days=ROLLING_WINDOW_DAYS - 1)
        days = await self._call(
            self.client.async_get_usage(service_point.service_point_id, oldest, newest)
        )

        if self.hass.is_running:
            async with self._import_lock:
                hours = await self._async_import(service_point, days)
        else:
            self._import_after_start(service_point, days)
            hours = 0

        self._maybe_start_backfill(service_point)

        return FirstEnergyData(
            account=self.account,
            service_point=service_point,
            balance=balance,
            invoices=invoices,
            last_read_date=days[-1].read_date if days else None,
            hours_imported=hours,
        )

    def _import_after_start(
        self, service_point: ServicePoint, days: Sequence[UsageDay]
    ) -> None:
        """Import once Home Assistant has started, not during setup.

        The recorder holds its queue until startup finishes, and an import
        waits for the recorder to commit. Run from setup, that wait lasts
        until Home Assistant gives up on the integration and cancels it.
        """
        async def _import(_hass: HomeAssistant) -> None:
            async with self._import_lock:
                await self._async_import(service_point, days)

        self.config_entry.async_on_unload(async_at_started(self.hass, _import))

    async def _async_import(
        self, service_point: ServicePoint, days: Sequence[UsageDay]
    ) -> int:
        """Bucket the consumption registers, sum them per hour, and write.

        A removed register can still report reads for the period it was
        live, a meter can carry a controlled-load register beside the
        general one, and a smart meter also reports reactive energy. Only
        CURRENT import registers count, and they are summed into the
        meter's single series.
        """
        if not days:
            return 0
        tz = ZoneInfo(service_point.timezone_name)
        active = {r.register_id for r in service_point.consumption_registers}
        if not active:
            # Nothing to filter on, rather than a meter with no live
            # register: dropping every read would empty the dashboard.
            _LOGGER.warning(
                "No CURRENT consumption register listed for NMI %s; "
                "importing every register",
                service_point.nmi,
            )
        result = bucket_hourly(days, tz, registers=active or None)
        for warning in result.warnings:
            _LOGGER.info("Interval count anomaly: %s", warning)
        return await async_import_buckets(
            self.hass,
            service_point.nmi,
            combine_registers(result.buckets),
            display_name=f"1st Energy {service_point.nmi}",
        )

    def _maybe_start_backfill(self, service_point: ServicePoint) -> None:
        """Start the history backfill unless it is done, running or backing off."""
        if self.config_entry.data.get(CONF_BACKFILL_DONE):
            return
        if self._backfill_task is not None and not self._backfill_task.done():
            return
        if self._backfill_polls_to_skip > 0:
            self._backfill_polls_to_skip -= 1
            return
        self._backfill_task = self.config_entry.async_create_background_task(
            self.hass, self._async_backfill(service_point), f"{DOMAIN}_backfill"
        )

    def _history_start(self, newest: date) -> date:
        """First day worth asking for: the account's creation, within the cap."""
        floor = newest - timedelta(days=MAX_BACKFILL_DAYS)
        created = self.account.creation_date
        return max(created, floor) if created else floor

    async def _async_backfill(self, service_point: ServicePoint) -> None:
        """Walk history oldest first, once, in the background.

        Deliberately not part of `_async_update_data`. A full history walk is
        many sequential requests against a rate-limit-shy endpoint; running it
        inside the update would block setup past Home Assistant's timeout and
        leave the integration looking broken while it worked perfectly.

        Each chunk is written as it arrives and the next unfetched day is
        saved, so a failure keeps what was already imported and the next
        attempt resumes where this one stopped.
        """
        newest = dt_util.now().date() - timedelta(days=1)
        oldest = self._history_start(newest)
        if cursor := self.config_entry.data.get(CONF_BACKFILL_CURSOR):
            oldest = max(oldest, date.fromisoformat(cursor))
        _LOGGER.info(
            "Backfilling history for NMI %s from %s", service_point.nmi, oldest
        )

        hours = 0
        try:
            async for _, window_end, days in self.client.async_iter_usage_range(
                service_point.service_point_id, oldest, newest
            ):
                async with self._import_lock:
                    hours += await self._async_import(service_point, days)
                self._update_entry_data(
                    {CONF_BACKFILL_CURSOR: (window_end + timedelta(days=1)).isoformat()}
                )
        except asyncio.CancelledError:
            raise
        except AuthenticationError:
            # Nothing here can raise into the coordinator's own error
            # handling, so the re-auth flow has to be started directly.
            _LOGGER.warning("Backfill for %s needs the password again", service_point.nmi)
            self.config_entry.async_start_reauth(self.hass)
            return
        except Exception as err:  # any failure is retried on a later poll
            self._backfill_failed(service_point, err)
            return

        self._backfill_failures = 0
        ir.async_delete_issue(self.hass, DOMAIN, self._backfill_issue_id)
        self._update_entry_data({CONF_BACKFILL_DONE: True}, remove=(CONF_BACKFILL_CURSOR,))
        _LOGGER.info(
            "Backfill complete for NMI %s: %d hourly buckets", service_point.nmi, hours
        )

    @property
    def _backfill_issue_id(self) -> str:
        return f"backfill_failing_{self.config_entry.entry_id}"

    def _backfill_failed(self, service_point: ServicePoint, err: Exception) -> None:
        """Back off, and tell the user once it is clearly not going away.

        Not fatal: the rolling window keeps the recent days current, and the
        next attempt resumes from the saved cursor rather than the start.
        """
        self._backfill_failures += 1
        self._backfill_polls_to_skip = min(
            2 ** (self._backfill_failures - 1), BACKFILL_MAX_SKIPPED_POLLS
        )
        _LOGGER.warning(
            "Backfill for %s did not complete (attempt %d), retrying in %d polls: %s",
            service_point.nmi, self._backfill_failures, self._backfill_polls_to_skip + 1, err,
        )
        if self._backfill_failures >= BACKFILL_FAILURES_BEFORE_ISSUE:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._backfill_issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="backfill_failing",
                translation_placeholders={
                    "nmi": service_point.nmi,
                    "attempts": str(self._backfill_failures),
                    "error": str(err) or type(err).__name__,
                },
            )

    def _update_entry_data(
        self, values: dict[str, Any], *, remove: tuple[str, ...] = ()
    ) -> None:
        data = {**self.config_entry.data, **values}
        for key in remove:
            data.pop(key, None)
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)

    async def _call[T](self, awaitable: Awaitable[T]) -> T:
        """Translate client errors into the outcomes Home Assistant expects."""
        try:
            return await awaitable
        except AuthenticationError as err:
            # Only raised after a retry with freshly minted tokens also failed,
            # so this really is the password rather than an expired session.
            raise ConfigEntryAuthFailed(
                "1st Energy rejected the stored credentials") from err
        except ApiError as err:
            raise UpdateFailed(f"1st Energy API returned HTTP {err.status}") from err
        except FirstEnergyError as err:
            raise UpdateFailed(str(err)) from err
