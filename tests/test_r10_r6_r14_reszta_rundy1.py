"""Domknięcie reszty rundy 1: R-10 (bariera na wyjątki), R-6 (I-4 poza oknem
planu), R-14 (property `diagnostics`) — z `docs/analysis/2026-08-16-faza-a-review.md`,
sekcja „Ustalenia rundy 1 zamknięte częściowo lub pozornie".

Każdy test w tym pliku pilnuje OBU stron: sondy z dokumentu (musi FAILować na
kodzie sprzed naprawy) i ochrony pierwotnej naprawki, którą runda 1 już zamknęła
(nie wolno jej cofnąć).

R-10 (reszta): bariera try/except w `_execute_command` zaczynała się PO
wydobyciu `command`/`params`/`request_id` z payloadu (linie 252-259 leżały
POZA nią) — payload w kształcie innym niż dict (lista/string/liczba/None)
wywalał `AttributeError` zanim bariera w ogóle zaczęła działać.

R-6 (reszta): `Fallback.as_slot()` nie niesie `price_pln_kwh` — I-4 jest ślepe
nie tylko na ścieżce SET_WORK_MODE (to zamknęła runda 1), ale też gdy
harmonogramu nie ma wcale albo gdy plan wygasł, bo `effective_slot()` w obu
przypadkach zwraca właśnie fallback. Nie ma skąd wziąć ceny (integracja nie
mapuje żadnej encji cenowej) — świadomy wybór: fallback musi być zachowawczy
wobec eksportu (blokować go), zamiast na niego domyślnie zezwalać.

R-14 (reszta): property `VolterExecutor.diagnostics` nadal zwraca żywą
referencję do `self._last` — `async_diagnose` (metoda) to naprawiła kopią
obronną, ale property zostało pominięte, mimo że to ten sam błąd w tej samej
klasie.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.volter.command_handler import VolterCommandHandler
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Action, GuardResult, Status
from custom_components.volter.schedule import Schedule
from tests.conftest import FakeHass

# ═══════════════════════════════════════════════════════════════════════════
# R-10 (reszta): bariera musi objąć CAŁĄ obsługę wiadomości, łącznie z
# wydobyciem pól z payloadu.
# ═══════════════════════════════════════════════════════════════════════════


def _handler(executor) -> VolterCommandHandler:
    hass = FakeHass()
    entry = type("E", (), {"options": {"entity_ems_mode": "select.tryb"}})()
    handler = VolterCommandHandler(
        hass=hass, entry=entry, device_id="dev-1",
        supabase_url="https://example.supabase.co",
        anon_key="anon", api_key="vk_test", executor=executor,
    )
    handler._report_result = AsyncMock()
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "zly_payload",
    [
        pytest.param(["eco_soc"], id="lista"),
        pytest.param("not-a-dict", id="string"),
        pytest.param(42, id="int"),
        pytest.param(3.14, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(None, id="none"),
    ],
)
async def test_r10_reszta_zly_ksztalt_payloadu_nie_wybucha(zly_payload):
    """Sonda z dokumentu regresji: `payload.payload` (czyli argument
    `_execute_command`) jako lista/string/liczba/None. Przed naprawą
    `payload.get('command', '')` (linia 252, POZA barierą) wywalał
    `AttributeError`, który propagował dalej do `_handle_message` i
    `_connect_and_listen` — zrywając WebSocket zamiast zaraportować błąd
    JEDNEJ komendy."""
    executor = AsyncMock()
    handler = _handler(executor)

    # Nie może rzucić wyjątku — to jest cała istota R-10 (reszta).
    await handler._execute_command(zly_payload)

    executor.async_apply.assert_not_awaited()
    handler._report_result.assert_awaited_once()
    args, kwargs = handler._report_result.await_args
    assert args[1] == "error"
    assert kwargs.get("errors"), "musi być powód w errors"


@pytest.mark.asyncio
async def test_r10_reszta_zly_payload_przez_handle_message_nie_zrywa_polaczenia():
    """Ten sam scenariusz, ale wejściem od góry: `_handle_message` z pełną
    kopertą Phoenix, w której `payload.payload` (broadcast_payload) jest listą.
    Musi przejść przez `_handle_message` bez wyjątku — inaczej `_connect_and_listen`
    zamyka WebSocket w `finally`."""
    executor = AsyncMock()
    handler = _handler(executor)

    msg = {
        "topic": handler._channel_topic,
        "event": "broadcast",
        "payload": {"event": "command", "payload": ["eco_soc"]},
    }

    # Nie może rzucić wyjątku.
    await handler._handle_message(msg)

    handler._report_result.assert_awaited_once()
    args, kwargs = handler._report_result.await_args
    assert args[1] == "error"


# ── Strona ochrony R-10 (pierwotny zakres z rundy 1) ─────────────────────────


@pytest.mark.asyncio
async def test_r10_reszta_ochrona_params_niedict_dalej_dziala():
    """Pierwotny scenariusz R-10 (payload TO jest dict, ale `params` w nim
    nie jest) musi dalej działać identycznie jak przed tą naprawą."""
    executor = AsyncMock()
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "params": ["eco_soc"]}
    await handler._execute_command(payload)

    executor.async_apply.assert_not_awaited()
    handler._report_result.assert_awaited_once()
    args, kwargs = handler._report_result.await_args
    assert args[1] == "error"
    assert kwargs.get("errors"), "musi być powód w errors"


@pytest.mark.asyncio
async def test_r10_reszta_ochrona_wyjatek_w_egzekutorze_nadal_raportowany():
    """Pierwotny scenariusz R-10 (wyjątek z wnętrza `async_apply`) musi dalej
    być łapany i raportowany z poprawnym `request_id`."""
    executor = AsyncMock()
    executor.async_apply.side_effect = RuntimeError("boom")
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-boom", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)

    handler._report_result.assert_awaited_once()
    args, kwargs = handler._report_result.await_args
    assert args[0] == "req-boom"
    assert args[1] == "error"
    assert kwargs.get("errors"), "musi być powód w errors"


@pytest.mark.asyncio
async def test_r10_reszta_ochrona_poprawna_komenda_dalej_dziala():
    """Kontrast: poprawnie ukształtowany payload musi przejść normalnie —
    bariera nie może połknąć prawidłowych komend."""
    executor = AsyncMock()
    executor.async_apply.return_value = GuardResult(
        params={"eco_soc": 30.0}, status=Status.SUCCESS, executed=["eco_soc"]
    )
    handler = _handler(executor)

    payload = {"command": "SET_WORK_MODE", "request_id": "req-ok", "params": {"eco_soc": 30}}
    await handler._execute_command(payload)

    executor.async_apply.assert_awaited_once()
    handler._report_result.assert_awaited_once()
    args, kwargs = handler._report_result.await_args
    assert args[0] == "req-ok"
    assert args[1] == "success"


# ═══════════════════════════════════════════════════════════════════════════
# R-6 (reszta): I-4 poza oknem ważności planu (brak harmonogramu / plan wygasł)
# ═══════════════════════════════════════════════════════════════════════════

OPTIONS_R6 = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_export_limit": "number.export_limit",
    "entity_export_limit_switch": "switch.export_limit",
}


def _hass_r6(soc: str = "55") -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", soc)
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb", "auto", {"options": ["auto", "charge_pv", "discharge_pv", "import_ac", "export_ac", "conserve", "off_grid", "battery_standby", "buy_power", "sell_power", "charge_battery", "discharge_battery"]}
    )
    hass.states.set("number.eco_soc", "20")
    hass.states.set("number.export_limit", "5000")
    return hass


@pytest.mark.asyncio
async def test_r6_reszta_brak_harmonogramu_blokuje_eksport(fake_entry):
    """Sonda: `self._schedule is None` (stan domyślny po instalacji). Nie ma
    ceny znikąd — fallback musi być zachowawczy i blokować eksport, bo nie da
    się zweryfikować, że cena jest dodatnia (I-4 z definicji ślepe tutaj)."""
    hass = _hass_r6(soc="55")
    fake_entry.options = dict(OPTIONS_R6)
    executor = VolterExecutor(hass, fake_entry)
    assert executor._schedule is None, "warunek testu: harmonogram nigdy nie ustawiony"

    result = await executor._async_execute_now()

    assert result.params.get("export_limit") == 0.0, (
        "brak harmonogramu -> brak wiedzy o cenie -> fallback musi blokować eksport "
        "(świadomy wybór zachowawczy, nie przeoczenie)"
    )
    assert result.params.get("export_limit_enabled") is True


@pytest.mark.asyncio
async def test_r6_reszta_wygasly_plan_blokuje_eksport(fake_entry):
    """Sonda: plan ISTNIEJE, ale wygasł — `effective_slot()` zwraca
    `fallback.as_slot()`, który też nie niesie ceny. Ta sama ochrona musi
    zadziałać jak przy całkowitym braku harmonogramu."""
    hass = _hass_r6(soc="55")
    fake_entry.options = dict(OPTIONS_R6)
    executor = VolterExecutor(hass, fake_entry)
    now = datetime.now(timezone.utc)
    executor._schedule = Schedule.from_dict({
        "schedule_id": "r6-wygasly",
        "slots": [{
            "from": (now - timedelta(hours=2)).isoformat(),
            "to": (now - timedelta(minutes=30)).isoformat(),
            "mode": "self_consume",
            "soc_target": 50.0,
            "price_pln_kwh": 0.30,
            "export_allowed": True,
        }],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    })
    assert executor._schedule.is_expired(now), "warunek testu: plan wygasł"

    result = await executor._async_execute_now()

    assert result.params.get("export_limit") == 0.0, (
        "plan wygasły -> fallback bez ceny -> eksport musi być zablokowany, mimo że "
        "OSTATNI znany slot dopuszczał eksport — fallback nie dziedziczy tej zgody"
    )
    assert result.params.get("export_limit_enabled") is True


# ── Strona ochrony R-6 (pierwotny zakres z rundy 1 + normalna praca) ────────


@pytest.mark.asyncio
async def test_r6_reszta_ochrona_i4_dalej_dziala_na_sciezce_set_work_mode(fake_entry):
    """Pierwotny scenariusz R-6 (runda 1) musi dalej działać: plan AKTYWNY z
    ceną <= 0, komenda SET_WORK_MODE bez ceny własnej — I-4 bierze cenę ze
    slotu."""
    hass = _hass_r6(soc="55")
    fake_entry.options = dict(OPTIONS_R6)
    executor = VolterExecutor(hass, fake_entry)
    now = datetime.now(timezone.utc)
    await executor.async_set_schedule({
        "schedule_id": "r6-aktywny",
        "slots": [{
            "from": (now - timedelta(minutes=10)).isoformat(),
            "to": (now + timedelta(minutes=50)).isoformat(),
            "mode": "self_consume",
            "soc_target": 50.0,
            "price_pln_kwh": -0.05,
            "export_allowed": True,
        }],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    })

    result = await executor.async_apply(
        {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="cloud"
    )

    assert any(n.invariant == "I-4" for n in result.notes)
    assert result.params.get("export_limit") == 0.0


@pytest.mark.asyncio
async def test_r6_reszta_ochrona_plan_aktywny_z_zezwoleniem_eksportuje_normalnie(fake_entry):
    """Strona ochrony: gdy plan jest AKTYWNY (nie fallback) i slot dopuszcza
    eksport przy dodatniej cenie, normalna praca NIE MOŻE zostać zablokowana —
    konserwatyzm dotyczy WYŁĄCZNIE fallbacku bez ceny, nie całego systemu."""
    hass = _hass_r6(soc="55")
    fake_entry.options = dict(OPTIONS_R6)
    executor = VolterExecutor(hass, fake_entry)
    now = datetime.now(timezone.utc)
    executor._schedule = Schedule.from_dict({
        "schedule_id": "r6-normalny",
        "slots": [{
            "from": (now - timedelta(minutes=10)).isoformat(),
            "to": (now + timedelta(minutes=50)).isoformat(),
            "mode": "self_consume",
            "soc_target": 50.0,
            "price_pln_kwh": 0.30,
            "export_allowed": True,
        }],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    })

    result = await executor._async_execute_now()

    assert result.params.get("export_limit_enabled") is False, (
        "plan aktywny z dodatnią ceną i zgodą na eksport musi eksportować normalnie "
        "— fallback (reszta R-6) nie może zmienić zachowania na ścieżce ze slotem"
    )


# ═══════════════════════════════════════════════════════════════════════════
# R-14 (reszta): property `diagnostics` — kopia obronna
# ═══════════════════════════════════════════════════════════════════════════


def test_r14_reszta_diagnostics_property_last_nie_jest_zywa_referencja(fake_entry):
    """`VolterExecutor.diagnostics['last']` musi być kopią `self._last` — ten
    sam błąd, który `async_diagnose` (metoda) już naprawiła kopią obronną, nie
    został naprawiony w property `diagnostics`, mimo że to ta sama klasa."""
    hass = FakeHass()
    executor = VolterExecutor(hass, fake_entry)
    executor._last = {"source": "schedule", "executed": ["eco_soc"], "errors": []}

    report = executor.diagnostics
    report["last"]["executed"].append("zmienione-z-zewnatrz")

    assert executor._last["executed"] == ["eco_soc"], (
        "modyfikacja odpowiedzi property w miejscu nie może zmienić stanu "
        "wewnętrznego executora — kontrakt property musi być spójny z async_diagnose"
    )


# ── Strona ochrony R-14 (pierwotny zakres — async_diagnose, nie regresja) ───


@pytest.mark.asyncio
async def test_r14_reszta_ochrona_async_diagnose_dalej_kopiuje(fake_entry):
    """Kontrast: `async_diagnose` (metoda, pierwotny zakres R-14) musi dalej
    zwracać kopię — ta naprawa nie może jej cofnąć."""
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    fake_entry.options = {"entity_soc": "sensor.soc"}
    executor = VolterExecutor(hass, fake_entry)
    executor._last = {"source": "schedule", "executed": ["eco_soc"]}

    report = await executor.async_diagnose()
    report["last"]["executed"].append("zmienione-z-zewnatrz")

    assert executor._last["executed"] == ["eco_soc"]
