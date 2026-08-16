"""Odczyt stanu instalacji i granic sprzętowych z encji Home Assistanta.

Cała wiedza o HA jest tutaj, żeby `guards.py` i `schedule.py` zostały czyste
i testowalne na hoście (a docelowo przepisywalne na C).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant, State

from .const import (
    OPT_ENTITY_BATTERY_POWER,
    OPT_ENTITY_EMS_MODE,
    OPT_ENTITY_GRID_POWER,
    OPT_ENTITY_PV_POWER,
    OPT_ENTITY_SOC,
)
from .guards import DeviceState, InverterLimits

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = ("unknown", "unavailable", "none", "")


def _num(state: State | None) -> float | None:
    if state is None or str(state.state).lower() in _UNAVAILABLE:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _age_s(state: State | None, now: datetime) -> float | None:
    if state is None:
        return None
    return max(0.0, (now - state.last_updated).total_seconds())


def read_device_state(
    hass: HomeAssistant,
    options: dict,
    previous_soc: float | None = None,
) -> DeviceState:
    """Zbierz migawkę stanu z zmapowanych encji monitoringu.

    `age_s` to wiek NAJSTARSZEGO istotnego odczytu — świadomie pesymistycznie,
    bo guard I-9 ma chronić przed działaniem na nieaktualnym obrazie instalacji.
    """
    now = datetime.now(timezone.utc)

    soc_state = hass.states.get(options.get(OPT_ENTITY_SOC, "")) if options.get(OPT_ENTITY_SOC) else None
    pv_state = hass.states.get(options.get(OPT_ENTITY_PV_POWER, "")) if options.get(OPT_ENTITY_PV_POWER) else None
    grid_state = hass.states.get(options.get(OPT_ENTITY_GRID_POWER, "")) if options.get(OPT_ENTITY_GRID_POWER) else None
    batt_state = (
        hass.states.get(options.get(OPT_ENTITY_BATTERY_POWER, ""))
        if options.get(OPT_ENTITY_BATTERY_POWER)
        else None
    )

    ages = [a for a in (_age_s(soc_state, now), _age_s(pv_state, now), _age_s(grid_state, now)) if a is not None]

    return DeviceState(
        soc=_num(soc_state),
        battery_power_w=_num(batt_state),
        pv_power_w=_num(pv_state),
        grid_power_w=_num(grid_state),
        age_s=max(ages) if ages else float("inf"),
        previous_soc=previous_soc,
    )


def read_inverter_limits(hass: HomeAssistant, options: dict) -> InverterLimits:
    """Odczytaj to, co da się odczytać z HA.

    `allowed_modes` bierzemy z atrybutu `options` encji select trybu pracy — dzięki temu
    walidacja I-10 działa na realnej liście opcji falownika, a nie na naszym przypuszczeniu.
    """
    allowed: tuple[str, ...] | None = None
    mode_entity = options.get(OPT_ENTITY_EMS_MODE)
    if mode_entity:
        state = hass.states.get(mode_entity)
        if state is not None:
            raw = state.attributes.get("options")
            if isinstance(raw, (list, tuple)) and raw:
                allowed = tuple(str(o) for o in raw)

    return InverterLimits(allowed_modes=allowed)


__all__ = ["read_device_state", "read_inverter_limits"]
