"""R-5, R-6, R-11 — ustalenia z przeglądu adwersaryjnego Fazy A
(`docs/analysis/2026-08-16-faza-a-review.md`), wszystkie w `executor.py`.

R-5 (WYSOKA): `self._prev_soc = state.soc` wykonuje się PRZED sprawdzeniem
`result.rejected`. Odczyt odrzucony przez I-9 jako fizycznie niemożliwy skok SoC
staje się zaufanym baseline'em NASTĘPNEGO przebiegu — I-9 chroni przez dokładnie
jeden tick zamiast chronić dopóki telemetria nie wróci do sensownych wartości.

R-6 (WYSOKA): `apply_guards` uruchamia I-4 tylko gdy `ctx.price_pln_kwh is not None`.
Ścieżka SET_WORK_MODE z chmury (`command_handler.py`) nie przekazuje ceny w ogóle —
I-4 (poziom 4 hierarchii, inwariant, nie reguła planu) jest więc martwe dokładnie
tam, gdzie ma bronić przed komendą operatora.

R-11 (ŚREDNIA): `self._schedule is None` (stan domyślny po instalacji i po restarcie
z pustym storage) prowadzi dziś do cichego `SUCCESS` bez żadnego zapisu — falownik
zostaje na dowolnej wcześniejszej nastawie. I-5 (TTL planu) ma reagować fallbackiem
na BRAK harmonogramu tak samo jak na jego wygaśnięcie.

Specyfikacja: `volter-box/03-produkt/guardy-i-inwarianty.md` (I-4, I-5, I-9).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Action, Status
from tests.conftest import FakeHass


OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_export_limit": "number.export_limit",
    "entity_export_limit_switch": "switch.export_limit",
}


def _hass_ze_swiezym_stanem(soc: str = "55") -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", soc)
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb", "auto", {"options": ["auto", "charge_pv", "discharge_pv", "import_ac", "export_ac", "conserve", "off_grid", "battery_standby", "buy_power", "sell_power", "charge_battery", "discharge_battery"]}
    )
    hass.states.set("number.eco_soc", "20")
    hass.states.set("number.export_limit", "5000")
    return hass


# ── R-5: I-9 musi chronić dłużej niż jeden tick ──────────────────────────────


@pytest.mark.asyncio
async def test_r5_i9_chroni_dluzej_niz_jeden_tick(fake_entry):
    """Scenariusz wprost z ustalenia R-5: `prev=50`, sensor zgłasza 95 → tick 1
    `degraded`, ale `_prev_soc=95` (dziś). Tick 2 (sensor NADAL zgłasza 95) → brak
    skoku względem podmienionego baseline'u → `success`, zapisy idą na podstawie
    odczytu, który system chwilę wcześniej uznał za niewiarygodny."""
    hass = _hass_ze_swiezym_stanem(soc="95")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    executor._prev_soc = 50.0  # ustalony, zaufany odczyt z poprzedniego przebiegu

    tick1 = await executor.async_apply({}, source="test")
    assert tick1.status is Status.DEGRADED
    assert any(n.invariant == "I-9" for n in tick1.notes)

    # Sensor nadal zgłasza 95 (bez zmian). Jeśli baseline został podmieniony w ticku 1,
    # ten przebieg nie zobaczy już skoku i przejdzie I-9.
    tick2 = await executor.async_apply({}, source="test")
    assert tick2.status is Status.DEGRADED, (
        "I-9 musi chronić także w kolejnym ticku — odczyt odrzucony jako fizycznie "
        "niemożliwy nie może stać się zaufanym baseline'em następnego przebiegu"
    )
    assert any(n.invariant == "I-9" for n in tick2.notes)


# ── R-6: I-4 musi działać na ścieżce komend z chmury ─────────────────────────


def _schedule_z_cena(price: float) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "r6",
        "slots": [
            {
                "from": (now - timedelta(minutes=10)).isoformat(),
                "to": (now + timedelta(minutes=50)).isoformat(),
                "mode": "self_consume",
                "soc_target": 50.0,
                "price_pln_kwh": price,
                "export_allowed": True,
            }
        ],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    }


@pytest.mark.asyncio
async def test_r6_i4_dziala_na_sciezce_komend_z_chmury(fake_entry):
    """I-4 to inwariant (poziom 4 hierarchii specyfikacji), nie reguła planu — ma
    bronić także przed komendą operatora. Symulujemy dokładnie to, co robi
    `command_handler._execute_command` dla `SET_WORK_MODE`: `async_apply` bez
    `price_pln_kwh`. Aktualny slot harmonogramu ma cenę <= 0 — I-4 musi ją wziąć
    stamtąd, skoro komenda ceny nie niesie."""
    hass = _hass_ze_swiezym_stanem(soc="55")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    await executor.async_set_schedule(_schedule_z_cena(-0.05))

    result = await executor.async_apply(
        {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="cloud"
    )

    assert any(n.invariant == "I-4" for n in result.notes), (
        "I-4 musi zadziałać na ścieżce SET_WORK_MODE, biorąc cenę z aktualnego slotu "
        "harmonogramu, skoro komenda z chmury jej nie niesie"
    )
    assert result.params.get("export_limit") == 0.0
    assert result.params.get("export_limit_enabled") is True


# ── R-11: brak harmonogramu w ogóle musi prowadzić do fallbacku ─────────────


@pytest.mark.asyncio
async def test_r11_brak_harmonogramu_prowadzi_do_fallbacku(fake_entry):
    """`self._schedule is None` to stan domyślny po instalacji i po restarcie z
    pustym storage — dziś kod loguje DEBUG i zwraca `SUCCESS` bez zapisu, zostawiając
    falownik na dowolnej wcześniejszej nastawie. Musi zamiast tego użyć domyślnego
    fallbacku (self_consume + rezerwa z opcji użytkownika), dokładnie jak przy
    wygaśnięciu planu (I-5)."""
    hass = _hass_ze_swiezym_stanem(soc="50")
    fake_entry.options = dict(OPTIONS)
    fake_entry.options["soc_reserve"] = 25.0
    executor = VolterExecutor(hass, fake_entry)
    assert executor._schedule is None, "warunek testu: harmonogram nigdy nie ustawiony"

    result = await executor._async_execute_now()

    assert result is not None
    assert result.executed, (
        "brak harmonogramu nie może być cichym SUCCESS bez zapisu — musi wykonać "
        "domyślny fallback (self_consume + rezerwa użytkownika)"
    )
    assert "mode" in result.executed
    assert result.params.get("eco_soc") == 25.0
