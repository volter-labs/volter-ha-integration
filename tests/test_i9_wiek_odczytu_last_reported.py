"""I-9: wiek odczytu liczy się od OSTATNIEGO RAPORTU, nie od ostatniej ZMIANY.

Znalezisko z 2026-08-23 (`sensor.volter_energy_last_write = degraded`,
nota „odczyt starszy niż 300s (wiek 2573s)"). HA odświeża `last_updated` tylko
wtedy, gdy stan lub atrybuty się ZMIENIĄ. Nocą `pv_power` stoi na 0 przez wiele
godzin, więc `last_updated` encji PV jest sprzed zmierzchu — a `age_s` bierze
MAKSIMUM z wieków trzech encji. Skutek: każdej nocy I-9 wstrzymywał zapisy,
mimo że falownik raportował co kilka sekund.

`State.last_reported` (HA ≥ 2024.4) jest odświeżane przy KAŻDYM raporcie
integracji, także gdy wartość się nie zmieniła. To jest właściwa miara świeżości.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.volter.const import (
    OPT_ENTITY_GRID_POWER,
    OPT_ENTITY_PV_POWER,
    OPT_ENTITY_SOC,
)
from custom_components.volter.ha_state import read_device_state
from tests.conftest import FakeHass

OPTIONS = {
    OPT_ENTITY_SOC: "sensor.soc",
    OPT_ENTITY_PV_POWER: "sensor.pv",
    OPT_ENTITY_GRID_POWER: "sensor.grid",
}


def test_plaski_pv_w_nocy_nie_jest_nieswiezy_gdy_raportowany():
    hass = FakeHass()
    teraz = datetime.now(timezone.utc)
    zmierzch = teraz - timedelta(minutes=43)
    hass.states.set("sensor.soc", "61", last_updated=teraz - timedelta(seconds=5))
    # PV = 0 od zmierzchu: wartość bez zmian, ale integracja raportuje co 10 s.
    hass.states.set("sensor.pv", "0", last_updated=zmierzch, last_reported=teraz - timedelta(seconds=10))
    hass.states.set("sensor.grid", "-120", last_updated=teraz - timedelta(seconds=3))

    state = read_device_state(hass, OPTIONS)

    assert state.age_s < 60


def test_encja_naprawde_martwa_nadal_jest_stara():
    hass = FakeHass()
    dawno = datetime.now(timezone.utc) - timedelta(minutes=43)
    hass.states.set("sensor.soc", "61")
    # Brak raportów od 43 minut — `last_reported` == `last_updated` (tak robi HA).
    hass.states.set("sensor.pv", "0", last_updated=dawno, last_reported=dawno)
    hass.states.set("sensor.grid", "-120")

    state = read_device_state(hass, OPTIONS)

    assert state.age_s > 2500


def test_stan_bez_last_reported_degraduje_do_last_updated():
    """Starsze HA / obiekty bez pola — zachowanie sprzed naprawy."""
    hass = FakeHass()
    dawno = datetime.now(timezone.utc) - timedelta(minutes=20)
    hass.states.set("sensor.soc", "61")
    hass.states.set("sensor.pv", "0", last_updated=dawno)
    hass.states.set("sensor.grid", "0")
    del hass.states.get("sensor.pv").last_reported

    state = read_device_state(hass, OPTIONS)

    assert 1100 < state.age_s < 1300
