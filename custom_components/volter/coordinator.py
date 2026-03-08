"""Telemetry Coordinator — zbiera stany encji i wysyła batche do Supabase."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    DEVICE_TELEMETRY_PATH,
    MONITORING_ENTITY_MAP,
    OPT_ENTITY_EMS_MODE,
    TELEMETRY_BATCH_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class VolterTelemetryCoordinator:
    """Zbiera dane z zmapowanych encji HA i wysyła batche co 60s do Supabase.

    Flow:
    1. Rejestruje listenery na encje z options (entity mapping)
    2. Na każdy state_change zapisuje najnowszą wartość encji
    3. Co 60s kompiluje snapshot i wysyła POST do device-telemetry
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api_key: str,
        device_id: str,
        supabase_url: str,
    ) -> None:
        """Inicjalizacja."""
        self.hass = hass
        self._entry = entry
        self._api_key = api_key
        self._device_id = device_id
        self._telemetry_url = f"{supabase_url}{DEVICE_TELEMETRY_PATH}"

        self._listeners: list[CALLBACK_TYPE] = []
        self._flush_unsub: CALLBACK_TYPE | None = None
        self._latest_values: dict[str, Any] = {}
        self._session: aiohttp.ClientSession | None = None
        self._running = False

    async def async_start(self) -> None:
        """Uruchom coordinator — zarejestruj listenery i timer."""
        self._running = True
        self._session = aiohttp.ClientSession()
        self._setup_state_listeners()
        self._schedule_flush()
        _LOGGER.debug("Telemetry coordinator started")

    async def async_stop(self) -> None:
        """Zatrzymaj coordinator — wyczyść listenery i timer."""
        self._running = False

        for unsub in self._listeners:
            unsub()
        self._listeners.clear()

        if self._flush_unsub is not None:
            self._flush_unsub()
            self._flush_unsub = None

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        _LOGGER.debug("Telemetry coordinator stopped")

    def _setup_state_listeners(self) -> None:
        """Zarejestruj listenery na zmapowane encje monitoringu."""
        options = self._entry.options
        entities_to_track: list[str] = []

        for opt_key in MONITORING_ENTITY_MAP:
            entity_id = options.get(opt_key, "")
            if entity_id:
                entities_to_track.append(entity_id)

        if not entities_to_track:
            _LOGGER.warning("No monitoring entities mapped — telemetry disabled")
            return

        unsub = async_track_state_change_event(
            self.hass,
            entities_to_track,
            self._async_on_state_change,
        )
        self._listeners.append(unsub)

        _LOGGER.info("Tracking %d entities for telemetry", len(entities_to_track))

    @callback
    def _async_on_state_change(self, event: Event[EventStateChangedData]) -> None:
        """Zapisz najnowszą wartość zmienionej encji."""
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]

        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return

        # Znajdź klucz telemetrii dla tej encji
        options = self._entry.options
        for opt_key, telemetry_key in MONITORING_ENTITY_MAP.items():
            if options.get(opt_key) == entity_id:
                try:
                    self._latest_values[telemetry_key] = float(new_state.state)
                except (ValueError, TypeError):
                    # EMS mode lub inne non-numeric
                    self._latest_values[telemetry_key] = new_state.state
                break

    def _schedule_flush(self) -> None:
        """Zaplanuj następny flush za TELEMETRY_BATCH_INTERVAL sekund."""
        if not self._running:
            return

        @callback
        def _flush_callback(_now: Any) -> None:
            self._flush_unsub = None
            if self._running:
                self.hass.async_create_task(self._async_flush())
                self._schedule_flush()

        self._flush_unsub = async_call_later(
            self.hass,
            TELEMETRY_BATCH_INTERVAL,
            _flush_callback,
        )

    async def _async_flush(self) -> None:
        """Wyślij aktualny snapshot telemetrii do Supabase."""
        if not self._latest_values:
            return

        reading = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **self._latest_values,
        }

        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

        try:
            async with self._session.post(
                self._telemetry_url,
                json={"readings": [reading]},
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self._api_key,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    _LOGGER.warning(
                        "Telemetry POST failed: %s %s", resp.status, body
                    )
                else:
                    _LOGGER.debug(
                        "Telemetry sent: %d values", len(self._latest_values)
                    )
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Telemetry POST error: %s", err)
