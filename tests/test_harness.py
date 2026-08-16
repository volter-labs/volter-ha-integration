"""Sanity check harnessu: moduły zależne od HA dają się zaimportować bez HA."""

from __future__ import annotations


def test_mozna_zaimportowac_modul_zalezny_od_ha():
    from custom_components.volter.executor import VolterExecutor

    assert VolterExecutor is not None


def test_fake_hass_ma_states_services_i_bus(fake_hass):
    assert hasattr(fake_hass, "states")
    assert hasattr(fake_hass, "services")
    assert hasattr(fake_hass, "bus")
