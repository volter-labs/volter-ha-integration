"""Integracja Volter Energy — telemetria i sterowanie falownikiem via Cloud."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .command_handler import VolterCommandHandler
from .const import CONF_API_KEY, CONF_DEVICE_ID, CONF_SUPABASE_ANON_KEY, CONF_SUPABASE_URL, DOMAIN
from .coordinator import VolterTelemetryCoordinator
from .executor import VolterExecutor
from .fetcher import ScheduleFetcher

_LOGGER = logging.getLogger(__name__)

type VolterConfigEntry = ConfigEntry

SERVICE_DIAGNOSE = "diagnose"


async def _async_register_services(hass: HomeAssistant) -> None:
    """Zarejestruj serwisy domeny (raz na instancję HA, nie na config entry)."""
    if hass.services.has_service(DOMAIN, SERVICE_DIAGNOSE):
        return

    async def _handle_diagnose(_call: ServiceCall) -> dict:
        entries = hass.data.get(DOMAIN, {})
        return {
            entry_id: await bundle["executor"].async_diagnose()
            for entry_id, bundle in entries.items()
        }

    hass.services.async_register(
        DOMAIN, SERVICE_DIAGNOSE, _handle_diagnose,
        supports_response=SupportsResponse.ONLY,
    )


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

    # Executor — pętla wykonawcza harmonogramu + jedyna brama zapisu do falownika
    executor = VolterExecutor(hass=hass, entry=entry)

    # Task 16: pobieranie planu z chmury (get-schedule, pull co 5 min) —
    # brakujące ogniwo między planerem a executorem. Musi powstać PO executorze,
    # bo przekazuje mu pobrany plan (`executor.async_set_schedule`).
    fetcher = ScheduleFetcher(
        hass=hass,
        entry=entry,
        supabase_url=supabase_url,
        api_key=api_key,
        executor=executor,
    )

    # Command handler — subskrybuje kanał Realtime i deleguje do executora
    command_handler = VolterCommandHandler(
        hass=hass,
        entry=entry,
        device_id=device_id,
        supabase_url=supabase_url,
        anon_key=anon_key,
        api_key=api_key,
        executor=executor,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "command_handler": command_handler,
        "executor": executor,
        "fetcher": fetcher,
    }

    await _async_register_services(hass)

    # Uruchom coordinator, executor, fetcher (po executorze — patrz wyżej) i command handler
    await coordinator.async_start()
    await executor.async_start()
    await fetcher.async_start()
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
    executor: VolterExecutor | None = data.get("executor")
    fetcher: ScheduleFetcher | None = data.get("fetcher")

    if coordinator:
        await coordinator.async_stop()

    if command_handler:
        await command_handler.async_stop()

    # Task 16 / S-5: odsubskrybuj TIMER pobierania PRZED zatrzymaniem executora —
    # inaczej tick fetchera mógłby jeszcze odpalić się w oknie między odsubskrybowaniem
    # a `executor.async_stop()` i trafić na executor, który już odmawia zapisu (S-5b).
    if fetcher:
        await fetcher.async_stop()

    if executor:
        await executor.async_stop()

    _LOGGER.info("Volter Energy integration unloaded")
    return True


async def _async_update_listener(hass: HomeAssistant, entry: VolterConfigEntry) -> None:
    """Przeładuj integrację po zmianie opcji (entity mapping)."""
    _LOGGER.info("Options changed, reloading Volter integration")
    await hass.config_entries.async_reload(entry.entry_id)
