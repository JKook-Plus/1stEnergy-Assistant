"""Fixture loading for the parser and statistics tests.

The JSON files under `dev/fixtures/` are synthetic, written by
`dev/make_synthetic_fixtures.py` in the shape the parsers expect. They keep
the suite runnable, but a hand-written fixture only ever proves the parser
agrees with whoever wrote the fixture: sanitised captures from a live
account (`dev/capture_fixtures.py`) are the stronger test, and can replace
these files directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "dev" / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def accounts_payload() -> dict:
    return load("accounts_electricity")


@pytest.fixture
def gas_payload() -> dict:
    return load("accounts_gas")


@pytest.fixture
def service_point_payload() -> dict:
    return load("servicepoint")


@pytest.fixture
def balance_payload() -> dict:
    return load("account_balance")


@pytest.fixture
def invoices_payload() -> dict:
    return load("account_invoices")


@pytest.fixture
def usage_7d_payload() -> dict:
    return load("usage_recent_7d")


@pytest.fixture
def usage_30d_payload() -> dict:
    return load("usage_30d")


@pytest.fixture
def usage_no_intervals_payload() -> dict:
    return load("usage_no_intervals")
