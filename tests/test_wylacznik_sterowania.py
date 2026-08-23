"""Wyłącznik sterowania — ostatnia brama przed dotknięciem falownika.

Powód istnienia: do czasu potwierdzenia mapowania trybów na żywym falowniku
(Etap 3) jedyną barierą przed pierwszym zapisem był PRZYPADEK — niezgodność
nazw trybów łapana przez I-10. Wyłącznik zamienia to w decyzję właściciela.

Te testy ustawiają opcję JAWNIE, więc autouse fixture z conftestu
(`_sterowanie_wlaczone_w_testach`) ich nie dotyczy.
"""

from __future__ import annotations

import pytest

from custom_components.volter import const
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


def _hass() -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set("select.tryb", "general",
                    {"options": ["general", "eco_charge", "eco_discharge"]})
    hass.states.set("number.eco_soc", "20")
    return hass


def test_produkcyjny_default_jest_wylaczony():
    """Bezpieczeństwo domyślne. Zmiana tej wartości to decyzja produktowa,
    nie refaktor — dlatego ma własny test, niezależny od podmianki z conftestu."""
    assert const.DEFAULT_CONTROL_ENABLED is False


@pytest.mark.asyncio
async def test_wylaczone_sterowanie_nie_dotyka_falownika(fake_entry):
    hass = _hass()
    fake_entry.options = {**OPTIONS, const.OPT_CONTROL_ENABLED: False}
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply({"eco_soc": 40.0}, source="test")

    assert hass.services.calls == [], "wyłączone sterowanie nie może wywołać ŻADNEGO serwisu"
    assert result.executed == []
    assert result.status is Status.THROTTLED
    assert any(n.invariant == "STEROWANIE" for n in result.notes)


@pytest.mark.asyncio
async def test_wlaczone_sterowanie_zapisuje(fake_entry):
    hass = _hass()
    fake_entry.options = {**OPTIONS, const.OPT_CONTROL_ENABLED: True}
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply({"eco_soc": 40.0}, source="test")

    assert result.status is Status.SUCCESS
    assert result.executed == ["eco_soc"]
    assert hass.services.calls, "włączone sterowanie musi zapisać"


@pytest.mark.asyncio
async def test_brak_opcji_znaczy_wylaczone(fake_entry, monkeypatch):
    """Instalacja sprzed tej wersji nie ma klucza w opcjach — ma być bezpieczna."""
    from custom_components.volter import executor as _executor

    monkeypatch.setattr(_executor, "DEFAULT_CONTROL_ENABLED", const.DEFAULT_CONTROL_ENABLED)
    hass = _hass()
    fake_entry.options = dict(OPTIONS)  # bez klucza control_enabled
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply({"eco_soc": 40.0}, source="test")

    assert hass.services.calls == []
    assert result.status is Status.THROTTLED


@pytest.mark.asyncio
async def test_nota_mowi_co_by_poszlo(fake_entry):
    """Wyłącznik odbiera prawo zapisu, nie widoczność — bez tego nie da się
    przygotować instalacji na sucho."""
    hass = _hass()
    fake_entry.options = {**OPTIONS, const.OPT_CONTROL_ENABLED: False}
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply({"eco_soc": 40.0}, source="test")

    nota = next(n for n in result.notes if n.invariant == "STEROWANIE")
    assert "eco_soc=40.0" in nota.message


@pytest.mark.asyncio
async def test_diagnose_dziala_przy_wylaczonym_sterowaniu(fake_entry):
    """`volter.diagnose` ma pokazywać decyzję niezależnie od wyłącznika."""
    hass = _hass()
    fake_entry.options = {**OPTIONS, const.OPT_CONTROL_ENABLED: False}
    executor = VolterExecutor(hass, fake_entry)

    report = await executor.async_diagnose()

    assert hass.services.calls == []
    assert report["state"]["soc"] == 55.0
