#!/usr/bin/env python3
"""Write synthetic API fixtures into dev/fixtures/ for the test suite.

Every value here is invented. Nothing comes from a real account: the
payloads copy the *shape* the parsers in custom_components/first_energy/api/
expect, with a made-up but plausible usage profile. The IDs match the
pseudonyms the tests already use (account 638594, service point 663701).

Sanitised captures from a live account (dev/capture_fixtures.py) test the
parsers against the API's real behaviour, which these cannot. If you have
them, prefer them; these exist so the suite runs without them.

    python dev/make_synthetic_fixtures.py

Deterministic: running it again produces identical files. Standard library
only.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent / "fixtures"

ACCOUNT_ID = "638594"
ACCOUNT_NUMBER = "516645"
SERVICE_POINT_ID = "663701"
NMI = "9999990001"

SLOT_MINUTES = 5
SLOTS = 24 * 60 // SLOT_MINUTES

# A residential time-of-use tariff in local time. Peak 16:00-20:00 matches
# what tests/test_statistics.py expects of an Endeavour residential plan.
RATES = {"Peak": 0.55, "Shoulder": 0.30, "Off Peak": 0.22}


def band(hour: int) -> str:
    if 16 <= hour < 20:
        return "Peak"
    if 7 <= hour < 16 or 20 <= hour < 22:
        return "Shoulder"
    return "Off Peak"


# Daily supply charge, in the same GST-exclusive terms as RATES.
SUPPLY_CHARGE = "1.10000"
ALL_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "PUBLIC_HOLIDAYS"]


def tariff_period(name: str, kind: str, windows: list[tuple[str, str]],
                  supply: str | None = None) -> dict:
    """One rate in a period of its own, the way 1st Energy lays them out."""
    period = {
        "type": "RETAIL_SERVICE",
        "displayName": f"{name} Usage",
        "startDate": "06-26",
        "endDate": "06-25",
        "timeZone": "LOCAL",
        "rateBlockUType": "timeOfUseRates",
        "timeOfUseRates": [{
            "displayName": f"{name} Usage",
            "type": kind,
            "rates": [{"unitPrice": f"{RATES[name]:.7f}", "measureUnit": "KWH", "volume": 0.0}],
            "timeOfUse": [{"startTime": start, "endTime": end, "days": ALL_DAYS}
                          for start, end in windows],
        }],
    }
    if supply is not None:
        period["dailySupplyCharge"] = supply
    return period


def usage_day(day: date) -> dict:
    """One register's 5-minute reads for one day, with matching aggregates."""
    rng = random.Random(day.toordinal())
    reads, costs, tou = [], [], []
    for slot in range(SLOTS):
        hour = slot * SLOT_MINUTES // 60
        # Overnight base load, a morning bump and a larger evening one.
        base = 0.012
        if 6 <= hour < 9:
            base += 0.02
        elif 16 <= hour < 22:
            base += 0.035
        kwh = round(base * rng.uniform(0.6, 1.4), 4)
        name = band(hour)
        reads.append(kwh)
        costs.append(round(kwh * RATES[name], 6))
        tou.append(name)
    return {
        "servicePointId": SERVICE_POINT_ID,
        "registerId": "E1",
        "registerSuffix": "E1",
        "meterId": "M1",
        "controlledLoad": False,
        "readStartDate": day.isoformat(),
        "readEndDate": day.isoformat(),
        "unitOfMeasure": "KWH",
        "readUType": "intervalRead",
        "intervalRead": {
            "readIntervalLength": SLOT_MINUTES,
            "aggregateValue": round(sum(reads), 6),
            "aggregateCosting": round(sum(costs), 6),
            "intervalReads": reads,
            "intervalCostings": costs,
            "intervalTOU": tou,
        },
    }


def usage_day_without_intervals(day: date) -> dict:
    """What `interval-reads=NONE` returns: 288 zero slots and a length of 0."""
    real = usage_day(day)
    return {
        **real,
        "intervalRead": {
            "readIntervalLength": 0,
            "aggregateValue": real["intervalRead"]["aggregateValue"],
            "aggregateCosting": real["intervalRead"]["aggregateCosting"],
            "intervalReads": [0.0] * SLOTS,
        },
    }


def usage_payload(reads: list[dict]) -> dict:
    return {
        "data": {"reads": reads},
        "links": {"self": "/v1/electricity/servicepoints/663701/usage"},
        "meta": {"totalRecords": len(reads), "totalPages": 1},
    }


def days(oldest: date, newest: date) -> list[date]:
    return [oldest + timedelta(days=i) for i in range((newest - oldest).days + 1)]


def fixtures() -> dict[str, dict]:
    end = date(2026, 8, 13)
    return {
        "accounts_electricity": {
            "data": {
                "accounts": [{
                    "accountId": ACCOUNT_ID,
                    "accountNumber": ACCOUNT_NUMBER,
                    "displayName": "<redacted:displayname>",
                    "creationDate": "2026-06-26",
                    "openStatus": "OPEN",
                    "plans": [{
                        "nickname": "Residential Time of Use",
                        "planOverview": {"displayName": "Residential Time of Use"},
                    }],
                    "servicePoints": [{"servicePointId": SERVICE_POINT_ID}],
                }],
            },
        },
        # The gas query returns the same account with no connection on it.
        "accounts_gas": {
            "data": {
                "accounts": [{
                    "accountId": ACCOUNT_ID,
                    "accountNumber": ACCOUNT_NUMBER,
                    "creationDate": "2026-06-26",
                    "openStatus": "OPEN",
                    "plans": [],
                    "servicePoints": [],
                }],
            },
        },
        # The account detail, carrying the plan and its tariff. Matches band():
        # the supply charge sits on one period only, as on the live account.
        "account_detail": {
            "data": {
                "accountId": ACCOUNT_ID,
                "accountNumber": ACCOUNT_NUMBER,
                "displayName": "<redacted:displayname>",
                "openStatus": "OPEN",
                "creationDate": "2026-06-26",
                "plans": [{
                    "planOverview": {
                        "displayName": "Residential Time of Use",
                        "startDate": "2026-06-26",
                        "endDate": "2027-06-25",
                    },
                    "planDetail": {
                        "fuelType": "ELECTRICITY",
                        "isContingentPlan": False,
                        "electricityContract": {
                            "pricingModel": "TIME_OF_USE",
                            "timeZone": "LOCAL",
                            "isFixed": False,
                            "paymentOption": ["PAPER_BILL"],
                            "tariffPeriod": [
                                tariff_period("Off Peak", "OFF_PEAK",
                                              [("00:00:00", "06:59:59"),
                                               ("22:00:00", "23:59:59")]),
                                tariff_period("Shoulder", "SHOULDER",
                                              [("07:00:00", "15:59:59"),
                                               ("20:00:00", "21:59:59")]),
                                tariff_period("Peak", "PEAK", [("16:00:00", "19:59:59")],
                                              supply=SUPPLY_CHARGE),
                            ],
                        },
                    },
                }],
                "servicePoints": [{"servicePointId": SERVICE_POINT_ID}],
            },
        },
        "account_balance": {"data": {"accountId": ACCOUNT_ID, "balance": "151.87"}},
        "account_invoices": {
            "data": {
                "invoices": [{
                    "invoiceNumber": "INV000007",
                    "accountId": ACCOUNT_ID,
                    "issueDate": "2026-08-03",
                    "dueDate": "2026-08-17",
                    "period": {"startDate": "2026-06-29", "endDate": "2026-07-28"},
                    "invoiceAmount": "138.06",
                    "gstAmount": "13.81",
                    "payOnTimeDiscount": {"discountAmount": "5.52236"},
                    "paymentStatus": "NOT_PAID",
                }],
            },
        },
        # A live register beside a removed one, as a meter exchange leaves it.
        "servicepoint": {
            "data": {
                "servicePointId": SERVICE_POINT_ID,
                "nationalMeteringId": NMI,
                "servicePointStatus": "ACTIVE",
                "jurisdictionCode": "NSW",
                "isGenerator": False,
                "distributionLossFactor": {
                    "code": "XX0A",
                    "description": "Low Voltage Urban",
                    "lossValue": "1.0580",
                },
                "relatedParticipants": [
                    {"party": "Example Networks", "role": "LNSP"},
                    {"party": "1st Energy", "role": "FRMP"},
                ],
                "meters": [
                    {
                        "meterId": "M1",
                        "specifications": {
                            "status": "CURRENT",
                            "installationType": "COMMS4D",
                            "readType": "RWDA",
                        },
                        "registers": [{
                            "registerId": "E1",
                            "status": "CURRENT",
                            "unitOfMeasure": "KWH",
                            "controlledLoad": False,
                            "networkTariffCode": "N70",
                            "timeOfDay": "ALLDAY",
                        }],
                    },
                    {
                        "meterId": "M0",
                        "specifications": {"status": "REMOVED"},
                        "registers": [{
                            "registerId": "E0",
                            "status": "REMOVED",
                            "unitOfMeasure": "KWH",
                            "controlledLoad": False,
                            "networkTariffCode": "N70",
                            "timeOfDay": "ALLDAY",
                        }],
                    },
                ],
            },
        },
        "usage_recent_7d": usage_payload(
            [usage_day(d) for d in days(end - timedelta(days=7), end)]),
        "usage_30d": usage_payload(
            [usage_day(d) for d in days(end - timedelta(days=30), end)]),
        "usage_no_intervals": usage_payload(
            [usage_day_without_intervals(d) for d in days(end - timedelta(days=3), end)]),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in fixtures().items():
        # The usage files are thousands of generated numbers; one per line
        # would triple their size for no benefit to a reader.
        if name.startswith("usage_"):
            text = json.dumps(payload, separators=(",", ":"))
        else:
            text = json.dumps(payload, indent=2)
        (OUT / f"{name}.json").write_text(text + "\n")
        print(f"wrote {name}.json")


if __name__ == "__main__":
    main()
