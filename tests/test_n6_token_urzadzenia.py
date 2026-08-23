"""N-6: tożsamość urządzenia dla prywatnych kanałów Realtime.

HA ma tylko klucz API. Kanały prywatne (migracja 060 w chmurze) wpuszczają
wyłącznie JWT z `auth.uid()` równym właścicielowi tematu. Token wydaje Edge
Function `device-token`; ten moduł go trzyma, odświeża przed wygaśnięciem
i degraduje do kanału publicznego, gdy chmura tokenu nie daje (przed cutoverem).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter.device_token import (
    REFRESH_MARGIN_S,
    DeviceToken,
    DeviceTokenProvider,
)

TERAZ = datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)


def test_token_swiezy_nie_wymaga_odswiezenia():
    t = DeviceToken("jwt", TERAZ + timedelta(hours=12))
    assert not t.needs_refresh(TERAZ)


def test_token_wymaga_odswiezenia_przed_wygasnieciem_z_marginesem():
    t = DeviceToken("jwt", TERAZ + timedelta(seconds=REFRESH_MARGIN_S - 1))
    assert t.needs_refresh(TERAZ)
    assert DeviceToken("jwt", TERAZ - timedelta(seconds=1)).needs_refresh(TERAZ)


def test_parsowanie_odpowiedzi_device_token():
    t = DeviceToken.from_response({
        "access_token": "a.b.c", "expires_at": "2026-08-24T06:00:00.000Z", "user_id": "u",
    })
    assert t.access_token == "a.b.c"
    assert t.expires_at == datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("zly", [
    {}, {"access_token": ""}, {"access_token": "x"},
    {"access_token": "x", "expires_at": "smiec"},
    {"access_token": 7, "expires_at": "2026-08-24T06:00:00Z"},
])
def test_zla_odpowiedz_to_none(zly):
    assert DeviceToken.from_response(zly) is None


class _Zegar:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.mark.asyncio
async def test_provider_pobiera_raz_i_cache_uje():
    wywolania = []

    async def pobierz():
        wywolania.append(1)
        return {"access_token": "jwt1", "expires_at": (TERAZ + timedelta(hours=12)).isoformat()}

    p = DeviceTokenProvider(pobierz, now=_Zegar(TERAZ))
    assert await p.async_get() == "jwt1"
    assert await p.async_get() == "jwt1"
    assert len(wywolania) == 1


@pytest.mark.asyncio
async def test_provider_odswieza_przed_wygasnieciem():
    n = 0
    zegar = _Zegar(TERAZ)

    async def pobierz():
        nonlocal n
        n += 1
        return {"access_token": f"jwt{n}", "expires_at": (zegar.t + timedelta(hours=1)).isoformat()}

    p = DeviceTokenProvider(pobierz, now=zegar)
    assert await p.async_get() == "jwt1"
    zegar.t = TERAZ + timedelta(hours=1) - timedelta(seconds=REFRESH_MARGIN_S // 2)
    assert await p.async_get() == "jwt2"


@pytest.mark.asyncio
async def test_provider_bez_chmury_zwraca_none_i_nie_rzuca():
    async def pobierz():
        raise ConnectionError("offline")

    p = DeviceTokenProvider(pobierz, now=_Zegar(TERAZ))
    assert await p.async_get() is None


@pytest.mark.asyncio
async def test_provider_trzyma_stary_token_gdy_odswiezenie_padnie_a_token_jeszcze_zyje():
    stan = {"n": 0}

    async def pobierz():
        stan["n"] += 1
        if stan["n"] > 1:
            raise ConnectionError("offline")
        return {"access_token": "jwt1", "expires_at": (TERAZ + timedelta(hours=1)).isoformat()}

    zegar = _Zegar(TERAZ)
    p = DeviceTokenProvider(pobierz, now=zegar)
    assert await p.async_get() == "jwt1"
    zegar.t = TERAZ + timedelta(hours=1) - timedelta(seconds=REFRESH_MARGIN_S // 2)
    # Odświeżenie padło, ale token formalnie jeszcze żyje — lepszy stary niż żaden.
    assert await p.async_get() == "jwt1"
    zegar.t = TERAZ + timedelta(hours=2)
    assert await p.async_get() is None


def test_changed_since_wykrywa_nowy_token():
    p = DeviceTokenProvider(lambda: None, now=_Zegar(TERAZ))
    p._token = DeviceToken("jwt1", TERAZ + timedelta(hours=1))
    assert p.changed_since("jwt0")
    assert not p.changed_since("jwt1")


# --- Kontrakt Phoenix: join prywatny z tokenem, publiczny bez ------------------

from unittest.mock import AsyncMock  # noqa: E402

from custom_components.volter.command_handler import VolterCommandHandler  # noqa: E402
from tests.conftest import FakeHass  # noqa: E402


def _handler(provider) -> VolterCommandHandler:
    entry = type("E", (), {"options": {"entity_ems_mode": "select.tryb"}})()
    h = VolterCommandHandler(
        hass=FakeHass(), entry=entry, device_id="dev-1",
        supabase_url="https://example.supabase.co",
        anon_key="anon", api_key="vk_test", executor=AsyncMock(),
        token_provider=provider,
    )
    return h


@pytest.mark.asyncio
async def test_join_z_tokenem_jest_prywatny():
    async def pobierz():
        return {"access_token": "jwt1", "expires_at": (TERAZ + timedelta(hours=12)).isoformat()}

    h = _handler(DeviceTokenProvider(pobierz, now=_Zegar(TERAZ)))
    msg = await h._join_message()
    assert msg["event"] == "phx_join"
    assert msg["topic"] == "realtime:device:dev-1"
    assert msg["payload"]["config"]["private"] is True
    assert msg["payload"]["access_token"] == "jwt1"


@pytest.mark.asyncio
async def test_join_bez_tokenu_degraduje_do_publicznego():
    async def pobierz():
        raise ConnectionError("brak funkcji")

    h = _handler(DeviceTokenProvider(pobierz, now=_Zegar(TERAZ)))
    msg = await h._join_message()
    assert msg["payload"]["config"].get("private", False) is False
    assert "access_token" not in msg["payload"]


@pytest.mark.asyncio
async def test_odswiezony_token_idzie_na_kanal_zdarzeniem_access_token():
    n = 0
    zegar = _Zegar(TERAZ)

    async def pobierz():
        nonlocal n
        n += 1
        return {"access_token": f"jwt{n}", "expires_at": (zegar.t + timedelta(hours=1)).isoformat()}

    h = _handler(DeviceTokenProvider(pobierz, now=zegar))
    await h._join_message()
    wyslane = []
    h._send_json = AsyncMock(side_effect=lambda d: wyslane.append(d))

    await h._refresh_channel_token()
    assert wyslane == []  # token bez zmian — cisza

    zegar.t = TERAZ + timedelta(hours=1) - timedelta(seconds=REFRESH_MARGIN_S // 2)
    await h._refresh_channel_token()
    assert wyslane[-1]["event"] == "access_token"
    assert wyslane[-1]["payload"] == {"access_token": "jwt2"}
    assert wyslane[-1]["topic"] == "realtime:device:dev-1"
