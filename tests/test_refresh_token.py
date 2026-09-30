"""The refresh token: renewing the access token without resending the password.

The login returns a ten-day refresh token beside the one-day access token.
Exchanging it keeps the session alive indefinitely, so the password goes to
the server once per restart rather than once a day. Every way a refresh can
fail falls back to the password login the client always used.
"""

from __future__ import annotations

import json
from datetime import timedelta

import aiohttp
import pytest
from conftest import load
from fake_api import APP_HEADERS, UNAUTHORIZED, FakeApi, jwt

from custom_components.first_energy.api.client import FirstEnergyClient

# Inside the two-minute refresh margin, so already due for renewal.
EXPIRING = timedelta(seconds=30)
TEN_DAYS = timedelta(days=10)


@pytest.fixture
async def api(socket_enabled):
    fake = FakeApi()
    fake.base_url = await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def client(api):
    async with aiohttp.ClientSession() as session:
        yield FirstEnergyClient(
            session, "user@example.com", "hunter2",
            api_base=api.base_url, portal_base=api.base_url, backfill_delay=0,
        )


def tokens(access_ttl: timedelta, refresh_ttl: timedelta = TEN_DAYS, *, tag: str) -> dict:
    return {
        "access_token": jwt(access_ttl, tag=f"access-{tag}"),
        "refresh_token": jwt(refresh_ttl, tag=f"refresh-{tag}"),
    }


def adaptor_tokens(api: FakeApi) -> list[str]:
    return [r.headers["adaptor-authorization"]
            for r in api.for_endpoint("/v1/energy/accounts")]


@pytest.fixture
def accounts(api):
    api.queue("bff", body=f'"{jwt()}"', always=True)
    api.queue("accounts", payload=load("accounts_electricity"), always=True)


@pytest.mark.usefixtures("accounts")
class TestRefresh:
    async def test_expired_access_token_is_refreshed_not_logged_in_again(self, api, client):
        first = tokens(EXPIRING, tag="login")
        renewed = tokens(timedelta(hours=24), tag="refresh")
        api.queue("login", payload=first)
        api.queue("refresh", payload=renewed)

        await client.async_get_accounts()
        await client.async_get_accounts()

        assert api.count("login") == 1
        assert api.count("refresh") == 1
        assert adaptor_tokens(api) == [first["access_token"], renewed["access_token"]]

    async def test_refresh_sends_the_refresh_token_with_the_bff_bearer(self, api, client):
        first = tokens(EXPIRING, tag="login")
        api.queue("login", payload=first)
        api.queue("refresh", payload=tokens(timedelta(hours=24), tag="refresh"))

        await client.async_get_accounts()
        await client.async_get_accounts()

        sent = api.for_endpoint("/v1/auth/refreshtoken")[0]
        assert json.loads(sent.body) == {"refresh_token": first["refresh_token"]}
        assert sent.headers["authorization"].startswith("Bearer ")
        assert "hunter2" not in sent.body

    async def test_the_rotated_refresh_token_is_used_next_time(self, api, client):
        """Each refresh returns a new refresh token; the old one is spent."""
        api.queue("login", payload=tokens(EXPIRING, tag="login"))
        second = tokens(EXPIRING, tag="second")
        api.queue("refresh", payload=second)
        api.queue("refresh", payload=tokens(timedelta(hours=24), tag="third"))

        for _ in range(3):
            await client.async_get_accounts()

        sent = [json.loads(r.body)["refresh_token"]
                for r in api.for_endpoint("/v1/auth/refreshtoken")]
        assert sent[1] == second["refresh_token"]
        assert api.count("login") == 1

    async def test_a_refused_refresh_falls_back_to_the_password(self, api, client):
        api.queue("login", payload=tokens(EXPIRING, tag="login"))
        api.queue("refresh", status=401, body=UNAUTHORIZED, headers=APP_HEADERS)
        relogin = tokens(timedelta(hours=24), tag="relogin")
        api.queue("login", payload=relogin)

        await client.async_get_accounts()
        await client.async_get_accounts()

        assert api.count("refresh") == 1
        assert api.count("login") == 2
        assert adaptor_tokens(api)[-1] == relogin["access_token"]

    @pytest.mark.parametrize("body", ["", "not json", "[]", '{"access_token": ""}'])
    async def test_an_unusable_refresh_response_falls_back_to_the_password(
        self, api, client, body
    ):
        api.queue("login", payload=tokens(EXPIRING, tag="login"))
        api.queue("refresh", body=body)
        api.queue("login", payload=tokens(timedelta(hours=24), tag="relogin"))

        await client.async_get_accounts()
        await client.async_get_accounts()

        assert api.count("login") == 2

    async def test_an_expired_refresh_token_is_not_tried(self, api, client):
        api.queue("login", payload=tokens(EXPIRING, EXPIRING, tag="login"))
        api.queue("login", payload=tokens(timedelta(hours=24), tag="relogin"))

        await client.async_get_accounts()
        await client.async_get_accounts()

        assert api.count("refresh") == 0
        assert api.count("login") == 2

    async def test_a_login_without_a_refresh_token_still_works(self, api, client):
        """The old behaviour, if the login ever stops returning one."""
        api.queue("login", payload={"access_token": jwt(EXPIRING, tag="a")})
        api.queue("login", payload={"access_token": jwt(timedelta(hours=24), tag="b")})

        await client.async_get_accounts()
        await client.async_get_accounts()

        assert api.count("refresh") == 0
        assert api.count("login") == 2

    async def test_a_request_refused_by_the_application_retries_with_the_password(
        self, api, client
    ):
        """The retry after an application 401 is the last before re-auth.

        Making it with a token refreshed from the one just refused would risk
        asking the user for a password that was never wrong.
        """
        api.queue("login", payload=tokens(timedelta(hours=24), tag="login"))
        api.queue("accounts", status=401, body=UNAUTHORIZED, headers=APP_HEADERS)
        api.queue("login", payload=tokens(timedelta(hours=24), tag="relogin"))

        await client.async_get_accounts()

        assert api.count("refresh") == 0
        assert api.count("login") == 2
