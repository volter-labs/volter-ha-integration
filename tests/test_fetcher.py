"""Pobieranie planu z chmury — pull, odporny na offline (Task 16, Faza D).

Kontekst z audytu (aneks rundy 3 audytu Fazy A + kontrola chmury Task 15):
  * `get-schedule` zwraca 200 z PUSTĄ listą slotów, gdy optymalizator jest wyłączony
    albo nie ma planu — to jest LEGALNA odpowiedź „nie mam planu", nie zły kształt.
  * Błąd sieci NIE MOŻE kasować planu lokalnego — urządzenie jedzie dalej na tym,
    co ma, a I-5 zadziała dopiero po wygaśnięciu slotów.
  * 500 (błąd bazy po stronie chmury) trzeba traktować jak błąd sieci — świadome
    rozróżnienie chmury „brak planu" (200) od „nie wiem" (500).
  * Strażnik `schedule_id` chroni `helpers.storage` przed zapisem co 5 min bez
    powodu — `executor.async_set_schedule` zapisuje bezwarunkowo (RR-4 tego
    wymaga po stronie command_handlera), więc to fetcher jest jedyną warstwą.
  * Odpowiedź może zawierać sloty z przeszłości (do ~48h wstecz) w dowolnej
    kolejności — `schedule.py` musi wybierać slot po czasie, nie po pozycji
    w tablicy (ustalenie J, kontrola chmury).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.volter.fetcher import ScheduleFetcher

PLAN = {
    "schedule_id": "abc",
    "generated_at": "2026-08-16T13:30:00Z",
    "slots": [{"from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
               "mode": "charge", "charge_source": "grid",
               "soc_target": 80, "export_allowed": False}],
    "fallback": {"mode": "self_consume", "soc_reserve": 20},
}

#: Kształt, w jakim `get-schedule` (Apka1) odpowiada, gdy optymalizator jest
#: wyłączony albo nie ma jeszcze planu — `schedule-assembly.ts::emptySchedule`.
#: `generated_at`/`schedule_id` puste (nie `null`), bo `toDeviceSchedule` nie ma
#: z czego ich wziąć przy zerowych slotach.
PUSTY_PLAN_Z_CHMURY = {
    "schedule_id": "",
    "generated_at": "",
    "slots": [],
    "fallback": {"mode": "self_consume", "soc_reserve": 20},
}


# ── podstawowy obieg: plan trafia do executora ───────────────────────────────


@pytest.mark.asyncio
async def test_pobrany_plan_trafia_do_executora(fake_hass, fake_entry):
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(return_value=PLAN)

    await fetcher.async_refresh()

    executor.async_set_schedule.assert_awaited_once_with(PLAN)


@pytest.mark.asyncio
async def test_niezmieniony_schedule_id_nie_jest_zapisywany_ponownie(fake_hass, fake_entry):
    """Ochrona helpers.storage przed zapisem co 5 min bez powodu."""
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(return_value=PLAN)

    await fetcher.async_refresh()
    await fetcher.async_refresh()

    assert executor.async_set_schedule.await_count == 1


@pytest.mark.asyncio
async def test_zmieniony_schedule_id_jest_zapisywany_ponownie(fake_hass, fake_entry):
    """Strona lustrzana: gdy plan NAPRAWDĘ się zmienił, strażnik nie może
    zablokować zapisu — inaczej replan reaktora nigdy by nie dotarł do falownika."""
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    inny_plan = {**PLAN, "schedule_id": "xyz"}
    fetcher._fetch = AsyncMock(side_effect=[PLAN, inny_plan])

    await fetcher.async_refresh()
    await fetcher.async_refresh()

    assert executor.async_set_schedule.await_count == 2


# ── błąd sieci / 500: plan lokalny przeżywa ──────────────────────────────────


@pytest.mark.asyncio
async def test_blad_sieci_nie_kasuje_planu_lokalnego(fake_hass, fake_entry):
    """Utrata łącza nie może zostawić urządzenia bez planu — I-5 ma zadziałać dopiero
    po wygaśnięciu slotów, nie po pierwszym timeoucie."""
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(side_effect=TimeoutError("brak lacza"))

    await fetcher.async_refresh()

    executor.async_set_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_blad_sieci_nie_rusza_realnego_planu_zaladowanego_w_executorze(
    fake_hass, fake_entry
):
    """Wariant jawny (nie na mocku): plan RAPORTUJE się w prawdziwym executorze
    (Store + `self._schedule`), potem `_fetch` zaczyna rzucać timeouty — plan
    w executorze musi zostać nietknięty aż do wygaśnięcia slotów."""
    from custom_components.volter.executor import VolterExecutor

    fake_entry.options = {}
    executor = VolterExecutor(fake_hass, fake_entry)
    await executor.async_start()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)

    fetcher._fetch = AsyncMock(return_value=PLAN)
    await fetcher.async_refresh()
    assert executor._schedule is not None
    assert executor._schedule.schedule_id == "abc"

    fetcher._fetch = AsyncMock(side_effect=TimeoutError("brak lacza"))
    await fetcher.async_refresh()

    assert executor._schedule is not None
    assert executor._schedule.schedule_id == "abc", "blad sieci nie moze skasowac planu lokalnego"


@pytest.mark.asyncio
async def test_500_traktowany_jak_blad_sieci(fake_hass, fake_entry):
    """S-2/S-3: 500 to świadome rozróżnienie chmury „nie wiem" (błąd bazy) od
    „brak planu" (200, puste slots) — traktujemy je jak każdy inny błąd sieci."""
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)

    class _FakeResp:
        status = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):  # pragma: no cover — nie powinno być wołane
            raise AssertionError("nie wolno parsować body odpowiedzi 500")

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, *a, **kw):
            return _FakeResp()

    import custom_components.volter.fetcher as fetcher_mod

    original = fetcher_mod.aiohttp.ClientSession
    fetcher_mod.aiohttp.ClientSession = _FakeSession
    try:
        plan = await fetcher._fetch()
    finally:
        fetcher_mod.aiohttp.ClientSession = original

    assert plan is None

    # I na poziomie async_refresh: 500 nie może wywołać executora.
    fetcher._fetch = AsyncMock(return_value=None)
    await fetcher.async_refresh()
    executor.async_set_schedule.assert_not_awaited()


# ── pusty plan (D-6): legalna odpowiedź, nie zły kształt ─────────────────────


@pytest.mark.asyncio
async def test_pusty_plan_z_chmury_nie_jest_odrzucany_jako_zly_ksztalt(fake_hass, fake_entry):
    """D-6: `get-schedule` odpowiada `generated_at=""` dla legalnego pustego planu
    (optymalizator wyłączony). `Schedule.from_dict` traktowało pusty string jak
    zepsuty znacznik czasu i odrzucało CAŁY harmonogram (S-2 fail-closed) — czyli
    urządzenie NIGDY nie dowiadywało się, że ma wejść w fallback I-5."""
    from custom_components.volter.executor import VolterExecutor

    fake_entry.options = {}
    executor = VolterExecutor(fake_hass, fake_entry)
    await executor.async_start()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(return_value=PUSTY_PLAN_Z_CHMURY)

    await fetcher.async_refresh()  # nie wolno rzucić wyjątkiem

    assert executor._schedule is not None
    assert executor._schedule.slots == []


@pytest.mark.asyncio
async def test_zly_ksztalt_planu_nie_wywala_petli_pobierania(fake_hass, fake_entry):
    """Strona przeciwna do D-6: PRAWDZIWIE zepsuta odpowiedź (nie legalny pusty
    plan) dalej musi zostać odrzucona przez S-2 — ale odrzucenie nie może wywalić
    `async_refresh` poza fetcher, bo wtedy jedna zła odpowiedź wyłączałaby
    pobieranie na resztę sesji HA."""
    from custom_components.volter.executor import VolterExecutor

    fake_entry.options = {}
    executor = VolterExecutor(fake_hass, fake_entry)
    await executor.async_start()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(return_value={
        "schedule_id": "zepsuty",
        "generated_at": "nie-jest-data",
        "slots": [{"from": "tez-nie-jest-data", "to": "2026-08-16T23:00:00Z", "mode": "charge"}],
        "fallback": {"mode": "self_consume", "soc_reserve": 20},
    })

    await fetcher.async_refresh()  # nie wolno rzucić wyjątkiem

    assert executor._schedule is None, "zepsuty plan nie mial prawa zostac przyjety"


# ── ustalenie J: slot wybierany po czasie, nie po pozycji ────────────────────


@pytest.mark.asyncio
async def test_slot_biezacy_wybierany_po_czasie_nie_po_pozycji_w_liscie(fake_hass, fake_entry):
    """Odpowiedź może nieść sloty sprzed ~48h w dowolnej kolejności (plan na
    wczoraj + plan na dziś sklejone przez `get-schedule`). Slot „teraz" jest
    tu celowo NA KOŃCU listy i otoczony słotem przyszłym i słotem sprzed 47h —
    wybór musi iść po czasie (`covers()`), nie po indeksie w tablicy."""
    from custom_components.volter.executor import VolterExecutor

    def iso(dt: datetime) -> str:
        return dt.isoformat()

    now = datetime.now(timezone.utc)
    plan = {
        "schedule_id": "kolejnosc-pomieszana",
        "generated_at": iso(now),
        "slots": [
            {"from": iso(now + timedelta(hours=2)), "to": iso(now + timedelta(hours=3)),
             "mode": "idle"},
            {"from": iso(now - timedelta(hours=47)), "to": iso(now - timedelta(hours=46)),
             "mode": "charge", "charge_source": "grid"},
            {"from": iso(now - timedelta(minutes=30)), "to": iso(now + timedelta(minutes=30)),
             "mode": "discharge", "discharge_purpose": "self"},
        ],
        "fallback": {"mode": "self_consume", "soc_reserve": 20},
    }

    fake_entry.options = {}
    executor = VolterExecutor(fake_hass, fake_entry)
    await executor.async_start()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(return_value=plan)

    await fetcher.async_refresh()

    slot, is_fallback = executor._schedule.effective_slot(now)
    assert is_fallback is False
    assert slot.action.value == "discharge"
    assert slot.discharge_purpose == "self"


# ── cykl życia: start/stop bez wiszących zadań (S-5) ─────────────────────────


@pytest.mark.asyncio
async def test_async_start_odpytuje_od_razu_i_planuje_interwal(fake_hass, fake_entry):
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(return_value=PLAN)

    await fetcher.async_start()

    executor.async_set_schedule.assert_awaited_once_with(PLAN)
    assert fetcher._unsub is not None


@pytest.mark.asyncio
async def test_async_stop_odsubskrybowuje_timer(fake_hass, fake_entry):
    """S-5: fetcher nie ma czego pilnować poza subskrypcją timera — sam fetch nie
    zostawia żadnego zadania w locie. `async_stop` musi mimo to odsubskrybować,
    inaczej HA wołałaby callback po wyładowaniu integracji."""
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)
    fetcher._fetch = AsyncMock(return_value=None)

    await fetcher.async_start()
    unsub = fetcher._unsub
    assert unsub is not None

    await fetcher.async_stop()

    unsub.assert_called_once()
    assert fetcher._unsub is None


@pytest.mark.asyncio
async def test_async_stop_jest_bezpieczny_bez_uprzedniego_startu(fake_hass, fake_entry):
    executor = AsyncMock()
    fetcher = ScheduleFetcher(fake_hass, fake_entry, "https://x.supabase.co", "vk_t", executor)

    await fetcher.async_stop()  # nie wolno rzucić wyjątkiem
