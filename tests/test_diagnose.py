"""Suchy przebieg: pokaż decyzję, nie zapisuj nic."""

from __future__ import annotations

import pytest

from custom_components.volter.executor import VolterExecutor
from tests.conftest import FakeHass

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
}


@pytest.mark.asyncio
async def test_diagnose_nie_zapisuje_nic(fake_entry):
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set("select.tryb", "auto", {"options": ["auto", "charge_battery"]})
    fake_entry.options = dict(OPTIONS)

    report = await VolterExecutor(hass, fake_entry).async_diagnose()

    assert hass.services.calls == [], "diagnose nie moze wolac zadnego serwisu"
    assert report["state"]["soc"] == 55.0


@pytest.mark.asyncio
async def test_diagnose_wypisuje_realne_allowed_modes(fake_entry):
    """To weryfikuje hipoteze z mappers.py przed pierwszym zapisem."""
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("select.tryb", "auto",
                    {"options": ["auto", "charge_battery", "discharge_battery", "backup"]})
    fake_entry.options = dict(OPTIONS)

    report = await VolterExecutor(hass, fake_entry).async_diagnose()

    assert report["limits"]["allowed_modes"] == [
        "auto", "charge_battery", "discharge_battery", "backup"
    ]


@pytest.mark.asyncio
async def test_diagnose_pokazuje_ktory_inwariant_by_zablokowal(fake_entry):
    """Przestarzały odczyt -> I-9. To jest odpowiedź na 'dlaczego nic się nie dzieje'."""
    from datetime import datetime, timedelta, timezone

    hass = FakeHass()
    stary = datetime.now(timezone.utc) - timedelta(minutes=30)
    hass.states.set("sensor.soc", "55", last_updated=stary)
    hass.states.set("sensor.pv", "0", last_updated=stary)
    hass.states.set("sensor.grid", "0", last_updated=stary)
    fake_entry.options = dict(OPTIONS)

    report = await VolterExecutor(hass, fake_entry).async_diagnose()

    assert report["guards"]["status"] == "degraded"
    assert any(n["invariant"] == "I-9" for n in report["guards"]["notes"])
    assert report["would_write"] == {}
