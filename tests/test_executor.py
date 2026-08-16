"""Testy wspólnej bramy zapisu."""

from __future__ import annotations

import pytest

from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Status
from tests.conftest import FakeHass


OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
}


def _hass_ze_swiezym_stanem() -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set("select.tryb", "general",
                    {"options": ["general", "eco_charge", "eco_discharge"]})
    hass.states.set("number.eco_soc", "20")
    return hass


@pytest.mark.asyncio
async def test_n4_executed_zawiera_tylko_faktycznie_zapisane(fake_entry):
    hass = _hass_ze_swiezym_stanem()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply({"eco_soc": 40.0}, source="test")

    assert result.status is Status.SUCCESS
    assert result.executed == ["eco_soc"]


@pytest.mark.asyncio
async def test_n4_throttle_nie_trafia_do_executed(fake_entry):
    """Drugi zapis tej samej wartości jest pominięty — nie wolno go raportować."""
    hass = _hass_ze_swiezym_stanem()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    await executor.async_apply({"eco_soc": 40.0}, source="test")
    result = await executor.async_apply({"eco_soc": 40.0}, source="test")

    assert result.executed == []
    assert any(n.invariant == "I-6" for n in result.notes)


@pytest.mark.asyncio
async def test_n4_blad_zapisu_nie_trafia_do_executed(fake_entry):
    hass = _hass_ze_swiezym_stanem()
    hass.services.fail_with = RuntimeError("falownik nie odpowiada")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply({"eco_soc": 40.0}, source="test")

    assert result.executed == []
    assert result.status is Status.ERROR


@pytest.mark.asyncio
async def test_zmiana_decyzji_loguje_info_powtorka_debug(fake_entry, caplog):
    import logging

    hass = _hass_ze_swiezym_stanem()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    with caplog.at_level(logging.INFO, logger="custom_components.volter.executor"):
        await executor.async_apply({"eco_soc": 40.0}, source="schedule")
        pierwszy = [r for r in caplog.records if r.levelno == logging.INFO]

        caplog.clear()
        await executor.async_apply({"eco_soc": 40.0}, source="schedule")
        drugi = [r for r in caplog.records if r.levelno == logging.INFO]

    assert pierwszy, "pierwsza decyzja musi byc na INFO"
    assert not drugi, "powtorzona decyzja nie moze smiecic w logu"
