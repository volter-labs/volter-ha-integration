"""Encje Voltera w HA: plan, moc, stan zapisu i wyłącznik sterowania.

Do tej wersji integracja nie tworzyła w HA ANI JEDNEJ encji — jedynym wglądem
był log, a jedynym hamulcem usunięcie mapowania encji falownika.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter.entity import plan_do_json
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Action
from custom_components.volter.schedule import Fallback, Schedule, Slot
from custom_components.volter.sensor import (
    VolterMocSensor,
    VolterOstatniZapisSensor,
    VolterPlanDoSensor,
    VolterPlanSensor,
)
from custom_components.volter.switch import VolterControlSwitch
from tests.conftest import FakeHass

#: Plan budujemy wokół REALNEGO „teraz”, bo `Schedule.effective_slot`
#: pyta o bieżący czas — slot ze sztywnej daty nigdy nie byłby aktywny.
TERAZ = datetime.now(timezone.utc)

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
}


def _plan() -> Schedule:
    baza = TERAZ.replace(minute=0)
    return Schedule(
        schedule_id="plan-1",
        slots=[
            Slot(start=baza - timedelta(hours=1), end=baza, action=Action.CHARGE,
                 charge_source="grid", power_w=3000.0, soc_target=80.0,
                 price_pln_kwh=0.31, export_allowed=False),
            Slot(start=baza, end=baza + timedelta(hours=1), action=Action.DISCHARGE,
                 discharge_purpose="sell", power_w=4000.0, soc_target=15.0,
                 price_pln_kwh=1.42, export_allowed=True, export_limit_w=5000.0),
        ],
        fallback=Fallback(action=Action.SELF_CONSUME, soc_reserve=10.0),
    )


def _executor(fake_entry, hass: FakeHass | None = None) -> VolterExecutor:
    hass = hass or FakeHass()
    fake_entry.options = dict(OPTIONS)
    ex = VolterExecutor(hass, fake_entry)
    ex._schedule = _plan()
    return ex


# ── serializacja planu dla karty ─────────────────────────────────────────────


def test_plan_do_json_niesie_wszystko_czego_potrzebuje_karta():
    sloty = plan_do_json(_plan(), TERAZ)

    assert len(sloty) == 2
    biezacy = [s for s in sloty if s["teraz"]]
    assert len(biezacy) == 1, "dokładnie jeden slot może być 'teraz'"
    assert biezacy[0]["akcja"] == "discharge"
    assert biezacy[0]["cel_rozladowania"] == "sell"
    assert biezacy[0]["moc_w"] == 4000.0
    assert biezacy[0]["cena"] == 1.42


def test_plan_do_json_uzywa_akcji_EFEKTYWNEJ_nie_pola_mode():
    """Karta musi pokazywać to, co robi urządzenie, a nie dosłowne pole planu.

    Slot `self_consume` z `discharge_purpose` JEST rozładowaniem (U-6) — gdyby
    karta czytała surowe `action`, użytkownik widziałby autokonsumpcję w godzinie,
    w której falownik sprzedaje.
    """
    baza = TERAZ.replace(minute=0)
    plan = Schedule(slots=[
        Slot(start=baza, end=baza + timedelta(hours=1), action=Action.SELF_CONSUME,
             discharge_purpose="self", power_w=1476.0),
    ])

    assert plan_do_json(plan, TERAZ)[0]["akcja"] == "discharge"


def test_plan_do_json_bez_planu_daje_pusta_liste():
    assert plan_do_json(None, TERAZ) == []


class _RuntimeStub:
    def __init__(self, wlaczone: bool = False) -> None:
        self.control_enabled = wlaczone
        self.zapisy: list[bool] = []

    async def async_set_control_enabled(self, wlaczone: bool) -> None:
        self.control_enabled = bool(wlaczone)
        self.zapisy.append(bool(wlaczone))


# ── sensory ──────────────────────────────────────────────────────────────────


def test_sensor_planu_pokazuje_biezacy_tryb_i_caly_plan(fake_entry):
    # Runtime podany JAWNIE: autouse fixture z conftestu włącza sterowanie na czas
    # testów toru zapisu, więc bez tego asercja mierzyłaby harness, nie encję.
    ex = _executor(fake_entry)
    ex._runtime = _RuntimeStub(False)
    czujnik = VolterPlanSensor(fake_entry, ex)

    assert czujnik.native_value == "discharge"
    atrybuty = czujnik.extra_state_attributes
    assert len(atrybuty["sloty"]) == 2
    assert atrybuty["schedule_id"] == "plan-1"
    assert atrybuty["sterowanie_wlaczone"] is False


def test_sensor_planu_bez_planu_nie_wybucha(fake_entry):
    fake_entry.options = dict(OPTIONS)
    czujnik = VolterPlanSensor(fake_entry, VolterExecutor(FakeHass(), fake_entry))

    assert czujnik.native_value == "brak planu"
    assert czujnik.extra_state_attributes["sloty"] == []


def test_sensor_mocy_bierze_moc_biezacego_slotu(fake_entry):
    assert VolterMocSensor(fake_entry, _executor(fake_entry)).native_value == 4000.0


def test_sensor_waznosci_planu(fake_entry):
    czujnik = VolterPlanDoSensor(fake_entry, _executor(fake_entry))

    assert czujnik.native_value == _plan().valid_until


def test_sensor_ostatniego_zapisu_bez_historii(fake_entry):
    assert VolterOstatniZapisSensor(fake_entry, _executor(fake_entry)).native_value == "brak"


# ── wyłącznik sterowania ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wylacznik_odzwierciedla_i_zmienia_stan(fake_entry):
    runtime = _RuntimeStub(False)
    przelacznik = VolterControlSwitch(fake_entry, _executor(fake_entry), runtime)

    assert przelacznik.is_on is False

    await przelacznik.async_turn_on()
    assert przelacznik.is_on is True

    await przelacznik.async_turn_off()
    assert przelacznik.is_on is False
    assert runtime.zapisy == [True, False], "każde przełączenie musi zostać utrwalone"


@pytest.mark.asyncio
async def test_wylacznik_nie_przeladowuje_config_entry(fake_entry):
    """S-7: przełączenie przez opcje wymuszałoby `async_reload`, czyli reset
    ochrony pamięci nieulotnej i budżetu anty-oscylacji. Przełącznik pisze do
    runtime'u i nie dotyka `entry.options`."""
    runtime = _RuntimeStub(False)
    przelacznik = VolterControlSwitch(fake_entry, _executor(fake_entry), runtime)
    opcje_przed = dict(fake_entry.options)

    await przelacznik.async_turn_on()

    assert dict(fake_entry.options) == opcje_przed


def test_executor_czyta_wylacznik_z_runtimeu(fake_entry):
    """To jest właściwość, na której stoi bezpieczeństwo: tor zapisu i encja
    muszą patrzeć na TĘ SAMĄ wartość."""
    runtime = _RuntimeStub(True)
    fake_entry.options = dict(OPTIONS)
    ex = VolterExecutor(FakeHass(), fake_entry, runtime=runtime)

    assert ex.sterowanie_wlaczone is True
    runtime.control_enabled = False
    assert ex.sterowanie_wlaczone is False
