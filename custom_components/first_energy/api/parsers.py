"""Pure functions mapping 1st Energy payloads onto domain objects.

No network, no Home Assistant — every function here takes an already-decoded
JSON payload and returns domain objects, so the whole module is testable
against captured fixtures.

The API is undocumented and inconsistent about types: identifiers arrive as
strings in some payloads, money as decimal strings, energy as floats. Parsers
normalise on the way in so nothing downstream has to care.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any

from ..domain import (
    Account,
    Invoice,
    Meter,
    Plan,
    Rate,
    Register,
    ServicePoint,
    TariffPeriod,
    TimeWindow,
    UsageDay,
)
from .exceptions import FirstEnergyError


class ParseError(FirstEnergyError):
    """A payload did not have the shape we require."""


def _date(value: Any) -> date | None:
    """ISO date, or None. Tolerates the full timestamps some fields carry."""
    if not value:
        return None
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _data(payload: Any, *, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ParseError(f"{what}: expected an object, got {type(payload).__name__}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ParseError(f"{what}: payload has no 'data' object")
    return data


def _object(value: Any) -> dict[str, Any]:
    """An optional nested object; anything else counts as absent."""
    return value if isinstance(value, dict) else {}


def _records(value: Any, *, what: str) -> list[dict[str, Any]]:
    """A list of objects, or ParseError.

    Callers catch FirstEnergyError. A malformed record would otherwise
    surface as a KeyError or AttributeError from deep inside a parser.
    """
    if not isinstance(value, list):
        raise ParseError(f"{what}: expected a list, got {type(value).__name__}")
    for item in value:
        if not isinstance(item, dict):
            raise ParseError(f"{what}: expected objects, got {type(item).__name__}")
    return value


# ---------------------------------------------------------------- accounts

def parse_accounts(payload: Any) -> tuple[Account, ...]:
    """`GET /v1/energy/accounts?fuel-type=ELECTRICITY`.

    An account with no service points is still returned — the gas response on
    the test account is exactly that. Such accounts are parsed rather than
    dropped, so the caller can tell "no gas connection" from "request failed".
    """
    data = _data(payload, what="accounts")
    if not isinstance(data.get("accounts"), list):
        raise ParseError("accounts: 'data.accounts' is not a list")
    accounts = _records(data["accounts"], what="accounts")

    out = []
    for raw in accounts:
        if raw.get("accountId") is None:
            raise ParseError("accounts: an account has no 'accountId'")
        plans = raw.get("plans") or []
        plan_name = None
        if isinstance(plans, list) and plans and isinstance(plans[0], dict):
            plan_name = plans[0].get("nickname") or (
                _object(plans[0].get("planOverview")).get("displayName"))

        sp_ids = tuple(
            str(sp["servicePointId"])
            for sp in _records(raw.get("servicePoints") or [], what="accounts: servicePoints")
            if isinstance(sp, dict) and sp.get("servicePointId") is not None
        )

        out.append(Account(
            account_id=str(raw["accountId"]),
            account_number=str(raw.get("accountNumber", "")),
            open_status=str(raw.get("openStatus", "")),
            creation_date=_date(raw.get("creationDate")),
            plan_name=plan_name,
            service_point_ids=sp_ids,
        ))
    return tuple(out)


def parse_balance(payload: Any) -> Decimal:
    """`GET /v1/accounts/{id}/balance` — a decimal string, kept exact."""
    data = _data(payload, what="balance")
    balance = _decimal(data.get("balance"))
    if balance is None:
        raise ParseError("balance: missing or unparseable 'balance'")
    return balance


def parse_invoices(payload: Any) -> tuple[Invoice, ...]:
    """`GET /v1/accounts/{id}/invoices`, newest first."""
    data = _data(payload, what="invoices")
    if not isinstance(data.get("invoices"), list):
        raise ParseError("invoices: 'data.invoices' is not a list")
    invoices = _records(data["invoices"], what="invoices")

    out = []
    for raw in invoices:
        period = _object(raw.get("period"))
        discount = _object(raw.get("payOnTimeDiscount")).get("discountAmount")
        out.append(Invoice(
            invoice_number=str(raw.get("invoiceNumber", "")),
            issue_date=_date(raw.get("issueDate")),
            due_date=_date(raw.get("dueDate")),
            amount=_decimal(raw.get("invoiceAmount")) or Decimal("0"),
            gst_amount=_decimal(raw.get("gstAmount")) or Decimal("0"),
            payment_status=str(raw.get("paymentStatus", "")),
            period_start=_date(period.get("startDate")),
            period_end=_date(period.get("endDate")),
            pay_on_time_discount=_decimal(discount),
        ))
    out.sort(key=lambda i: (i.issue_date or date.min), reverse=True)
    return tuple(out)


# ------------------------------------------------------------------ plans

def _time(value: Any) -> time | None:
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        return None


def _rate(raw: dict[str, Any]) -> Rate | None:
    """A time-of-use rate, or None when it has no usable price.

    Only the first price step is kept. A stepped rate (a cheaper price
    after some volume) would need the running total for the billing
    period, which nothing here has.
    """
    steps = _records(raw.get("rates") or [], what="plan: rates")
    price = _decimal(steps[0].get("unitPrice")) if steps else None
    if price is None:
        return None
    windows = []
    for window in _records(raw.get("timeOfUse") or [], what="plan: timeOfUse"):
        start, end = _time(window.get("startTime")), _time(window.get("endTime"))
        if start is None or end is None:
            continue
        days = window.get("days")
        windows.append(TimeWindow(
            start=start, end=end,
            days=frozenset(str(d).upper() for d in days) if isinstance(days, list)
            else frozenset(),
        ))
    return Rate(
        name=str(raw.get("displayName") or raw.get("type") or ""),
        band=str(raw.get("type") or "").lower(),
        unit_price=price,
        windows=tuple(windows),
    )


def parse_plans(payload: Any) -> tuple[Plan, ...]:
    """`GET /v1/accounts/{id}`: the account's plans and their tariffs.

    Periods whose rates aren't time-of-use still count, for the supply
    charge they carry; a rate without a price is dropped.
    """
    data = _data(payload, what="account")
    out = []
    for raw in _records(data.get("plans") or [], what="plans"):
        overview = _object(raw.get("planOverview"))
        contract = _object(_object(raw.get("planDetail")).get("electricityContract"))
        periods = []
        for period in _records(contract.get("tariffPeriod") or [],
                               what="plan: tariffPeriod"):
            rates = (_rate(r) for r in _records(period.get("timeOfUseRates") or [],
                                                what="plan: timeOfUseRates"))
            periods.append(TariffPeriod(
                start=str(period.get("startDate") or "01-01"),
                end=str(period.get("endDate") or "12-31"),
                daily_supply_charge=_decimal(period.get("dailySupplyCharge")),
                rates=tuple(r for r in rates if r is not None),
            ))
        out.append(Plan(
            name=str(overview.get("displayName") or raw.get("nickname") or ""),
            start_date=_date(overview.get("startDate")),
            end_date=_date(overview.get("endDate")),
            pricing_model=contract.get("pricingModel"),
            periods=tuple(periods),
        ))
    return tuple(out)


# ----------------------------------------------------------- service point

def parse_service_point(payload: Any) -> ServicePoint:
    """`GET /v1/electricity/servicepoints/{spid}`."""
    data = _data(payload, what="service point")

    meters = []
    for raw_meter in _records(data.get("meters") or [], what="service point: meters"):
        specs = _object(raw_meter.get("specifications"))
        registers = tuple(
            Register(
                register_id=str(r.get("registerId", "")),
                status=str(r.get("status", "")),
                unit_of_measure=str(r.get("unitOfMeasure", "")),
                controlled_load=bool(r.get("controlledLoad", False)),
                network_tariff_code=r.get("networkTariffCode"),
                time_of_day=r.get("timeOfDay"),
                multiplier=_float(r.get("multiplier")),
            )
            for r in _records(raw_meter.get("registers") or [], what="service point: registers")
        )
        meters.append(Meter(
            meter_id=str(raw_meter.get("meterId", "")),
            status=specs.get("status"),
            registers=registers,
            installation_type=specs.get("installationType"),
            read_type=specs.get("readType"),
        ))

    nmi = data.get("nationalMeteringId")
    if not nmi:
        raise ParseError("service point: missing 'nationalMeteringId'")

    participants = data.get("relatedParticipants")
    distributor = next(
        (p.get("party") for p in participants
         if isinstance(p, dict) and str(p.get("role", "")).upper() == "LNSP"),
        None,
    ) if isinstance(participants, list) else None
    loss = _object(data.get("distributionLossFactor"))

    return ServicePoint(
        service_point_id=str(data.get("servicePointId", "")),
        nmi=str(nmi),
        status=str(data.get("servicePointStatus", "")),
        jurisdiction_code=data.get("jurisdictionCode"),
        is_generator=bool(data.get("isGenerator", False)),
        meters=tuple(meters),
        distributor=distributor,
        loss_factor=_float(loss.get("lossValue")),
        loss_factor_code=loss.get("code"),
        loss_factor_description=loss.get("description"),
    )


# ------------------------------------------------------------------ usage

def parse_usage(payload: Any) -> tuple[UsageDay, ...]:
    """`GET /v1/electricity/servicepoints/{spid}/usage`, oldest first.

    With `interval-reads=MIN_30` the response carries three parallel arrays at
    the meter's native resolution — energy, cost and time-of-use band. Their
    lengths are checked here: downstream bucketing zips them, and a mismatch
    would silently attribute cost to the wrong hour rather than fail loudly.

    Days whose slot count disagrees with `readIntervalLength` are NOT rejected.
    That is exactly what a daylight-saving transition looks like, and dropping
    those days would punch a hole in the Energy dashboard twice a year.
    """
    data = _data(payload, what="usage")
    if not isinstance(data.get("reads"), list):
        raise ParseError("usage: 'data.reads' is not a list")
    reads = _records(data["reads"], what="usage")

    out = []
    for raw in reads:
        interval = _object(raw.get("intervalRead"))
        energy = tuple(_float(v) or 0.0 for v in (interval.get("intervalReads") or []))
        cost = tuple(_float(v) or 0.0 for v in (interval.get("intervalCostings") or []))
        tou = tuple(str(v) for v in (interval.get("intervalTOU") or []))

        if cost and len(cost) != len(energy):
            raise ParseError(
                f"usage {raw.get('readStartDate')}: {len(energy)} energy slots "
                f"but {len(cost)} cost slots")
        if tou and len(tou) != len(energy):
            raise ParseError(
                f"usage {raw.get('readStartDate')}: {len(energy)} energy slots "
                f"but {len(tou)} time-of-use slots")

        read_date = _date(raw.get("readStartDate"))
        if read_date is None:
            raise ParseError(f"usage: unparseable readStartDate {raw.get('readStartDate')!r}")

        out.append(UsageDay(
            service_point_id=str(raw.get("servicePointId", "")),
            register_id=str(raw.get("registerId", "")),
            read_date=read_date,
            unit_of_measure=str(raw.get("unitOfMeasure", "kWh")),
            controlled_load=bool(raw.get("controlledLoad", False)),
            interval_minutes=int(interval.get("readIntervalLength") or 0),
            energy_kwh=_float(interval.get("aggregateValue")),
            cost_aud=_float(interval.get("aggregateCosting")),
            intervals=energy,
            costings=cost,
            tou=tou,
        ))

    out.sort(key=lambda u: (u.read_date, u.register_id))
    return tuple(out)
