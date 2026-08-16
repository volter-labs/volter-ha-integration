"""U-8 / U-9 — aneks „regresja po Tasku 13" w `docs/analysis/2026-08-16-kontrakt-mocy.md`.

**U-8 (ŚREDNIA):** `fetcher.py` — strażnik `if schedule_id and schedule_id == self._last_schedule_id`
nigdy nie działa dla LEGALNEGO pustego planu, bo `get-schedule` zwraca dla niego
`schedule_id=""` (kontrakt `schedule-assembly.ts::emptySchedule`), a pusty string jest
falsy. Zmierzone: 288 zapisów do `helpers.storage` na dobę w stanie, który może trwać
bezterminowo (optymalizator wyłączony) — dosłowne zaprzeczenie obietnicy z docstringu
modułu ("chroni Store przed zapisem co 5 min bez powodu").

**U-9 (ŚREDNIA):** `schedule.py::kierunek_slotu` (dawniej `mappers._kierunek`) wyprowadza
kierunek z pól opisowych BEZ sprawdzenia zgodności z `mode`. Zmierzone: slot
`{mode: 'idle', discharge_purpose: 'self', power_w: 1476}` → `eco_discharge` + `eco_power`,
czyli slot opisany jako „nie rób nic" wydaje komendę rozładowania. Ta sama klasa luki co
S-2: niezaufany JSON z chmury przechodzący przez parser do falownika, tylko na nowych
polach U-1. Naprawa: sprzeczna kombinacja pól jest ZŁYM KSZTAŁTEM wejścia i musi zostać
odrzucona PRZY PARSOWANIU (`Slot.from_dict`), fail-closed — jedyne miejsce, które i tak
już waliduje `mode`/`charge_source`/`discharge_purpose` osobno.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.volter.fetcher import ScheduleFetcher
from custom_components.volter.schedule import InvalidSchedule, Schedule, Slot

# ── U-8: fetcher a pusty plan ────────────────────────────────────────────────

#: Kształt `get-schedule` (Apka1) dla optymalizatora wyłączonego / braku planu —
#: `schedule-assembly.ts::emptySchedule`. `schedule_id`/`generated_at` puste (nie
#: `null`), bo `toDeviceSchedule` nie ma z czego ich wziąć przy zerowych slotach.
PUSTY_PLAN = {
    "schedule_id": "",
    "generated_at": "",
    "slots": [],
    "fallback": {"mode": "self_consume", "soc_reserve": 20},
}

NIEPUSTY_PLAN = {
    "schedule_id": "abc",
    "generated_at": "2026-08-16T13:30:00Z",
    "slots": [{"from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
               "mode": "charge", "charge_source": "grid", "soc_target": 80}],
    "fallback": {"mode": "self_consume", "soc_reserve": 20},
}


@pytest.mark.asyncio
async def test_u8_pusty_plan_powtorzony_nie_zapisuje_store_wielokrotnie(fake_hass, fake_entry):
    """SONDA U-8 (odtworzenie): pięć kolejnych tików tego samego legalnego pustego
    planu ma dać JEDEN zapis do Store, nie pięć — na produkcji to różnica między
    1 a 288 zapisami na dobę, bezterminowo, dopóki optymalizator jest wyłączony."""
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(return_value=PUSTY_PLAN)

    for _ in range(5):
        await fetcher.async_refresh()

    assert executor.async_set_schedule.await_count == 1, (
        "U-8: pusty schedule_id jest falsy, więc strażnik `schedule_id and ...` "
        "nie ma prawa zablokować deduplikacji — musi zadziałać mimo to"
    )


@pytest.mark.asyncio
async def test_u8_przejscie_z_niepustego_planu_na_pusty_zapisuje_fallback(fake_hass, fake_entry):
    """Strona lustrzana: przejście „plan realny → optymalizator wyłączony" MUSI
    dotrzeć do executora, inaczej urządzenie nigdy nie wejdzie w fallback I-5."""
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(side_effect=[NIEPUSTY_PLAN, PUSTY_PLAN])

    await fetcher.async_refresh()
    await fetcher.async_refresh()

    assert executor.async_set_schedule.await_count == 2, (
        "przejście niepusty→pusty nie może zostać zdławione przez strażnik dedup"
    )


@pytest.mark.asyncio
async def test_u8_pusty_plan_o_innej_tresci_zapisuje_ponownie(fake_hass, fake_entry):
    """Rozwiązanie porównujące po `schedule_id` samo w sobie nie wystarczy — dwa
    RÓŻNE puste plany (np. inna `fallback.soc_reserve`) mają zawsze ten sam pusty
    id, więc dedup po treści musi dostrzec zmianę, a nie tylko zbieżność id."""
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    inny_pusty = {**PUSTY_PLAN, "fallback": {"mode": "self_consume", "soc_reserve": 40}}
    fetcher._fetch = AsyncMock(side_effect=[PUSTY_PLAN, inny_pusty])

    await fetcher.async_refresh()
    await fetcher.async_refresh()

    assert executor.async_set_schedule.await_count == 2, (
        "dedup po pustym id nie może zjeść realnej zmiany treści pustego planu"
    )


@pytest.mark.asyncio
async def test_u8_pusty_plan_po_pustym_ta_sama_tresc_ale_niezmieniony_fallback_dedup(
    fake_hass, fake_entry
):
    """Kontrola pozytywna do testu wyżej: identyczna treść dwa razy pod rząd nadal
    ma zostać zdedupowana — to samo ustalenie, tylko z drugiej strony."""
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(side_effect=[dict(PUSTY_PLAN), dict(PUSTY_PLAN)])

    await fetcher.async_refresh()
    await fetcher.async_refresh()

    assert executor.async_set_schedule.await_count == 1


# ── U-9: spójność pól kierunku przy parsowaniu ───────────────────────────────

_BAZA = {"from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z"}


@pytest.mark.parametrize(
    "pola,oczekiwane_pole",
    [
        # SONDA U-9 (odtworzenie 1:1): "nie rób nic" wydające komendę rozładowania.
        ({"mode": "idle", "discharge_purpose": "self", "power_w": 1476}, "mode"),
        ({"mode": "idle", "charge_source": "grid"}, "mode"),
        # Analogicznie po stronie ładowania: mode=charge cicho gubi discharge_purpose.
        ({"mode": "charge", "discharge_purpose": "self", "power_w": 3000}, "discharge_purpose"),
        ({"mode": "discharge", "charge_source": "grid", "power_w": 3000}, "charge_source"),
        # Oba pola opisowe naraz to dwa sprzeczne kierunki w jednym slocie.
        (
            {"mode": "self_consume", "charge_source": "grid", "discharge_purpose": "self"},
            "charge_source",
        ),
    ],
)
def test_u9_sprzeczna_kombinacja_pol_odrzuca_slot_przy_parsowaniu(pola, oczekiwane_pole):
    with pytest.raises(InvalidSchedule) as err:
        Slot.from_dict({**_BAZA, **pola})

    assert err.value.field == oczekiwane_pole


def test_u9_sprzeczna_kombinacja_uniewaznia_caly_harmonogram():
    """S-2 fail-closed obowiązuje tak samo dla tej klasy błędu: jeden zły slot = brak planu."""
    with pytest.raises(InvalidSchedule) as err:
        Schedule.from_dict({
            "schedule_id": "u9",
            "slots": [
                {**_BAZA, "mode": "self_consume"},
                {"from": "2026-08-16T23:00:00Z", "to": "2026-08-17T00:00:00Z",
                 "mode": "idle", "discharge_purpose": "self", "power_w": 1476},
            ],
        })

    assert err.value.field == "slots[1].mode"


@pytest.mark.parametrize(
    "pola",
    [
        {"mode": "self_consume", "discharge_purpose": "self", "power_w": 1476},
        {"mode": "self_consume", "charge_source": "grid", "power_w": 1200},
        {"mode": "charge", "charge_source": "grid", "power_w": 3000},
        {"mode": "discharge", "discharge_purpose": "sell", "power_w": 5000},
        {"mode": "idle"},
    ],
)
def test_u9_zgodne_kombinacje_pol_dalej_przechodza_parsowanie(pola):
    """Kontrola pozytywna: naprawa nie ma prawa zdusić ŻADNEGO z legalnych kształtów
    kontraktu U-1 — to te same kombinacje, które produkuje `device-schedule.ts`."""
    slot = Slot.from_dict({**_BAZA, **pola})
    assert slot.action.value == pola["mode"]
