"""Stan sterowany przez użytkownika z poziomu HA, trwały między restartami.

Powód istnienia: wyłącznik sterowania musi dać się przełączyć JEDNYM kliknięciem
w encji, a nie przejściem całego Options Flow. Trzymanie go wyłącznie w opcjach
config entry wymuszałoby `async_reload` przy każdym przełączeniu — czyli pełny
reset ochrony NVM i budżetu anty-oscylacji (ustalenie S-7) tylko po to, żeby
zmienić jeden bool.

Opcja z Options Flow zostaje WARTOŚCIĄ POCZĄTKOWĄ: instalacja, która nigdy nie
dotknęła przełącznika, dziedziczy to, co właściciel ustawił w konfiguracji.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_CONTROL_ENABLED,
    OPT_CONTROL_ENABLED,
    RUNTIME_STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class VolterRuntime:
    """Stan przełączalny z UI. Jedno źródło prawdy dla toru zapisu."""

    def __init__(self, hass: HomeAssistant, entry_id: str, opcje: dict[str, Any]) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"{RUNTIME_STORAGE_KEY}.{entry_id}")
        self._opcje = dict(opcje)
        self._control_enabled: bool | None = None

    async def async_load(self) -> None:
        """Wczytaj stan; przy pierwszym uruchomieniu weź wartość z opcji."""
        dane = await self._store.async_load()
        if isinstance(dane, dict) and "control_enabled" in dane:
            self._control_enabled = bool(dane["control_enabled"])
        else:
            self._control_enabled = bool(
                self._opcje.get(OPT_CONTROL_ENABLED, DEFAULT_CONTROL_ENABLED)
            )
        _LOGGER.info(
            "Sterowanie falownikiem: %s",
            "WŁĄCZONE" if self._control_enabled else "wyłączone",
        )

    @property
    def control_enabled(self) -> bool:
        if self._control_enabled is None:
            # Przed `async_load` — zawsze bezpiecznie, nigdy nie zgadujemy na TAK.
            return bool(self._opcje.get(OPT_CONTROL_ENABLED, DEFAULT_CONTROL_ENABLED))
        return self._control_enabled

    async def async_set_control_enabled(self, wlaczone: bool) -> None:
        self._control_enabled = bool(wlaczone)
        await self._store.async_save({"control_enabled": self._control_enabled})
        _LOGGER.warning(
            "Sterowanie falownikiem %s przez użytkownika",
            "WŁĄCZONE" if wlaczone else "WYŁĄCZONE",
        )


__all__ = ["VolterRuntime"]
