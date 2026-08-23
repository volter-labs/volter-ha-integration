"""Sygnał, że Volter wstrzymał zapisy (I-9)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import VolterEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    dane = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([VolterDegradedBinarySensor(entry, dane["executor"])])


class VolterDegradedBinarySensor(VolterEntity, BinarySensorEntity):
    """`on` = Volter NIE pisze do falownika, bo nie ufa danym (I-9).

    Osobna encja, a nie atrybut, bo to jedyny stan, na który warto zawiesić
    automatyzację albo powiadomienie: plan jest, a mimo to nic się nie dzieje.
    """

    _attr_translation_key = "degraded"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:shield-alert-outline"

    def __init__(self, entry: ConfigEntry, executor) -> None:
        super().__init__(entry, executor, "degraded")

    @property
    def is_on(self) -> bool:
        return (self._executor.diagnostics.get("last") or {}).get("status") == "degraded"
