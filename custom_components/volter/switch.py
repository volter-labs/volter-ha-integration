"""Wyłącznik sterowania falownikiem — jedyny hamulec dostępny z dashboardu."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import VolterEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    dane = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        VolterControlSwitch(entry, dane["executor"], dane["runtime"])
    ])


class VolterControlSwitch(VolterEntity, SwitchEntity):
    """Jedno kliknięcie zatrzymuje wszystkie zapisy do falownika.

    Dopóki tego nie było, jedynym sposobem zatrzymania Voltera było usunięcie
    mapowania encji albo wyłączenie całej integracji. Przełącznik NIE przeładowuje
    config entry (patrz `runtime.py`), więc nie resetuje ochrony pamięci
    nieulotnej ani budżetu anty-oscylacji.

    Przy wyłączonym sterowaniu integracja dalej liczy plan i pokazuje decyzję —
    odbieramy prawo zapisu, nie widoczność.
    """

    _attr_translation_key = "control_enabled"
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, entry: ConfigEntry, executor, runtime) -> None:
        super().__init__(entry, executor, "control_enabled")
        self._runtime = runtime

    @property
    def is_on(self) -> bool:
        return self._runtime.control_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._runtime.async_set_control_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._runtime.async_set_control_enabled(False)
        self.async_write_ha_state()
