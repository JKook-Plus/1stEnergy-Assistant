"""Config and reauthentication flows."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, AuthenticationError, FirstEnergyClient, FirstEnergyError
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_NUMBER,
    CONF_BACKFILL_CURSOR,
    CONF_BACKFILL_DONE,
    CONF_INCLUDE_GST,
    DOMAIN,
)
from .domain import Account

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
})

OPTIONS_SCHEMA = vol.Schema({
    vol.Required(CONF_INCLUDE_GST, default=False): bool,
})


class FirstEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect credentials, then one entry per electricity account."""

    VERSION = 1
    # 2: re-run the history backfill once, to rewrite series that summed a
    # removed register or wrote two rows per hour for a two-register meter.
    # 3: the balance sensor's unit went from "$" to "AUD".
    # 4: re-run the backfill, to fill days lost to short usage pages and drop
    # reactive-energy registers from consumption.
    # 5: re-run the backfill, to add the daily supply charge to cost.
    # 6: re-run the backfill, to write the per-band series for past days.
    MINOR_VERSION = 6

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FirstEnergyOptionsFlow()

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._accounts: tuple[Account, ...] = ()

    async def _async_fetch_accounts(self, username: str, password: str) -> tuple[Account, ...]:
        client = FirstEnergyClient(
            async_get_clientsession(self.hass), username, password
        )
        await client.async_validate_credentials()
        return await client.async_get_accounts()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                accounts = await self._async_fetch_accounts(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except (ApiError, FirstEnergyError):
                # Includes edge refusals, which look identical to a bad password
                # in the response body but are a transport problem, not a
                # credential one.
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during 1st Energy setup")
                errors["base"] = "unknown"
            else:
                usable = [a for a in accounts if a.service_point_ids]
                if not usable:
                    errors["base"] = "no_accounts"
                else:
                    self._username = user_input[CONF_USERNAME]
                    self._password = user_input[CONF_PASSWORD]
                    self._accounts = tuple(usable)
                    if len(usable) == 1:
                        return await self._async_create(usable[0])
                    return await self.async_step_account()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which account to add when the login covers several."""
        if user_input is not None:
            chosen = next(
                a for a in self._accounts
                if a.account_id == user_input[CONF_ACCOUNT_ID]
            )
            return await self._async_create(chosen)

        options = {
            a.account_id: f"{a.account_number} — {a.plan_name or 'Electricity'}"
            for a in self._accounts
        }
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema({vol.Required(CONF_ACCOUNT_ID): vol.In(options)}),
        )

    async def _async_create(self, account: Account) -> ConfigFlowResult:
        # Keyed on the account id so the same account cannot be added twice,
        # while a second account on the same login still gets its own entry.
        await self.async_set_unique_id(account.account_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"1st Energy {account.account_number}",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_ACCOUNT_ID: account.account_id,
                CONF_ACCOUNT_NUMBER: account.account_number,
            },
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enter the password after a genuine credential rejection.

        Reached only when a retry with freshly issued tokens was also refused —
        an expired session alone never gets here.
        """
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            username = entry.data[CONF_USERNAME]
            try:
                await self._async_fetch_accounts(username, user_input[CONF_PASSWORD])
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except (ApiError, FirstEnergyError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )


class FirstEnergyOptionsFlow(OptionsFlowWithReload):
    """Whether costs include GST. Saving reloads the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            entry = self.config_entry
            if user_input[CONF_INCLUDE_GST] != entry.options.get(CONF_INCLUDE_GST, False):
                # Every stored cost is on the old basis. Run the history
                # import again, from the start, to rewrite it on the new one.
                data = {**entry.data}
                data.pop(CONF_BACKFILL_DONE, None)
                data.pop(CONF_BACKFILL_CURSOR, None)
                self.hass.config_entries.async_update_entry(entry, data=data)
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options),
        )
