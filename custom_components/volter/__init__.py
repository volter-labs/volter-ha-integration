"""Integracja Volter Energy — telemetria i sterowanie falownikiem via Cloud."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .command_handler import VolterCommandHandler
from .const import CONF_API_KEY, CONF_DEVICE_ID, CONF_SUPABASE_ANON_KEY, CONF_SUPABASE_URL, DOMAIN
from .coordinator import VolterTelemetryCoordinator

_LOGGER = logging.getLogger(__name__)

type VolterConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: VolterConfigEntry) -> bool:
    """Konfiguracja integracji Volter z config entry."""
    api_key = entry.data[CONF_API_KEY]
    device_id = entry.data[CONF_DEVICE_ID]
    supabase_url = entry.data[CONF_SUPABASE_URL]
    anon_key = entry.data[CONF_SUPABASE_ANON_KEY]

    # Telemetry coordinator — zbiera stany encji i wysyła batche co 60s
    coordinator = VolterTelemetryCoordinator(
        hass=hass,
        entry=entry,
        api_key=api_key,
        device_id=device_id,
        supabase_url=supabase_url,
    )

    # Command handler — subskrybuje kanał Realtime i wykonuje service calls
    command_handler = VolterCommandHandler(
        hass=hass,
        entry=entry,
        device_id=device_id,
        supabase_url=supabase_url,
        anon_key=anon_key,
        api_key=api_key,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "command_handler": command_handler,
    }

    # Uruchom coordinator i command handler
    await coordinator.async_start()
    await command_handler.async_start()

    # Reaguj na zmiany w Options Flow (przeładuj encje)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info(
        "Volter Energy integration started — device_id=%s, telemetry=60s, commands=realtime",
        device_id,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VolterConfigEntry) -> bool:
    """Wyładuj integrację Volter."""
    data = hass.data[DOMAIN].pop(entry.entry_id, {})

    coordinator: VolterTelemetryCoordinator | None = data.get("coordinator")
    command_handler: VolterCommandHandler | None = data.get("command_handler")

    if coordinator:
        await coordinator.async_stop()

    if command_handler:
        await command_handler.async_stop()

    _LOGGER.info("Volter Energy integration unloaded")
    return True


async def _async_update_listener(hass: HomeAssistant, entry: VolterConfigEntry) -> None:
    """Przeładuj integrację po zmianie opcji (entity mapping)."""
    _LOGGER.info("Options changed, reloading Volter integration")
    await hass.config_entries.async_reload(entry.entry_id)
