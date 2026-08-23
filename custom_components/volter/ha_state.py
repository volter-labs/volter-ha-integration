"""Odczyt stanu instalacji i granic sprzętowych z encji Home Assistanta.

Cała wiedza o HA jest tutaj, żeby `guards.py` i `schedule.py` zostały czyste
i testowalne na hoście (a docelowo przepisywalne na C).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant, State

from .const import (
    COMMAND_ENTITY_MAP,
    OPT_ENTITY_BATTERY_POWER,
    OPT_ENTITY_EMS_MODE,
    OPT_ENTITY_GRID_POWER,
    OPT_ENTITY_PV_POWER,
    OPT_ENTITY_SOC,
    PARAM_BOUNDS_CACHE_TTL_S,
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
    """Wiek odczytu od OSTATNIEGO RAPORTU integracji, nie od ostatniej ZMIANY.

    HA odświeża `last_updated` tylko, gdy stan lub atrybuty się zmienią. Nocą
    `pv_power` stoi na 0 godzinami, więc `last_updated` jest sprzed zmierzchu —
    a I-9 bierze maksimum z wieków trzech encji. Przed tą poprawką każdej nocy
    zapisy były wstrzymane („odczyt starszy niż 300s, wiek 2573s"), choć falownik
    raportował co kilka sekund. `last_reported` (HA ≥ 2024.4) rośnie przy każdym
    raporcie; starsze obiekty bez tego pola degradują do `last_updated`.
    """
    if state is None:
        return None
    stamp = getattr(state, "last_reported", None) or state.last_updated
    return max(0.0, (now - stamp).total_seconds())


def read_device_state(
    hass: HomeAssistant,
    options: dict,
    previous_soc: float | None = None,
    previous_soc_age_s: float | None = None,
) -> DeviceState:
    """Zbierz migawkę stanu z zmapowanych encji monitoringu.

    `age_s` to wiek NAJSTARSZEGO istotnego odczytu — świadomie pesymistycznie,
    bo guard I-9 ma chronić przed działaniem na nieaktualnym obrazie instalacji.

    RR-1: `previous_soc_age_s` to odstęp między poprzednią zaufaną próbką SoC
    a bieżącym odczytem. Podaje go wołający (executor), bo tylko on wie, kiedy
    baseline został przyjęty — HA nie przechowuje historii naszych decyzji.
    Bez tego I-9 porównuje same wartości i myli awarię czujnika z normalną zmianą
    po przerwie w telemetrii.
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
        previous_soc_age_s=previous_soc_age_s,
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


class ParamBoundsCache:
    """RR-2 (reszta R-8): ostatnie ZNANE granice nastaw, per parametr.

    Powód: bez pamięci werdykt I-10 zależał od DOSTĘPNOŚCI encji — ta sama komenda
    dostawała raz `success` (encja podała `max`), raz `error` (encja `unavailable`,
    więc `_bounds` zwracało `None` i wracał zgadywany zakres z `PARAM_SPECS`).
    Dla użytkownika wyglądało to na losowe odrzucanie poleceń.

    Granice `min`/`max` encji `number` to właściwość SPRZĘTU (zakres rejestru), a nie
    jego stan — chwilowa niedostępność integracji nie zmienia tego, co falownik
    przyjmie. Pamięć jest jednak ograniczona w czasie (`ttl_s`), bo po wymianie albo
    przekonfigurowaniu falownika stara granica byłaby groźniejsza niż jej brak.
    Cache żyje wyłącznie w pamięci procesu — restart HA i tak go czyści.
    """

    def __init__(self, ttl_s: float = PARAM_BOUNDS_CACHE_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._entries: dict[str, tuple[ParamBounds, float]] = {}

    def remember(self, param: str, bounds: ParamBounds, now: float) -> None:
        self._entries[param] = (bounds, now)

    def get(self, param: str, now: float) -> ParamBounds | None:
        entry = self._entries.get(param)
        if entry is None:
            return None
        bounds, ts = entry
        if now - ts > self.ttl_s:
            # Wygasłego wpisu nie usuwamy w miejscu: `snapshot()` ma być tanią kopią,
            # a wygaśnięcie i tak liczy się od znacznika, nie od obecności w słowniku.
            return None
        return bounds

    def snapshot(self) -> "ParamBoundsCache":
        """Kopia do SUCHEGO przebiegu (`async_diagnose`).

        RR-2: diagnoza musi widzieć te same granice co realny tor zapisu, ale nie wolno
        jej niczego zapamiętać — kontrakt „zero mutacji stanu" z R-7/R-14. Zapisy lądują
        na kopii i giną razem z nią.
        """
        kopia = ParamBoundsCache(self.ttl_s)
        kopia._entries = dict(self._entries)
        return kopia


def read_inverter_limits(
    hass: HomeAssistant,
    options: dict,
    bounds_cache: "ParamBoundsCache | None" = None,
    now: float | None = None,
) -> InverterLimits:
    """Odczytaj to, co da się odczytać z HA.

    `allowed_modes` bierzemy z atrybutu `options` encji select trybu pracy — dzięki temu
    walidacja I-10 działa na realnej liście opcji falownika, a nie na naszym przypuszczeniu.

    `param_bounds` bierzemy z atrybutów `min`/`max` encji `number` (R-8). To jedyne
    źródło realnych granic dostępne przed mapą nastaw z Etapu 3, a bez nich część mocowa
    I-3 była martwa: wektor T-3 („przytnij do granicy") nie miał do czego przycinać.

    RR-2: `bounds_cache` przechowuje ostatnie znane granice i wchodzi WYŁĄCZNIE wtedy,
    gdy encja akurat nic sensownego nie podaje — dzięki temu werdykt I-10 nie zmienia
    się w zależności od tego, czy integracja falownika chwilowo odpowiada. Świeży odczyt
    ma zawsze pierwszeństwo (cache jest awaryjny, nie autorytatywny).
    """
    timestamp = time.monotonic() if now is None else now
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
            if bounds_cache is not None:
                bounds_cache.remember(param, found, timestamp)
            continue
        if bounds_cache is not None:
            # RR-2: encja chwilowo nie odpowiada — sięgamy po ostatnią znaną granicę
            # zamiast udawać, że sprzęt nagle przestał mieć zakres. Bez pamięci ta sama
            # komenda dostawała raz `success`, raz `error`, zależnie od stanu encji.
            zapamietana = bounds_cache.get(param, timestamp)
            if zapamietana is not None:
                bounds[param] = zapamietana
                _LOGGER.debug(
                    "Granice %s z pamięci (encja %s niedostępna): %s..%s [RR-2]",
                    param, entity_id, zapamietana.lo, zapamietana.hi,
                )

    return InverterLimits(allowed_modes=allowed, param_bounds=bounds)


__all__ = ["ParamBoundsCache", "read_device_state", "read_inverter_limits"]
