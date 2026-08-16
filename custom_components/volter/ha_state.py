"""Odczyt stanu instalacji i granic sprzętowych z encji Home Assistanta.

Cała wiedza o HA jest tutaj, żeby `guards.py` i `schedule.py` zostały czyste
i testowalne na hoście (a docelowo przepisywalne na C).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant, State

from .const import (
    COMMAND_ENTITY_MAP,
    OPT_ENTITY_BATTERY_POWER,
    OPT_ENTITY_EMS_MODE,
    OPT_ENTITY_GRID_POWER,
    OPT_ENTITY_PV_POWER,
    OPT_ENTITY_SOC,
)
from .guards import DeviceState, InverterLimits, ParamBounds

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


#: R-8: parametry sterowane encją `number` — tylko one wystawiają `min`/`max`.
#: Wyprowadzamy je z `COMMAND_ENTITY_MAP`, żeby dołożenie nastawy w jednym miejscu
#: automatycznie objęło ją granicami I-3 (`mode` to `select`, więc wypada sam).
_NUMBER_PARAMS: dict[str, str] = {
    param: opt_key
    for param, (opt_key, domain, _service, _data_key) in COMMAND_ENTITY_MAP.items()
    if domain == "number"
}


def _attr_num(state: State, name: str) -> float | None:
    raw = state.attributes.get(name)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def _bounds(state: State | None) -> ParamBounds | None:
    """Granica z encji `number` albo `None`, gdy nie da się jej zaufać.

    R-8: „nie wiem" jest bezpieczniejsze niż granica zmyślona — przy braku granicy
    zostaje sanity-check z `PARAM_SPECS`, a przy granicy fałszywej I-3 przycinałby
    każdą nastawę do wartości bez pokrycia w sprzęcie.
    """
    if state is None or str(state.state).lower() in _UNAVAILABLE:
        # Encja niedostępna: jej atrybuty bywają wtedy puste albo resztkowe.
        return None
    hi = _attr_num(state, "max")
    if hi is None or hi <= 0:
        # `max=0` to encja jeszcze niezainicjalizowana albo błędny odczyt: wzięcie
        # tego za prawdę przycięłoby każdą nastawę do zera i zatrzymało instalację.
        return None
    lo = _attr_num(state, "min")
    lo = 0.0 if lo is None else lo
    if lo >= hi:
        return None
    return ParamBounds(lo=lo, hi=hi)


def read_inverter_limits(hass: HomeAssistant, options: dict) -> InverterLimits:
    """Odczytaj to, co da się odczytać z HA.

    `allowed_modes` bierzemy z atrybutu `options` encji select trybu pracy — dzięki temu
    walidacja I-10 działa na realnej liście opcji falownika, a nie na naszym przypuszczeniu.

    `param_bounds` bierzemy z atrybutów `min`/`max` encji `number` (R-8). To jedyne
    źródło realnych granic dostępne przed mapą nastaw z Etapu 3, a bez nich część mocowa
    I-3 była martwa: wektor T-3 („przytnij do granicy") nie miał do czego przycinać.
    """
    allowed: tuple[str, ...] | None = None
    mode_entity = options.get(OPT_ENTITY_EMS_MODE)
    if mode_entity:
        state = hass.states.get(mode_entity)
        if state is not None:
            raw = state.attributes.get("options")
            if isinstance(raw, (list, tuple)) and raw:
                allowed = tuple(str(o) for o in raw)

    bounds: dict[str, ParamBounds] = {}
    for param, opt_key in _NUMBER_PARAMS.items():
        entity_id = options.get(opt_key)
        if not entity_id:
            continue
        found = _bounds(hass.states.get(entity_id))
        if found is not None:
            bounds[param] = found

    return InverterLimits(allowed_modes=allowed, param_bounds=bounds)


__all__ = ["read_device_state", "read_inverter_limits"]
