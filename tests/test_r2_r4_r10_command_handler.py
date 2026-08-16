"""R-2, R-4, R-10 — ustalenia z przeglądu adwersaryjnego Fazy A
(`docs/analysis/2026-08-16-faza-a-review.md`), wszystkie w `command_handler.py`.

R-2 (KRYTYCZNA): dedup zjada komendy przy statusach przejściowych. `_remember_unless_error`
pomijał zapamiętanie tylko dla `Status.ERROR` — `DEGRADED` (I-9), `THROTTLED` (I-8) i `SUCCESS`
z pustym `executed` (wszystko odfiltrował throttle I-6) też oznaczają "nic nie poszło do
falownika", a mimo to blokowały retry na stałe.

R-4 (WYSOKA): `payload.get('request_id', 'unknown')` — komendy bez `request_id` (ręczny
broadcast, smoke test Fazy C) dzieliły jeden wspólny klucz dedup. Po pierwszej udanej każda
następna była cicho odrzucana jako duplikat.

R-10 (ŚREDNIA): brak bariery na wyjątki w treści `_execute_command`. `params` niebędące
dictem wywala `AttributeError` w `infer_action`/`sanitize_params`, wyjątek propaguje przez
`_handle_message` do `_connect_and_listen`, które w `finally` zamyka WebSocket — zrywając
połączenie Realtime zamiast zaraportować błąd komendy.
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


# ── R-2: statusy przejściowe nie mogą blokować retry ─────────────────────────


@pytest.mark.asyncio
async def test_r2_degraded_nie_blokuje_retry_na_stale():
    """Scenariusz wprost z ustalenia R-2.

    `req-1` trafia na przestarzałą telemetrię -> `degraded`, zero zapisów. Telemetria
    wraca, chmura ponawia `req-1` -> przed naprawą: `duplicate`, zero wykonania,
    komenda przepada na stałe. Po naprawie: drugie wywołanie faktycznie wykonuje komendę.
    """
    executor = AsyncMock()
    executor.async_apply.side_effect = [
        GuardResult(params={}, status=Status.DEGRADED),
        GuardResult(params={"eco_soc": 30.0}, status=Status.SUCCESS, executed=["eco_soc"]),
    ]
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-1", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_apply.await_count == 2, (
        "DEGRADED nie może zapamiętać request_id — komenda musi zostać retryowalna"
    )
    handler._report_result.assert_awaited_with(
        "req-1", "success", executed=["eco_soc"], errors=[], notes=[]
    )


@pytest.mark.asyncio
async def test_r2_throttled_nie_blokuje_retry():
    """I-8 (anty-oscylacja) zwraca THROTTLED — to też jest z definicji przejściowe."""
    executor = AsyncMock()
    executor.async_apply.side_effect = [
        GuardResult(params={}, status=Status.THROTTLED),
        GuardResult(params={"mode": "eco_charge"}, status=Status.SUCCESS, executed=["mode"]),
    ]
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-throttle", "params": {"mode": "eco_charge"}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_apply.await_count == 2, "THROTTLED nie może zablokować retry"


@pytest.mark.asyncio
async def test_r2_success_z_pustym_executed_nie_blokuje_retry():
    """SUCCESS z pustym `executed` (wszystko odfiltrował throttle I-6) to też
    przypadek "nic nie poszło" — nie wolno go traktować jak wykonaną komendę.
    """
    executor = AsyncMock()
    executor.async_apply.side_effect = [
        GuardResult(params={"eco_soc": 30.0}, status=Status.SUCCESS, executed=[]),
        GuardResult(params={"eco_soc": 30.0}, status=Status.SUCCESS, executed=["eco_soc"]),
    ]
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-empty", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_apply.await_count == 2, (
        "SUCCESS bez faktycznego zapisu (executed=[]) nie może zjeść retry"
    )


@pytest.mark.asyncio
async def test_r2_success_z_executed_jest_zapamietywany():
    """Kontrast: gdy komenda FAKTYCZNIE coś zapisała, dedup musi dalej działać (T-12)."""
    executor = AsyncMock()
    executor.async_apply.return_value = GuardResult(
        params={"eco_soc": 30.0}, status=Status.SUCCESS, executed=["eco_soc"]
    )
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-real", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_apply.await_count == 1, "prawdziwy zapis musi dalej blokować duplikaty"
    handler._report_result.assert_awaited_with("req-real", "duplicate")


# ── R-4: brak request_id nigdy nie deduplikuje ───────────────────────────────


@pytest.mark.asyncio
async def test_r4_brak_request_id_nigdy_nie_dedupikuje():
    """Ręczny broadcast bez `request_id` (smoke test Fazy C) nie może dzielić
    jednego wspólnego klucza z pierwszą taką komendą."""
    executor = AsyncMock()
    executor.async_apply.return_value = GuardResult(
        params={"eco_soc": 30.0}, status=Status.SUCCESS, executed=["eco_soc"]
    )
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "params": {"eco_soc": 30}}  # brak request_id
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_apply.await_count == 2, (
        "brak request_id -> None -> nigdy nie deduplikuj"
    )
    for call in handler._report_result.await_args_list:
        assert call.args[1] != "duplicate", "komenda bez request_id nie może zostać odrzucona jako duplikat"


# ── R-10: bariera na wyjątki, walidacja params ───────────────────────────────


@pytest.mark.asyncio
async def test_r10_params_niedict_nie_wybucha_i_zglasza_blad():
    """Scenariusz wprost z ustalenia R-10.

    `{'command':'SET_WORK_MODE','params':['eco_soc']}` przed naprawą wywalał
    `AttributeError` w `infer_action` (`params.get` na liście), wyjątek propagował dalej.
    """
    executor = AsyncMock()
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "params": ["eco_soc"]}

    # Nie może rzucić wyjątku — to jest cała istota R-10.
    await handler._execute_command(payload)

    executor.async_apply.assert_not_awaited()
    handler._report_result.assert_awaited_once()
    args, kwargs = handler._report_result.await_args
    assert args[1] == "error"
    assert kwargs.get("errors"), "musi być powód w errors"


@pytest.mark.asyncio
async def test_r10_wyjatek_w_egzekutorze_nie_propaguje():
    """Bariera try/except musi łapać KAŻDY wyjątek z treści komendy, nie tylko
    ten wywołany złym kształtem `params` — inaczej dowolny inny bug znów zrywa Realtime."""
    executor = AsyncMock()
    executor.async_apply.side_effect = RuntimeError("boom")
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-boom", "params": {"eco_soc": 30}}

    # Nie może propagować — inaczej _connect_and_listen zamyka WebSocket w finally.
    await handler._execute_command(payload)

    handler._report_result.assert_awaited_once()
    args, kwargs = handler._report_result.await_args
    assert args[0] == "req-boom"
    assert args[1] == "error"
    assert kwargs.get("errors"), "musi być powód w errors"
