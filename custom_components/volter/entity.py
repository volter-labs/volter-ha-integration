"""Wspólna baza encji Volter + serializacja planu dla frontendu.

Wszystkie encje wiszą pod JEDNYM urządzeniem w rejestrze HA, żeby użytkownik
widział „Volter Energy" jako jedno urządzenie z kompletem informacji, a nie
sześć luźnych encji rozsypanych po liście.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, MANUFACTURER
from .executor import VolterExecutor
from .schedule import Schedule, akcja_efektywna


def plan_do_json(plan: Schedule | None, teraz: datetime | None = None) -> list[dict[str, Any]]:
    """Sloty planu w formie, którą rozumie karta Lovelace.

    Świadomie płaska lista prostych typów: atrybuty encji HA są serializowane do
    JSON i renderowane w przeglądarce, więc nie ma tu miejsca na obiekty domenowe.
    `akcja` to kierunek EFEKTYWNY (ten sam, który widzą guardy), a nie dosłowne
    pole `mode` — inaczej karta pokazywałaby co innego niż robi urządzenie.
    """
    if plan is None:
        return []
    teraz = teraz or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for s in plan.slots:
        out.append({
            "od": s.start.isoformat(),
            "do": s.end.isoformat(),
            "akcja": akcja_efektywna(s).value,
            "zrodlo_ladowania": s.charge_source,
            "cel_rozladowania": s.discharge_purpose,
            "moc_w": s.power_w,
            "soc_docelowy": s.soc_target,
            "cena": s.price_pln_kwh,
            "eksport": s.export_allowed,
            "limit_eksportu_w": s.export_limit_w,
            "teraz": s.covers(teraz),
        })
    return out


class VolterEntity(Entity):
    """Baza: wspólne urządzenie i dostęp do executora."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, executor: VolterExecutor, klucz: str) -> None:
        self._entry = entry
        self._executor = executor
        self._attr_unique_id = f"{entry.entry_id}_{klucz}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Volter Energy",
            manufacturer=MANUFACTURER,
            model="EMS",
        )


__all__ = ["VolterEntity", "plan_do_json"]
