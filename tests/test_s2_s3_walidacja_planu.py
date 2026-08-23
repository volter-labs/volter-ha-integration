"""S-2 i S-3 — zatruty plan przeżywa restart; jedna zła wiadomość kasuje plan.

Ustalenia z audytu świeżym okiem (`docs/analysis/2026-08-16-faza-a-audyt-swiezym-okiem.md`).

**S-2 (KRYTYCZNA)** — `async_set_schedule` UTRWALA plan w `Store` przed jakąkolwiek
walidacją typów, bo `Slot.from_dict` przepisuje `power_w`/`soc_target`/`price_pln_kwh`
z JSON-a bez kontroli. Sonda C: slot `{'price_pln_kwh': '0.45'}` (liczba jako string)
wywala `TypeError` na I-4, ale zatruty plan JEST JUŻ w `Store`. Po restarcie HA każdy
tick rzuca wyjątkiem, zapisy do falownika = `[]`, i urządzenie NIE WCHODZI w fallback
I-5 — bo wyjątek leci przed decyzją. Najgorszy tryb awarii: cichy, trwały, przeżywa
restart.

**S-3 (WYSOKA)** — `command_handler` wybierał harmonogram przez
`payload.get('schedule') or params.get('schedule') or params`, więc plan pod innym
kluczem przekazywał do `Schedule.from_dict` DOWOLNY dict, a ta akceptowała brak
`slots` i budowała PUSTY harmonogram. Sonda D: aktywny plan skasowany, pustka
utrwalona, raport do chmury `success`, a RR-4 zapamiętuje `request_id` — więc
retransmisja poprawnej wersji już nie pomoże.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.volter.command_handler import VolterCommandHandler
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Action
from custom_components.volter.schedule import Schedule, Slot
from tests.conftest import FakeHass

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_eco_mode_power": "number.eco_power",
    "entity_charge_limit": "number.charge_limit",
    "entity_soc_upper": "number.soc_upper",
    "entity_discharge_limit": "number.discharge_limit",
    "entity_export_limit_switch": "switch.export_limit",
    "soc_reserve": 20.0,
    "user_mode": "autarky",
}


def _hass(soc: str = "60") -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", soc)
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb",
        "auto",
        {"options": ["auto", "charge_battery", "discharge_battery", "backup"]},
    )
    hass.states.set("number.eco_soc", "20")
    hass.states.set("number.eco_power", "0")
    hass.states.set("number.discharge_limit", "0")
    return hass


def _slot(przesuniecie_min: float = -10.0, dlugosc_min: float = 60.0, **nadpisz) -> dict:
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=przesuniecie_min)
    slot = {
        "from": start.isoformat(),
        "to": (start + timedelta(minutes=dlugosc_min)).isoformat(),
        "mode": "self_consume",
        "power_w": 3000.0,
        "soc_target": 50.0,
        "price_pln_kwh": 0.45,
        "export_allowed": True,
    }
    slot.update(nadpisz)
    return slot


def _plan(schedule_id: str = "s1", slots: list[dict] | None = None) -> dict:
    return {
        "schedule_id": schedule_id,
        "slots": [_slot()] if slots is None else slots,
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    }


# ═══════════════════════════════════════════════════════════════════════════
# S-2 — sonda C: zatruty plan
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_s2_sonda_c_cena_jako_string_nie_trafia_do_store(fake_entry):
    """Sonda C wprost: `{'price_pln_kwh': '0.45'}` musi być ODRZUCONE przed Store.

    Przed naprawą: `Slot.from_dict` przepisywał string bez kontroli, `async_set_schedule`
    zapisywał plan do `Store`, a dopiero potem I-4 wywalało `TypeError` — zatruty plan
    zostawał utrwalony i przeżywał restart.
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    with pytest.raises(ValueError):
        await executor.async_set_schedule(_plan(slots=[_slot(price_pln_kwh="0.45")]))

    assert await executor._store.async_load() is None, (
        "S-2: zatruty plan trafił do pamięci trwałej — przeżyje restart HA"
    )
    assert executor._schedule is None, (
        "S-2: zatruty plan podmienił aktywny harmonogram w pamięci procesu"
    )


@pytest.mark.asyncio
async def test_s2_zly_slot_odrzuca_caly_harmonogram_a_nie_czesc(fake_entry):
    """Fail-closed jak `sanitize_params` (I-10): zły slot unieważnia CAŁY plan.

    Wariant szczególnie podstępny: zatruty jest slot PRZYSZŁY, więc przed naprawą
    komenda kończyła się cichym sukcesem (bieżący slot był poprawny), a plan wybuchał
    dopiero za kilka godzin — już po restarcie, bez związku z komendą, która go wniosła.
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    await executor.async_set_schedule(_plan("dobry"))

    with pytest.raises(ValueError):
        await executor.async_set_schedule(
            _plan(
                "mieszany",
                slots=[_slot(), _slot(przesuniecie_min=60.0, soc_target="80")],
            )
        )

    zapisany = await executor._store.async_load()
    assert zapisany is not None and zapisany["schedule_id"] == "dobry", (
        "S-2: częściowo zatruty plan nadpisał poprawny harmonogram w Store"
    )
    assert executor._schedule is not None and executor._schedule.schedule_id == "dobry"


@pytest.mark.asyncio
async def test_s2_export_allowed_jako_string_nie_wlacza_po_cichu_eksportu(fake_entry):
    """`bool('false') == True` — string „false" z chmury odblokowywał eksport.

    To ten sam brak walidacji typu co przy cenie, ale groźniejszy: nie rzuca wyjątkiem,
    tylko po cichu odwraca intencję planu (`export_allowed=False` → eksport dozwolony).
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    with pytest.raises(ValueError):
        await executor.async_set_schedule(_plan(slots=[_slot(export_allowed="false")]))

    assert await executor._store.async_load() is None


@pytest.mark.asyncio
async def test_s2_zatruty_plan_w_store_nie_blokuje_urzadzenia_po_restarcie(fake_entry):
    """Rdzeń S-2: plan zapisany przez STARĄ wersję nie może zabić nowego procesu.

    Symulacja restartu HA: `Store` zawiera już zatruty plan (`price_pln_kwh` jako
    string). Przed naprawą każdy tick nowego executora rzucał `TypeError`, zapisy do
    falownika = `[]`, i urządzenie stało bez decyzji — bo wyjątek leciał przed I-5.
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    executor._store._data = {
        "version": 1,
        "schedule_id": "zatruty",
        "generated_at": None,
        "slots": [_slot(price_pln_kwh="0.45")],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    }

    await executor.async_start()
    await executor._async_tick()

    assert hass.services.calls, (
        "S-2: po restarcie z zatrutym planem urządzenie nie zapisało NIC — "
        "wyjątek zjadł decyzję i nie doszło nawet do fallbacku I-5"
    )


@pytest.mark.asyncio
async def test_s2_wyjatek_w_torze_wykonania_prowadzi_do_fallbacku_a_nie_do_ciszy(
    fake_entry,
):
    """Niezależna warstwa (c): nawet jeśli coś przeciecze przez walidację.

    Zatruty `Slot` budowany jest tu WPROST (z pominięciem `Slot.from_dict`), żeby
    sprawdzić samą odporność pętli wykonawczej, a nie walidację parsera. Kontrakt:
    wyjątek w środku `_async_execute_now` nie może zostawić urządzenia bez decyzji —
    ma prowadzić do fallbacku I-5.
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    now = datetime.now(timezone.utc)
    executor._schedule = Schedule(
        schedule_id="zatruty",
        slots=[
            Slot(
                start=now - timedelta(minutes=10),
                end=now + timedelta(minutes=50),
                action=Action.SELF_CONSUME,
                soc_target=50.0,
                price_pln_kwh="0.45",  # type: ignore[arg-type]
            )
        ],
    )

    # `wait_for` pilnuje interakcji z S-1: awaryjny fallback woła `async_apply`,
    # czyli bierze `_write_lock` PONOWNIE. Gdyby kiedyś trafił pod ten sam zamek,
    # co przebieg, który właśnie wybuchł, dostaniemy czytelny timeout zamiast
    # zawieszonej na zawsze pętli wykonawczej HA.
    await asyncio.wait_for(executor._async_tick(), timeout=5.0)

    assert hass.services.calls, (
        "S-2c: wyjątek w torze wykonania zostawił urządzenie bez decyzji — "
        "musi prowadzić do fallbacku I-5, nie do ciszy"
    )
    zapisane_tryby = [
        call[2].get("option") for call in hass.services.calls if call[0] == "select"
    ]
    assert "auto" in zapisane_tryby, (
        f"awaryjny fallback musi ustawić tryb bezpieczny (self_consume), "
        f"a zapisano {zapisane_tryby}"
    )


@pytest.mark.asyncio
async def test_s2_brak_klucza_slots_to_blad_a_nie_pusty_plan():
    """`Schedule.from_dict` nie może budować pustego planu z dowolnego dicta.

    To jest wspólny korzeń S-2 i S-3: brak `slots` był interpretowany jako „plan bez
    slotów", więc każdy obcy dict przechodził jako legalny (pusty) harmonogram.
    """
    with pytest.raises(ValueError):
        Schedule.from_dict({"schedule_id": "x", "plan": {"sloty": []}})


def test_s2_poprawny_plan_przechodzi_i_zachowuje_wartosci():
    """Kontrola sensu: walidacja nie może odrzucać poprawnych planów.

    Pilnuje też round-tripu przez `to_dict`/`from_dict`, bo dokładnie tą drogą plan
    wraca z `Store` po restarcie.
    """
    plan = Schedule.from_dict(_plan())
    assert plan.slots[0].price_pln_kwh == 0.45
    assert plan.slots[0].soc_target == 50.0
    assert plan.slots[0].export_allowed is True

    powrot = Schedule.from_dict(plan.to_dict())
    assert powrot.to_dict() == plan.to_dict()


# ═══════════════════════════════════════════════════════════════════════════
# S-3 — sonda D: plan pod innym kluczem kasuje aktywny plan
# ═══════════════════════════════════════════════════════════════════════════


def _handler(executor: VolterExecutor) -> VolterCommandHandler:
    entry = type("E", (), {"options": {"entity_ems_mode": "select.tryb"}})()
    handler = VolterCommandHandler(
        hass=FakeHass(), entry=entry, device_id="dev-1",
        supabase_url="https://example.supabase.co",
        anon_key="anon", api_key="vk_test", executor=executor,
    )
    handler._report_result = AsyncMock()
    return handler


def _status_raportu(handler: VolterCommandHandler) -> str:
    return handler._report_result.await_args.args[1]


@pytest.mark.asyncio
async def test_s3_sonda_d_plan_pod_innym_kluczem_nie_kasuje_aktywnego(fake_entry):
    """Sonda D wprost: `params={'plan': {'slots': []}}` nie może skasować planu.

    Przed naprawą `raw = payload.get('schedule') or params.get('schedule') or params`
    przekazywało do `Schedule.from_dict` cały `params`, ta budowała PUSTY harmonogram,
    aktywny plan znikał ze Store, a do chmury szedł `success` — i RR-4 zapamiętywał
    `request_id`, więc retransmisja poprawnej wersji już nie pomagała.
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    await executor.async_set_schedule(_plan("s1"))
    handler = _handler(executor)

    await handler._execute_command(
        {
            "command": "SET_SCHEDULE",
            "request_id": "req-x",
            "params": {"plan": {"slots": []}},
        }
    )

    assert executor.diagnostics["schedule_id"] == "s1", "S-3: aktywny plan skasowany"
    assert executor.diagnostics["slots"] == 1
    zapisany = await executor._store.async_load()
    assert zapisany["schedule_id"] == "s1", "S-3: pustka utrwalona w Store"
    assert _status_raportu(handler) == "error", (
        "S-3: skasowanie planu zaraportowane do chmury jako sukces"
    )
    assert not handler._dedup.is_duplicate("req-x"), (
        "S-3: zły kształt zapamiętany w dedupie — retransmisja poprawnej wersji nie pomoże"
    )


@pytest.mark.asyncio
async def test_s3_schedule_pod_zlym_typem_nie_wpada_do_params(fake_entry):
    """`payload['schedule']` innego typu niż obiekt musi być błędem, nie fallbackiem.

    Stary łańcuch `or` traktował każdą wartość falsy/niepasującą jako „nie ma", więc
    cicho zsuwał się do `params` — czyli do dokładnie tego samego wektora co sonda D.
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    await executor.async_set_schedule(_plan("s1"))
    handler = _handler(executor)

    await handler._execute_command(
        {
            "command": "SET_SCHEDULE",
            "request_id": "req-lista",
            "schedule": [{"slots": []}],
            "params": {"slots": []},
        }
    )

    assert executor.diagnostics["schedule_id"] == "s1"
    assert _status_raportu(handler) == "error"


@pytest.mark.asyncio
async def test_s3_pusta_lista_slotow_jest_legalna_i_jawna(fake_entry):
    """DECYZJA: `{'slots': []}` to LEGALNY komunikat „nie mam dla ciebie planu".

    Odróżnienie od złego kształtu jest strukturalne, nie heurystyczne: liczy się
    OBECNOŚĆ klucza `slots`, a nie jego zawartość. Chmura, która chce wycofać plan,
    musi to powiedzieć wprost; chmura, która przysłała śmieć, nie ma jak tego udać.
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    await executor.async_set_schedule(_plan("s1"))
    handler = _handler(executor)

    await handler._execute_command(
        {
            "command": "SET_SCHEDULE",
            "request_id": "req-pusty",
            "schedule": {
                "schedule_id": "pusty",
                "slots": [],
                "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
            },
        }
    )

    assert executor.diagnostics["schedule_id"] == "pusty"
    assert executor.diagnostics["slots"] == 0
    assert _status_raportu(handler) in ("success", "partial")
    assert handler._dedup.is_duplicate("req-pusty"), (
        "jawne wycofanie planu to trwały skutek — RR-4 musi je zapamiętać"
    )


@pytest.mark.asyncio
async def test_s3_stary_kontrakt_params_jako_harmonogram_dalej_dziala(fake_entry):
    """Strona ochrony: `params` NIOSĄCE `slots` to nadal poprawny harmonogram.

    Wąski kontrakt nie może zerwać działającej ścieżki — rozpoznajemy ją po kluczu
    `slots`, tak samo jak legalną pustkę wyżej.
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    handler = _handler(executor)

    await handler._execute_command(
        {"command": "SET_SCHEDULE", "request_id": "req-legacy", "params": _plan("legacy")}
    )

    assert executor.diagnostics["schedule_id"] == "legacy"
    assert executor.diagnostics["slots"] == 1
    assert _status_raportu(handler) in ("success", "partial")


@pytest.mark.asyncio
async def test_s3_zatruty_slot_z_chmury_nie_kasuje_planu_i_zostaje_retryowalny(
    fake_entry,
):
    """Spięcie S-2 z S-3: zły slot przez pełną ścieżkę komendy z chmury.

    Aktywny plan musi przeżyć, raport ma być błędem, a `request_id` NIE może wpaść
    do dedupu — inaczej poprawiona retransmisja z chmury trafiłaby w „duplicate"
    i urządzenie zostałoby ze starym planem bez żadnego sygnału.
    """
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    await executor.async_set_schedule(_plan("s1"))
    handler = _handler(executor)

    await handler._execute_command(
        {
            "command": "SET_SCHEDULE",
            "request_id": "req-zatruty",
            "schedule": _plan("nowy", slots=[_slot(price_pln_kwh="0.45")]),
        }
    )

    assert executor.diagnostics["schedule_id"] == "s1"
    assert (await executor._store.async_load())["schedule_id"] == "s1"
    assert _status_raportu(handler) == "error"
    assert not handler._dedup.is_duplicate("req-zatruty")
