"""Testy toru komend — idempotencja i routing."""

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


@pytest.mark.asyncio
async def test_n5_retry_po_bledzie_jest_wykonywany_ponownie():
    executor = AsyncMock()
    executor.async_apply.return_value = GuardResult(params={}, status=Status.ERROR)
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-1", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_apply.await_count == 2, "komenda zakonczona bledem musi byc retryowalna"


@pytest.mark.asyncio
async def test_t12_powtorka_po_sukcesie_jest_pomijana():
    executor = AsyncMock()
    executor.async_apply.return_value = GuardResult(params={"eco_soc": 30.0},
                                                    status=Status.SUCCESS)
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-2", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)
    await handler._execute_command(payload)

    assert executor.async_apply.await_count == 1
    handler._report_result.assert_awaited_with("req-2", "duplicate")
