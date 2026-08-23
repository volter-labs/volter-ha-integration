"""R-3, R-9 — ustalenia z przeglądu adwersaryjnego Fazy A
(`docs/analysis/2026-08-16-faza-a-review.md`).

R-3 (WYSOKA): `apply_params` robi `continue` dla niezmapowanej encji BEZ wpisu do `errors`.
Executor ustawia `ERROR` tylko `if errors and not executed` — brak błędu + brak zapisu = `SUCCESS`
(albo, jeśli guard już wcześniej ustawił `PARTIAL` z innego powodu, ten status po prostu
przechodzi bez zmian, mimo że nic nie poszło do falownika). Scenariusz szczególnie groźny:
I-4 przy cenie <= 0 ustawia `export_limit=0` i `export_limit_enabled=True` — jeśli te encje nie
są zmapowane, zabezpieczenie znika bez śladu.

R-9 (ŚREDNIA): `_report_guard_result` w `command_handler.py` przekazuje `errors=[]` na sztywno.
Realne błędy per-encja (z `executor._last`/`GuardResult`) nigdy nie opuszczają HA.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.volter.applier import apply_params
from custom_components.volter.command_handler import VolterCommandHandler
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Action, GuardResult, Status
from tests.conftest import FakeHass


# ── R-3: niezmapowana encja musi zostawić ślad w errors ──────────────────────


@pytest.mark.asyncio
async def test_r3_niezmapowana_encja_to_cichy_no_op_raportowany_jako_sukces():
    """`apply_params` dla parametru bez zmapowanej encji dziś robi `continue` bez
    wpisu do `errors` — `executed=[]` i `errors=[]` razem wyglądają jak "nic nie
    trzeba było robić", a nie jak "próbowałem i nie miałem gdzie zapisać"."""
    hass = FakeHass()
    options: dict = {}  # entity_eco_mode_soc świadomie NIE zmapowane

    # RR-3: `forced_params=None` (domyślne, brak wiedzy od guardów — stary kontrakt
    # wywołania spoza executora) zachowuje pierwotną naprawę R-3 co do joty: KAŻDY
    # brak mapowania jest błędem.
    executed, errors, notes = await apply_params(hass, options, {"eco_soc": 40.0})

    assert executed == []
    assert errors, "niezmapowana encja musi zostawić wpis w errors — inaczej cichy no-op"
    assert errors[0]["entity"] == "eco_soc"
    assert notes == []


@pytest.mark.asyncio
async def test_r3_niezmapowany_przelacznik_export_limit_to_cichy_no_op():
    """Ta sama luka, ale w gałęzi `export_limit_enabled` (osobny `continue` w applier.py)."""
    hass = FakeHass()
    options: dict = {}  # entity_export_limit_switch świadomie NIE zmapowany

    executed, errors, notes = await apply_params(hass, options, {"export_limit_enabled": True})

    assert executed == []
    assert errors, "niezmapowany przełącznik limitu eksportu też musi trafić do errors"
    assert errors[0]["entity"] == "export_limit_enabled"
    assert notes == []


@pytest.mark.asyncio
async def test_r3_czesciowy_zapis_niezmapowanej_encji_daje_errors_i_wykonane_osobno():
    """Jeden parametr zmapowany (zapisuje się), drugi nie — `executed` i `errors`
    muszą pokazać oba fakty osobno, żeby executor mógł policzyć PARTIAL/ERROR."""
    hass = FakeHass()
    hass.states.set("select.tryb", "auto", {"options": ["auto", "charge_battery"]})
    options = {"entity_ems_mode": "select.tryb"}  # eco_soc świadomie NIE zmapowane

    executed, errors, notes = await apply_params(
        hass, options, {"mode": "charge_battery", "eco_soc": 40.0}
    )

    assert executed == ["mode"]
    assert [e["entity"] for e in errors] == ["eco_soc"]
    assert notes == []


@pytest.mark.asyncio
async def test_r3_i4_export_limit_niezmapowany_nie_moze_wygladac_jak_zabezpieczenie(fake_entry):
    """Scenariusz wprost z ustalenia R-3: cena <= 0, I-4 chce ustawić
    `export_limit=0` + `export_limit_enabled=True`, ale obie encje są niezmapowane.

    Przed naprawą: `result.status` zostaje `PARTIAL` z notą I-4 "eksport zablokowany",
    mimo że NIC nie poszło do falownika — chmura widzi coś, co wygląda jak zadziałane
    zabezpieczenie. Naprawa: skoro nic nie poszło i są errory, status musi być `ERROR`.
    """
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    fake_entry.options = {"entity_soc": "sensor.soc"}
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply(
        {}, price_pln_kwh=-0.05, action=Action.SELF_CONSUME, source="test"
    )

    assert result.executed == [], "encje niezmapowane — nic nie mogło pójść do falownika"
    assert result.status is Status.ERROR, (
        "brak zapisu + realne errory zapisu = ERROR, nie PARTIAL wyglądający jak sukces "
        "zabezpieczenia I-4"
    )
    assert result.errors, "R-9: GuardResult musi nieść realne błędy zapisu dalej"


# ── R-9: błędy zapisu muszą dotrzeć do chmury ─────────────────────────────────


def _handler(executor) -> VolterCommandHandler:
    hass = FakeHass()
    entry = type("E", (), {"options": {"entity_ems_mode": "select.tryb"}})()
    handler = VolterCommandHandler(
        hass=hass, entry=entry, device_id="dev-1",
        supabase_url="https://example.supabase.co",
        anon_key="anon", api_key="vk_test", executor=executor,
    )
    handler._report_result = AsyncMock()
    return handler


@pytest.mark.asyncio
async def test_r9_bledy_zapisu_docieraja_do_chmury():
    """`_report_guard_result` przekazywał `errors=[]` na sztywno — realne błędy per-encja
    z `GuardResult.errors` nigdy nie docierały do `_report_result`, czyli nigdy nie
    opuszczały HA. To regresja wobec implementacji sprzed Fazy A."""
    executor = AsyncMock()
    executor.async_apply.return_value = GuardResult(
        params={"eco_soc": 30.0},
        status=Status.ERROR,
        executed=[],
        errors=[{"entity": "eco_soc", "error": "falownik nie odpowiada"}],
    )
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-err", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)

    handler._report_result.assert_awaited_with(
        "req-err",
        "error",
        executed=[],
        errors=[{"entity": "eco_soc", "error": "falownik nie odpowiada"}],
        notes=[],
    )
