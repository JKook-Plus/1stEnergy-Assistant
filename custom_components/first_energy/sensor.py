"""Sensors for the parts of an account that genuinely are current state.

Consumption deliberately does not appear here. It is a day old and belongs in
long-term statistics with historical timestamps — publishing it as a sensor
state would file yesterday's kilowatt-hours under today. See `statistics.py`.

What remains is account-level information that really is current: the balance,
the next invoice, how far the meter data has actually reached, and the tariff
in force right now.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import FirstEnergyConfigEntry
from .const import CURRENCY, DOMAIN, GST_MULTIPLIER
from .coordinator import FirstEnergyCoordinator, FirstEnergyData
from .domain import Plan, Rate, plan_on

# The CDR's time-of-use rate types, lower-cased.
TOU_BANDS = ["peak", "off_peak", "shoulder", "solar_sponge"]


@dataclass(frozen=True, kw_only=True)
class FirstEnergySensorDescription(SensorEntityDescription):
    value: Callable[[FirstEnergyData], Decimal | date | float | str | None]
    attributes: Callable[[FirstEnergyData], dict[str, Any]] | None = None


SENSORS: tuple[FirstEnergySensorDescription, ...] = (
    FirstEnergySensorDescription(
        key="balance",
        translation_key="balance",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY,
        value=lambda data: data.balance,
    ),
    FirstEnergySensorDescription(
        key="next_invoice_amount",
        translation_key="next_invoice_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        value=lambda data: inv.amount if (inv := data.next_invoice) else None,
    ),
    FirstEnergySensorDescription(
        key="next_invoice_due",
        translation_key="next_invoice_due",
        device_class=SensorDeviceClass.DATE,
        value=lambda data: inv.due_date if (inv := data.next_invoice) else None,
    ),
    FirstEnergySensorDescription(
        key="last_read_date",
        translation_key="last_read_date",
        device_class=SensorDeviceClass.DATE,
        # Worth surfacing: it makes the roughly one-day lag visible, so an
        # empty-looking Energy dashboard can be recognised as normal rather
        # than as a broken integration.
        value=lambda data: data.last_read_date,
    ),
)


# Facts about the connection and meter. They rarely change, but they are
# what a distributor or retailer asks for, and the tariff code decides the
# network charges.
DIAGNOSTIC_SENSORS: tuple[FirstEnergySensorDescription, ...] = (
    FirstEnergySensorDescription(
        key="nmi",
        translation_key="nmi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda data: data.service_point.nmi,
    ),
    FirstEnergySensorDescription(
        key="distributor",
        translation_key="distributor",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda data: data.service_point.distributor,
    ),
    FirstEnergySensorDescription(
        key="network_tariff",
        translation_key="network_tariff",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda data: data.service_point.network_tariff_code,
    ),
    FirstEnergySensorDescription(
        key="loss_factor",
        translation_key="loss_factor",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=4,
        value=lambda data: data.service_point.loss_factor,
        attributes=lambda data: {
            "code": data.service_point.loss_factor_code,
            "description": data.service_point.loss_factor_description,
        },
    ),
    FirstEnergySensorDescription(
        key="meter",
        translation_key="meter",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda data: m.meter_id if (m := data.service_point.consumption_meter) else None,
        attributes=lambda data: {
            "installation_type": m.installation_type if (
                m := data.service_point.consumption_meter) else None,
            "read_type": m.read_type if m else None,
            "registers": [r.register_id for r in data.service_point.active_registers],
        },
    ),
)


@dataclass(frozen=True, slots=True)
class Tariff:
    """The plan and rate in force at a moment, as the sensors show them."""

    plan: Plan | None
    rate: Rate | None
    includes_gst: bool

    @property
    def price(self) -> Decimal | None:
        """The rate's unit price, with GST added if the option is on."""
        if self.rate is None:
            return None
        if self.includes_gst:
            return self.rate.unit_price * GST_MULTIPLIER
        return self.rate.unit_price


@dataclass(frozen=True, kw_only=True)
class FirstEnergyTariffSensorDescription(SensorEntityDescription):
    """A sensor read from the plan at the current local time."""

    value: Callable[[Tariff], Decimal | date | str | None]
    attributes: Callable[[Tariff], dict[str, Any]] = lambda tariff: {}


def _current_plan(plans: tuple[Plan, ...], today: date) -> Plan | None:
    """The plan in force today, or failing that the most recent one."""
    if plan := plan_on(plans, today):
        return plan
    dated = [p for p in plans if p.start_date is not None and p.start_date <= today]
    return max(dated, key=lambda p: p.start_date or date.min) if dated else None


TARIFF_SENSORS: tuple[FirstEnergyTariffSensorDescription, ...] = (
    FirstEnergyTariffSensorDescription(
        key="current_price",
        translation_key="current_price",
        # Not MONETARY: that device class takes a bare currency, and a
        # price per kWh is what the Energy dashboard's "use an entity with
        # the current price" option expects.
        native_unit_of_measurement=f"{CURRENCY}/{UnitOfEnergy.KILO_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value=lambda tariff: tariff.price,
        attributes=lambda tariff: {
            "rate": tariff.rate.name if tariff.rate else None,
            "includes_gst": tariff.includes_gst,
        },
    ),
    FirstEnergyTariffSensorDescription(
        key="current_period",
        translation_key="current_period",
        device_class=SensorDeviceClass.ENUM,
        options=TOU_BANDS,
        value=lambda tariff: (
            tariff.rate.band if tariff.rate and tariff.rate.band in TOU_BANDS else None),
    ),
    FirstEnergyTariffSensorDescription(
        key="plan_end",
        translation_key="plan_end",
        device_class=SensorDeviceClass.DATE,
        value=lambda tariff: tariff.plan.end_date if tariff.plan else None,
        attributes=lambda tariff: {
            "plan": tariff.plan.name if tariff.plan else None,
            "start_date": (tariff.plan.start_date.isoformat()
                           if tariff.plan and tariff.plan.start_date else None),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FirstEnergyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        FirstEnergySensor(coordinator, description)
        for description in (*SENSORS, *DIAGNOSTIC_SENSORS)
    )
    async_add_entities(
        FirstEnergyTariffSensor(coordinator, description) for description in TARIFF_SENSORS
    )


def _device_info(coordinator: FirstEnergyCoordinator) -> DeviceInfo:
    account = coordinator.account
    return DeviceInfo(
        identifiers={(DOMAIN, account.account_id)},
        name=f"1st Energy {account.account_number}",
        manufacturer="1st Energy",
        model=account.plan_name or "Electricity",
        configuration_url="https://myaccount.1stenergy.com.au",
    )


class FirstEnergySensor(CoordinatorEntity[FirstEnergyCoordinator], SensorEntity):
    entity_description: FirstEnergySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FirstEnergyCoordinator,
        description: FirstEnergySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.account.account_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self) -> Decimal | date | float | str | None:
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.coordinator.data)


class FirstEnergyTariffSensor(CoordinatorEntity[FirstEnergyCoordinator], SensorEntity):
    """The tariff at this moment, updated as the clock crosses into a new rate.

    Rather than ticking every minute, each update schedules the next one
    for when the rate next changes, or local midnight, whichever is
    sooner; midnight is when a plan starts or ends. The plan's times are
    the meter's local time, which isn't necessarily Home Assistant's.
    """

    entity_description: FirstEnergyTariffSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FirstEnergyCoordinator,
        description: FirstEnergyTariffSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.account.account_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)
        self._unsub_change: CALLBACK_TYPE | None = None

    def _now(self) -> datetime:
        tz = ZoneInfo(self.coordinator.data.service_point.timezone_name)
        return dt_util.now(tz)

    def _current(self) -> Tariff:
        now = self._now()
        plan = _current_plan(self.coordinator.data.plans, now.date())
        return Tariff(plan, plan.rate_at(now) if plan else None, self.coordinator.include_gst)

    @property
    def native_value(self) -> Decimal | date | str | None:
        return self.entity_description.value(self._current())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attributes(self._current())

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_change)
        self._schedule_change()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_change()
        super()._handle_coordinator_update()

    @callback
    def _cancel_change(self) -> None:
        if self._unsub_change is not None:
            self._unsub_change()
            self._unsub_change = None

    @callback
    def _schedule_change(self) -> None:
        self._cancel_change()
        now = self._now()
        midnight = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
        plan = plan_on(self.coordinator.data.plans, now.date())
        change = plan.next_change(now) if plan else None
        self._unsub_change = async_track_point_in_time(
            self.hass, self._changed, min(change, midnight) if change else midnight)

    @callback
    def _changed(self, _now: datetime) -> None:
        self._unsub_change = None
        self._schedule_change()
        self.async_write_ha_state()
