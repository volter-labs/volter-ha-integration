"""Encje prezentujące plan i stan sterowania w Home Assistancie."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import VolterEntity, plan_do_json


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    dane = hass.data[DOMAIN][entry.entry_id]
    executor = dane["executor"]
    async_add_entities([
        VolterPlanSensor(entry, executor),
        VolterMocSensor(entry, executor),
        VolterPlanDoSensor(entry, executor),
        VolterOstatniZapisSensor(entry, executor),
    ])


class VolterPlanSensor(VolterEntity, SensorEntity):
    """Tryb na teraz + CAŁY plan w atrybutach.

    Plan jedzie w atrybutach, a nie jako osobne encje per godzina, bo to jedna
    rzecz zmieniająca się razem — a karta Lovelace i tak czyta ją w całości.
    """

    _attr_translation_key = "planned_mode"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, entry: ConfigEntry, executor) -> None:
        super().__init__(entry, executor, "planned_mode")

    @property
    def native_value(self) -> str:
        diag = self._executor.diagnostics
        slot = (diag.get("last") or {}).get("params", {})
        plan = self._executor.plan
        if plan is None:
            return "brak planu"
        teraz = datetime.now(timezone.utc)
        biezacy, fallback = plan.effective_slot(teraz)
        from .schedule import akcja_efektywna

        return f"{akcja_efektywna(biezacy).value}{' (fallback)' if fallback else ''}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self._executor.plan
        diag = self._executor.diagnostics
        teraz = datetime.now(timezone.utc)
        atrybuty: dict[str, Any] = {
            "sloty": plan_do_json(plan, teraz),
            "schedule_id": diag.get("schedule_id"),
            "wazny_do": diag.get("schedule_valid_until"),
            "sterowanie_wlaczone": self._executor.sterowanie_wlaczone,
        }
        if plan is not None:
            biezacy, fallback = plan.effective_slot(teraz)
            atrybuty |= {
                "slot_od": biezacy.start.isoformat(),
                "slot_do": biezacy.end.isoformat(),
                "cena": biezacy.price_pln_kwh,
                "moc_w": biezacy.power_w,
                "soc_docelowy": biezacy.soc_target,
                "fallback": fallback,
            }
        return atrybuty


class VolterMocSensor(VolterEntity, SensorEntity):
    """Moc zadana przez plan na bieżącą godzinę."""

    _attr_translation_key = "current_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:flash"

    def __init__(self, entry: ConfigEntry, executor) -> None:
        super().__init__(entry, executor, "current_power")

    @property
    def native_value(self) -> float | None:
        plan = self._executor.plan
        if plan is None:
            return None
        biezacy, _ = plan.effective_slot(datetime.now(timezone.utc))
        return biezacy.power_w


class VolterPlanDoSensor(VolterEntity, SensorEntity):
    """Do kiedy plan jest ważny — po tym urządzenie wchodzi w fallback (I-5)."""

    _attr_translation_key = "schedule_valid_until"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-sand"

    def __init__(self, entry: ConfigEntry, executor) -> None:
        super().__init__(entry, executor, "schedule_valid_until")

    @property
    def native_value(self) -> datetime | None:
        plan = self._executor.plan
        return plan.valid_until if plan is not None else None


class VolterOstatniZapisSensor(VolterEntity, SensorEntity):
    """Status ostatniej decyzji + ślad guardów.

    To jest odpowiedź na „dlaczego nic się nie dzieje" dostępna z dashboardu,
    bez wchodzenia w log HA.
    """

    _attr_translation_key = "last_write"
    _attr_icon = "mdi:content-save-check-outline"

    def __init__(self, entry: ConfigEntry, executor) -> None:
        super().__init__(entry, executor, "last_write")

    @property
    def native_value(self) -> str:
        return (self._executor.diagnostics.get("last") or {}).get("status") or "brak"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ostatni = self._executor.diagnostics.get("last") or {}
        return {
            "zapisano": ostatni.get("executed", []),
            "bledy": ostatni.get("errors", []),
            "noty": ostatni.get("notes", []),
            "zrodlo": ostatni.get("source"),
            "kiedy": ostatni.get("at"),
        }
