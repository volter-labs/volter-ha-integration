# tests/test_config_flow.py
"""Options Flow musi zapisywać parametry, z których korzystają guardy."""

from __future__ import annotations

import pytest

from custom_components.volter.const import (
    DEFAULT_RATED_POWER_W,
    DEFAULT_SOC_RESERVE,
    OPT_RATED_POWER_W,
    OPT_SOC_RESERVE,
    OPT_USER_MODE,
)


@pytest.mark.asyncio
async def test_n2_krok_strategii_zapisuje_parametry_guardow(fake_entry):
    from custom_components.volter.config_flow import VolterOptionsFlow

    flow = VolterOptionsFlow(fake_entry)
    flow.async_create_entry = lambda *, data: {"type": "create_entry", "data": data}

    result = await flow.async_step_strategy(
        {OPT_SOC_RESERVE: 35.0, OPT_USER_MODE: "backup", OPT_RATED_POWER_W: 8000.0}
    )

    assert result["data"][OPT_SOC_RESERVE] == 35.0
    assert result["data"][OPT_USER_MODE] == "backup"
    assert result["data"][OPT_RATED_POWER_W] == 8000.0


def test_n2_defaulty_sa_spojne_ze_specyfikacja():
    assert DEFAULT_SOC_RESERVE == 20.0
    assert DEFAULT_RATED_POWER_W == 10000.0
