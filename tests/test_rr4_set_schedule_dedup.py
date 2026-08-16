"""RR-4 (WYSOKA) — naprawa R-2 pozwala cofnąć aktywny plan retransmisją SET_SCHEDULE.

`docs/analysis/2026-08-16-faza-a-regresje.md`, sonda P12.

`_remember_if_effective` (R-2) wymaga niepustego `result.executed`. Ale
`VolterExecutor.async_set_schedule` UTRWALA harmonogram w `Store` i podmienia
`self._schedule` ZANIM dojdzie do jakiegokolwiek zapisu do falownika — to jest
trwały skutek uboczny, niezależny od tego, czy throttle I-6 odfiltruje wszystkie
zapisy (`executed == []`). Gdy taki request_id nie zostanie zapamiętany, jego
retransmisja (np. po reconnect Realtime) nie trafia w dedup i potrafi cofnąć
aktywny (nowszy) plan do starszej wersji.

Każdy test w tym pliku pilnuje OBU stron:
  * że RR-4 faktycznie chroni SET_SCHEDULE przed cofnięciem planu retransmisją,
  * że naprawa NIE przywraca pierwotnego problemu R-2 — SET_WORK_MODE zakończone
    DEGRADED/THROTTLED/ERROR musi pozostać retryowalne (pusty `executed` NIE może
    zablokować retry dla tej komendy).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.volter.command_handler import VolterCommandHandler
from custom_components.volter.guards import GuardResult, Status
from tests.conftest import FakeHass


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


# ── RR-4: SET_SCHEDULE musi się zapamiętać, gdy plan został utrwalony ────────


@pytest.mark.asyncio
async def test_rr4_p12_retransmisja_starego_planu_nie_cofa_aktywnego():
    """Sonda P12 wprost: req-A instaluje plan A (zapamiętany). req-B instaluje
    plan B, ale I-6 odfiltrowuje wszystkie zapisy (`executed=[]`) — mimo to plan
    B jest już w `Store`. req-C instaluje plan C. Reconnect Realtime redostarcza
    req-B: PRZED naprawą to nie duplikat -> plan cofa się do B. PO naprawie: req-B
    jest już zapamiętany od razu przy instalacji, więc retransmisja to `duplicate`
    i executor.async_set_schedule NIE jest wołany ponownie."""
    executor = AsyncMock()
    executor.async_set_schedule.side_effect = [
        GuardResult(params={}, status=Status.SUCCESS, executed=["mode", "eco_soc"]),  # req-A
        GuardResult(params={}, status=Status.SUCCESS, executed=[]),  # req-B: I-6 filtruje wszystko
        GuardResult(params={}, status=Status.SUCCESS, executed=["mode"]),  # req-C
    ]
    handler = _handler(executor)

    await handler._execute_command(
        {"command": "SET_SCHEDULE", "request_id": "req-A", "schedule": {"schedule_id": "A"}}
    )
    await handler._execute_command(
        {"command": "SET_SCHEDULE", "request_id": "req-B", "schedule": {"schedule_id": "B"}}
    )
    await handler._execute_command(
        {"command": "SET_SCHEDULE", "request_id": "req-C", "schedule": {"schedule_id": "C"}}
    )

    # Reconnect Realtime redostarcza req-B.
    await handler._execute_command(
        {"command": "SET_SCHEDULE", "request_id": "req-B", "schedule": {"schedule_id": "B"}}
    )

    assert executor.async_set_schedule.await_count == 3, (
        "retransmisja req-B nie może ponownie zainstalować planu B i cofnąć aktywnego planu C"
    )
    handler._report_result.assert_awaited_with("req-B", "duplicate")


@pytest.mark.asyncio
async def test_rr4_set_schedule_z_pustym_executed_jest_mimo_to_zapamietywany():
    """Wariant izolowany sondy P12: sam fakt, że `executed=[]` (throttle I-6
    odfiltrował wszystko), nie może uniemożliwić zapamiętania SET_SCHEDULE —
    bo harmonogram i tak trafił do `Store` (skutek trwały, niezależny od executed)."""
    executor = AsyncMock()
    executor.async_set_schedule.return_value = GuardResult(
        params={}, status=Status.SUCCESS, executed=[]
    )
    handler = _handler(executor)

    payload = {"command": "SET_SCHEDULE", "request_id": "req-empty", "schedule": {"schedule_id": "X"}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_set_schedule.await_count == 1, (
        "SET_SCHEDULE z executed=[] wciąż utrwaliło plan — retransmisja musi być duplikatem"
    )
    handler._report_result.assert_awaited_with("req-empty", "duplicate")


@pytest.mark.asyncio
async def test_rr4_blad_parsowania_harmonogramu_nie_jest_zapamietywany():
    """Strona ochrony: gdy `async_set_schedule` w ogóle rzuci wyjątkiem (plan
    nigdy nie trafił do `Store` — nie ma trwałego skutku ubocznego do ochrony),
    komenda MUSI pozostać retryowalna, dokładnie jak przed RR-4."""
    executor = AsyncMock()
    executor.async_set_schedule.side_effect = ValueError("zły JSON harmonogramu")
    handler = _handler(executor)

    payload = {"command": "SET_SCHEDULE", "request_id": "req-bad", "schedule": {"bad": True}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_set_schedule.await_count == 2, (
        "błąd parsowania nie utrwalił planu — komenda musi zostać retryowalna"
    )


# ── strona ochrony R-2: RR-4 nie może przywrócić pierwotnego problemu ────────


@pytest.mark.asyncio
async def test_rr4_nie_przywraca_r2_set_work_mode_degraded_dalej_retryowalny():
    """RR-4 dotyczy WYŁĄCZNIE SET_SCHEDULE (harmonogram = trwały skutek uboczny
    zapisu do Store). SET_WORK_MODE nie ma takiego skutku — dla niego musi
    obowiązywać dokładnie R-2: DEGRADED z zerowym executed nie blokuje retry."""
    executor = AsyncMock()
    executor.async_apply.side_effect = [
        GuardResult(params={}, status=Status.DEGRADED),
        GuardResult(params={"eco_soc": 30.0}, status=Status.SUCCESS, executed=["eco_soc"]),
    ]
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-wm", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_apply.await_count == 2, (
        "RR-4 nie może odebrać SET_WORK_MODE retryowalności z R-2"
    )
    handler._report_result.assert_awaited_with(
        "req-wm", "success", executed=["eco_soc"], errors=[], notes=[]
    )


@pytest.mark.asyncio
async def test_rr4_nie_przywraca_r2_set_work_mode_success_pusty_executed_retryowalny():
    """Analogicznie: SUCCESS z pustym `executed` (throttle I-6) dla SET_WORK_MODE
    też musi pozostać retryowalny — to zachowanie R-2, nietknięte przez RR-4."""
    executor = AsyncMock()
    executor.async_apply.side_effect = [
        GuardResult(params={"eco_soc": 30.0}, status=Status.SUCCESS, executed=[]),
        GuardResult(params={"eco_soc": 30.0}, status=Status.SUCCESS, executed=["eco_soc"]),
    ]
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-wm-2", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_apply.await_count == 2, (
        "SET_WORK_MODE z executed=[] musi zostać retryowalny — to jest istota R-2"
    )
