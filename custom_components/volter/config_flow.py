"""Config flow i Options flow integracji Volter Energy."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithConfigEntry,
)
from homeassistant.helpers import selector

from .const import (
    CLAIM_DEVICE_PATH,
    CONF_API_KEY,
    CONF_DEVICE_ID,
    CONF_SUPABASE_ANON_KEY,
    CONF_SUPABASE_URL,
    DEFAULT_SUPABASE_URL,
    DOMAIN,
    OPT_ENTITY_BATTERY_POWER,
    OPT_ENTITY_CHARGE_LIMIT,
    OPT_ENTITY_DISCHARGE_LIMIT,
    OPT_ENTITY_ECO_MODE_POWER,
    OPT_ENTITY_ECO_MODE_SOC,
    OPT_ENTITY_EMS_MODE,
    OPT_ENTITY_EXPORT_LIMIT,
    OPT_ENTITY_EXPORT_LIMIT_SWITCH,
    OPT_ENTITY_GRID_EXPORT_TOTAL,
    OPT_ENTITY_GRID_IMPORT_TOTAL,
    OPT_ENTITY_GRID_POWER,
    OPT_ENTITY_LOAD_POWER,
    OPT_ENTITY_PV_ENERGY_TOTAL,
    OPT_ENTITY_PV_POWER,
    OPT_ENTITY_SOC,
)

_LOGGER = logging.getLogger(__name__)


class CannotConnect(Exception):
    """Nie można połączyć z API."""


class InvalidAuth(Exception):
    """Nieprawidłowy klucz API."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIG FLOW — konfiguracja początkowa (API key → claim device)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class VolterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow: użytkownik wpisuje API key, claim-device weryfikuje."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Krok 1: wpisanie klucza API."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()

            try:
                claim_data = await self._async_claim_device(api_key)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during claim")
                errors["base"] = "unknown"
            else:
                device_id = claim_data["device_id"]

                # Zapobiegnij duplikatom
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title="Volter Energy",
                    data={
                        CONF_API_KEY: api_key,
                        CONF_DEVICE_ID: device_id,
                        CONF_SUPABASE_URL: claim_data["supabase_url"],
                        CONF_SUPABASE_ANON_KEY: claim_data["supabase_anon_key"],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def _async_claim_device(self, api_key: str) -> dict[str, Any]:
        """Wywołaj claim-device endpoint i zwróć config urządzenia."""
        url = f"{DEFAULT_SUPABASE_URL}{CLAIM_DEVICE_PATH}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"api_key": api_key},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 401:
                        raise InvalidAuth()
                    if resp.status >= 500:
                        raise CannotConnect()
                    if resp.status != 200:
                        body = await resp.text()
                        _LOGGER.error("Claim failed: %s %s", resp.status, body)
                        raise CannotConnect()

                    data = await resp.json()
                    return data

        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("Connection error during claim: %s", err)
            raise CannotConnect() from err

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> VolterOptionsFlow:
        """Zwróć options flow handler."""
        return VolterOptionsFlow(config_entry)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OPTIONS FLOW — mapowanie encji HA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class VolterOptionsFlow(OptionsFlowWithConfigEntry):
    """Options flow: 3-krokowe mapowanie encji."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Inicjalizacja."""
        super().__init__(config_entry)
        self._options: dict[str, Any] = dict(config_entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Krok 1: Monitoring — wymagane encje."""
        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_monitoring_extended()

        sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_ENTITY_SOC,
                        default=self._options.get(OPT_ENTITY_SOC, ""),
                    ): sensor_selector,
                    vol.Required(
                        OPT_ENTITY_PV_POWER,
                        default=self._options.get(OPT_ENTITY_PV_POWER, ""),
                    ): sensor_selector,
                    vol.Required(
                        OPT_ENTITY_GRID_POWER,
                        default=self._options.get(OPT_ENTITY_GRID_POWER, ""),
                    ): sensor_selector,
                }
            ),
        )

    async def async_step_monitoring_extended(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Krok 2: Monitoring rozszerzony — opcjonalne encje."""
        if user_input is not None:
            # Filtruj puste wartości
            self._options.update(
                {k: v for k, v in user_input.items() if v}
            )
            return await self.async_step_control()

        sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )

        return self.async_show_form(
            step_id="monitoring_extended",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        OPT_ENTITY_BATTERY_POWER,
                        default=self._options.get(OPT_ENTITY_BATTERY_POWER, ""),
                    ): sensor_selector,
                    vol.Optional(
                        OPT_ENTITY_LOAD_POWER,
                        default=self._options.get(OPT_ENTITY_LOAD_POWER, ""),
                    ): sensor_selector,
                    vol.Optional(
                        OPT_ENTITY_PV_ENERGY_TOTAL,
                        default=self._options.get(OPT_ENTITY_PV_ENERGY_TOTAL, ""),
                    ): sensor_selector,
                    vol.Optional(
                        OPT_ENTITY_GRID_IMPORT_TOTAL,
                        default=self._options.get(OPT_ENTITY_GRID_IMPORT_TOTAL, ""),
                    ): sensor_selector,
                    vol.Optional(
                        OPT_ENTITY_GRID_EXPORT_TOTAL,
                        default=self._options.get(OPT_ENTITY_GRID_EXPORT_TOTAL, ""),
                    ): sensor_selector,
                }
            ),
        )

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Krok 3: Sterowanie — opcjonalne encje do komend."""
        if user_input is not None:
            self._options.update(
                {k: v for k, v in user_input.items() if v}
            )
            return self.async_create_entry(data=self._options)

        return self.async_show_form(
            step_id="control",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        OPT_ENTITY_EMS_MODE,
                        default=self._options.get(OPT_ENTITY_EMS_MODE, ""),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="select")
                    ),
                    vol.Optional(
                        OPT_ENTITY_CHARGE_LIMIT,
                        default=self._options.get(OPT_ENTITY_CHARGE_LIMIT, ""),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="number")
                    ),
                    vol.Optional(
                        OPT_ENTITY_DISCHARGE_LIMIT,
                        default=self._options.get(OPT_ENTITY_DISCHARGE_LIMIT, ""),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="number")
                    ),
                    vol.Optional(
                        OPT_ENTITY_EXPORT_LIMIT,
                        default=self._options.get(OPT_ENTITY_EXPORT_LIMIT, ""),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="number")
                    ),
                    vol.Optional(
                        OPT_ENTITY_EXPORT_LIMIT_SWITCH,
                        default=self._options.get(OPT_ENTITY_EXPORT_LIMIT_SWITCH, ""),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="switch")
                    ),
                    vol.Optional(
                        OPT_ENTITY_ECO_MODE_POWER,
                        default=self._options.get(OPT_ENTITY_ECO_MODE_POWER, ""),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="number")
                    ),
                    vol.Optional(
                        OPT_ENTITY_ECO_MODE_SOC,
                        default=self._options.get(OPT_ENTITY_ECO_MODE_SOC, ""),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="number")
                    ),
                }
            ),
        )
