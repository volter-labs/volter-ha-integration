"""Rejestracja serwisów integracji."""

from __future__ import annotations

import pytest

from custom_components.volter.const import DOMAIN
from tests.conftest import FakeHass


@pytest.mark.asyncio
async def test_serwis_diagnose_jest_rejestrowany(fake_entry):
    from custom_components.volter import _async_register_services

    hass = FakeHass()
    await _async_register_services(hass)

    assert hass.services.has_service(DOMAIN, "diagnose")
