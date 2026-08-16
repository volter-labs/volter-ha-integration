"""S-5b / S-5c / S-5d — runda 3, wady wprowadzone przez samą naprawę S-5.

Naprawa S-5 zrobiła sekwencję nastaw NIEPRZERYWALNĄ (`asyncio.shield` +
`asyncio.ensure_future` świadomie poza śledzeniem HA). Rozwiązała stan połowiczny
falownika, ale kupiła trzy nowe wady:

S-5b (WYSOKA) — sekwencja przeżywa `async_stop()` po `STOP_WRITE_TIMEOUT_S` i pisze
    do falownika JUŻ PO wyładowaniu integracji, ścigając się z NOWYM executorem
    powołanym przez reload. Nowy executor ma czysty `WriteThrottle` (S-7), więc kończy
    z pamięcią inną niż stan fizyczny falownika — to jest rozjazd S-1 o piętro wyżej.
    Sonda M: użytkownik zmienia opcje w chwili, gdy falownik nie odpowiada.

S-5c (ŚREDNIA) — `async_apply` po zdobyciu `_write_lock` czeka na porzuconą sekwencję,
    ale po `STOP_WRITE_TIMEOUT_S` rezygnuje i przepuszcza nowy przebieg, więc
    serializacja S-1 przestaje obowiązywać WEWNĄTRZ jednego executora.
    Sonda L: `select.tryb=eco_charge` (#1) → `number.eco_soc=55` (#2) przy trwającym #1.

S-5d (NISKA) — `except (asyncio.CancelledError, TimeoutError): pass` wokół
    `asyncio.wait_for(self._listen_task, ...)` w `command_handler.async_stop()` łapie
    także anulowanie WOŁAJĄCEGO, sprzecznie z zasadą przyjętą w tym samym commicie
    w `executor._poczekaj_na_zapis`.

Wspólna teza napraw: **zapis, który przeżył swojego właściciela, nie ma prawa dotknąć
falownika.** Sekwencja dostaje odbieralną przepustkę (`guards.WritePermit`) i sprawdza
ją PRZED każdym service callem.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.volter import executor as executor_module
from custom_components.volter.command_handler import VolterCommandHandler
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Action
from tests.conftest import FakeHass

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_eco_mode_power": "number.eco_power",
    "entity_export_limit_switch": "switch.export_limit",
    "soc_reserve": 20.0,
    "user_mode": "autarky",
    "rated_power_w": 10000.0,
}


def _hass(soc: str = "60") -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", soc)
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb",
        "general",
        {"options": ["general", "eco_charge", "eco_discharge", "backup"]},
    )
    hass.states.set("number.eco_soc", "20")
    hass.states.set("number.eco_power", "0")
    return hass


def _plan(mode: str, *, soc_target: float) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "s5b",
        "slots": [
            {
                "from": (now - timedelta(minutes=10)).isoformat(),
                "to": (now + timedelta(hours=6)).isoformat(),
                "mode": mode,
                "power_w": 5000.0,
                "soc_target": soc_target,
                "export_allowed": True,
            }
        ],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    }


def _encje(hass: FakeHass) -> list[str]:
    return [data.get("entity_id", "") for _d, _s, data in hass.services.calls]


def _zawies_na_select(hass: FakeHass) -> asyncio.Event:
    """Falownik nie odpowiada: pierwszy service call (`select.tryb`) wisi do odwołania.

    To jest chwila z sondy M — zapis stoi na pierwszej nastawie PARAM_ORDER, czyli
    dokładnie wtedy, gdy przerwanie sekwencji jest najbardziej kosztowne.
    """
    zwolnij = asyncio.Event()

    async def _call(domain, service, data, blocking=False, **_kw):
        hass.services.calls.append((domain, service, dict(data)))
        if domain == "select":
            await zwolnij.wait()

    hass.services.async_call = _call  # type: ignore[assignment]
    return zwolnij


async def _poczekaj_na_pierwszy_zapis(hass: FakeHass) -> None:
    """Oddaj sterowanie pętli, aż sekwencja dojdzie do pierwszego service calla."""
    for _ in range(200):
        if hass.services.calls:
            return
        await asyncio.sleep(0)
    raise AssertionError("sekwencja zapisu nie ruszyła")


# ── S-5b, sonda M ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s5b_sekwencja_po_wyladowaniu_nie_dotyka_falownika(fake_entry, monkeypatch):
    """Sonda M: zmiana opcji w chwili, gdy falownik nie odpowiada.

    `async_stop()` odpuszcza czekanie po `STOP_WRITE_TIMEOUT_S` — i to jest w porządku,
    bo wyładowanie HA nie może wisieć w nieskończoność. NIE jest w porządku to, że
    porzucona sekwencja leci dalej: reload powołuje NOWY executor z czystym
    `WriteThrottle`, więc każdy zapis starej sekwencji trafia do falownika bez wiedzy
    nowego właściciela — rozjazd plan/rzeczywistość dokładnie jak w S-1, tylko przez
    granicę executora.
    """
    monkeypatch.setattr(executor_module, "STOP_WRITE_TIMEOUT_S", 0.05)
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    zwolnij = _zawies_na_select(hass)
    przebieg = asyncio.create_task(
        executor.async_set_schedule(_plan("charge", soc_target=80.0))
    )
    await _poczekaj_na_pierwszy_zapis(hass)

    # Wyładowanie integracji: czekanie ma twardy limit, więc kończy się rezygnacją.
    await asyncio.wait_for(executor.async_stop(), timeout=5.0)
    po_stopie = list(_encje(hass))
    assert po_stopie == ["select.tryb"], "test ma mierzyć sytuację PO rezygnacji z czekania"

    # Falownik wreszcie odpowiada — ale integracja już nie żyje.
    zwolnij.set()
    await asyncio.wait_for(przebieg, timeout=5.0)
    await asyncio.sleep(0)

    assert _encje(hass) == po_stopie, (
        f"S-5b: sekwencja pisała do falownika po wyładowaniu integracji: {_encje(hass)}"
    )


@pytest.mark.asyncio
async def test_s5b_przerwana_sekwencja_zostawia_slad_o_zlamanym_param_order(
    fake_entry, monkeypatch
):
    """Przerwanie w połowie jest LEPSZE niż dokończenie — ale nie wolno mu być ciche.

    Dokończenie ściga się z nowym właścicielem (S-5b), więc przerywamy. Kosztem jest
    złamany `PARAM_ORDER`: falownik został w trybie z nowego slotu i limitach ze
    starego. To był cały cel pierwotnego S-5, więc przerwanie MUSI zostawić ślad
    wymieniający nastawy, które nigdy nie poszły — inaczej wracamy do
    `diagnostics['last'] == {}` z sondy E.
    """
    monkeypatch.setattr(executor_module, "STOP_WRITE_TIMEOUT_S", 0.05)
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    zwolnij = _zawies_na_select(hass)
    przebieg = asyncio.create_task(
        executor.async_set_schedule(_plan("charge", soc_target=80.0))
    )
    await _poczekaj_na_pierwszy_zapis(hass)
    await asyncio.wait_for(executor.async_stop(), timeout=5.0)
    zwolnij.set()
    await asyncio.wait_for(przebieg, timeout=5.0)

    ostatni = executor.diagnostics["last"]
    assert ostatni, "S-5b: przerwana sekwencja nie zostawiła żadnego śladu"
    assert ostatni.get("status") != "success", f"przerwana sekwencja to nie sukces: {ostatni}"
    pominiete = {e.get("entity") for e in ostatni.get("errors", [])}
    assert {"eco_soc", "eco_power", "export_limit_enabled"} <= pominiete, (
        f"ślad musi wymieniać nastawy, które NIE poszły do falownika: {ostatni}"
    )
    assert any(n.get("invariant") == "S-5b" for n in ostatni.get("notes", [])), (
        f"brak noty tłumaczącej złamanie PARAM_ORDER: {ostatni.get('notes')}"
    )


@pytest.mark.asyncio
async def test_s5b_przerwana_sekwencja_nie_zapamietuje_niezapisanych_wartosci(
    fake_entry, monkeypatch
):
    """Throttle I-6 musi pamiętać WYŁĄCZNIE to, co fizycznie poszło do falownika.

    Gdyby przerwana sekwencja commitowała cały zestaw, kolejny przebieg uznałby
    niezapisane nastawy za „bez zmiany" i nigdy by ich nie dopisał — to jest ten sam
    trwały rozjazd, przed którym broni S-1.
    """
    monkeypatch.setattr(executor_module, "STOP_WRITE_TIMEOUT_S", 0.05)
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    zwolnij = _zawies_na_select(hass)
    przebieg = asyncio.create_task(
        executor.async_set_schedule(_plan("charge", soc_target=80.0))
    )
    await _poczekaj_na_pierwszy_zapis(hass)
    await asyncio.wait_for(executor.async_stop(), timeout=5.0)
    zwolnij.set()
    await asyncio.wait_for(przebieg, timeout=5.0)

    assert "eco_soc" not in executor._throttle._last_value, (
        "I-6 zapamiętał wartość, która NIGDY nie dotarła do falownika"
    )


@pytest.mark.asyncio
async def test_s5b_przebieg_czekajacy_na_zamek_nie_startuje_w_trakcie_wyladowania(
    fake_entry, monkeypatch
):
    """Druga droga do tej samej wady: nie porzucona sekwencja, tylko nowa.

    `async_stop` nie trzyma `_write_lock`, więc przebieg stojący w kolejce po zamek
    (tick złapany w locie albo komenda z chmury) mógłby wystartować w środku
    wyładowania. Powołałby WŁASNĄ przepustkę, której nikt już nie odbierze — czyli
    obszedłby całą naprawę bokiem i pisał do falownika po wyładowaniu integracji.
    """
    monkeypatch.setattr(executor_module, "STOP_WRITE_TIMEOUT_S", 0.05)
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    zwolnij = _zawies_na_select(hass)
    pierwszy = asyncio.create_task(
        executor.async_set_schedule(_plan("charge", soc_target=80.0))
    )
    await _poczekaj_na_pierwszy_zapis(hass)

    # Komenda z chmury czeka na zamek zajęty przez zawieszoną sekwencję.
    spozniony = asyncio.create_task(
        executor.async_apply({"eco_soc": 55.0}, action=Action.SELF_CONSUME, source="cloud")
    )
    await asyncio.sleep(0)

    await asyncio.wait_for(executor.async_stop(), timeout=5.0)
    zwolnij.set()
    await asyncio.wait_for(pierwszy, timeout=5.0)
    wynik = await asyncio.wait_for(spozniony, timeout=5.0)

    assert _encje(hass) == ["select.tryb"], (
        f"S-5b: przebieg z kolejki zaczął pisać po wyładowaniu integracji: "
        f"{hass.services.calls}"
    )
    assert wynik.status.value == "error", (
        "odmowa zapisu po zatrzymaniu executora musi być widoczna w wyniku"
    )


# ── S-5c, sonda L ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s5c_nowy_przebieg_nie_przeplata_sie_z_porzucona_sekwencja(
    fake_entry, monkeypatch
):
    """Sonda L: serializacja S-1 musi obowiązywać także po anulowaniu wołającego.

    Furtka czasowa w `async_apply` przepuszczała nowy przebieg, gdy stara sekwencja
    nadal trzymała falownik — czyli przywracała przeplot S-1 wewnątrz jednego
    executora. Po naprawie furtka jest BEZPIECZNA: przed jej przekroczeniem stara
    sekwencja traci przepustkę, więc na pewno nie dopisze już ani jednej nastawy.
    """
    monkeypatch.setattr(executor_module, "STOP_WRITE_TIMEOUT_S", 0.05)
    hass = _hass()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    zwolnij = _zawies_na_select(hass)
    pierwszy = asyncio.create_task(
        executor.async_set_schedule(_plan("charge", soc_target=80.0))
    )
    await _poczekaj_na_pierwszy_zapis(hass)

    # Wołający #1 znika (anulowana komenda / przeładowanie), sekwencja leci dalej.
    pierwszy.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pierwszy

    # #2 z drugiej pętli — musi dostać falownik na wyłączność.
    drugi = asyncio.create_task(
        executor.async_apply({"eco_soc": 55.0}, action=Action.SELF_CONSUME, source="cloud")
    )
    await asyncio.sleep(0.2)
    zwolnij.set()
    await asyncio.wait_for(drugi, timeout=5.0)
    await asyncio.sleep(0)

    assert _encje(hass) == ["select.tryb", "number.eco_soc"], (
        f"S-5c: porzucona sekwencja przeplotła się z nowym przebiegiem: {hass.services.calls}"
    )
    assert hass.services.calls[-1][2]["value"] == 55.0, (
        "ostatnia wartość na falowniku musi pochodzić od przebiegu, który commitował"
    )


# ── S-5b, kontrakt samego applier'a ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_s5b_applier_przerywa_na_granicy_nastawy_i_rozlicza_pominiete():
    """Przerwanie ma być POLICZALNE — to jedyny powód, dla którego jest dopuszczalne.

    Przepustka odebrana po pierwszej nastawie: `mode` idzie do falownika, reszta nie.
    Każda pominięta nastawa musi mieć własny wpis w `errors` (nie jedną zbiorczą notę),
    bo to `errors` czyta chmura i to one odróżniają „plan wykonany" od „falownik został
    w stanie mieszanym".
    """
    from custom_components.volter.applier import apply_params
    from custom_components.volter.guards import WritePermit

    hass = _hass()
    permit = WritePermit()

    async def _call(domain, service, data, blocking=False, **_kw):
        hass.services.calls.append((domain, service, dict(data)))
        permit.revoke("test odbiera prawo zapisu")

    hass.services.async_call = _call  # type: ignore[assignment]

    executed, errors, _notes = await apply_params(
        hass,
        dict(OPTIONS),
        {"mode": "eco_charge", "eco_soc": 80.0, "eco_power": 50.0},
        forced_params=set(),
        permit=permit,
    )

    assert executed == ["mode"], "przerwanie musi nastąpić na granicy, nie w środku nastawy"
    assert {e["entity"] for e in errors} == {"eco_soc", "eco_power"}
    assert all("PARAM_ORDER" in e["error"] for e in errors), (
        f"ślad musi nazywać skutek po imieniu: {errors}"
    )
    assert permit.aborted is True


@pytest.mark.asyncio
async def test_s5b_ponowienie_zapisu_nie_przezywa_odebrania_przepustki(monkeypatch):
    """Backoff ponowień to najdłuższe `await` w torze zapisu — i najczęstsze miejsce utraty prawa.

    `WRITE_RETRIES=3` z backoffem 1 s + 2 s znaczy, że jedna zawieszona nastawa trzyma
    sekwencję ~3 s. Gdyby ponowienie nie sprawdzało przepustki, sekwencja porzucona
    w trakcie backoffu i tak dopisałaby wartość do falownika mającego już innego
    właściciela — czyli wróciłby cały S-5b, tylko wąską ścieżką.
    """
    from custom_components.volter import applier as applier_module
    from custom_components.volter.applier import apply_params
    from custom_components.volter.guards import WritePermit

    hass = _hass()
    permit = WritePermit()
    proby: list[dict] = []

    async def _spij(_s: float) -> None:
        permit.revoke("właściciel zniknął w trakcie backoffu")

    async def _call(domain, service, data, blocking=False, **_kw):
        proby.append(dict(data))
        raise RuntimeError("falownik nie odpowiada")

    hass.services.async_call = _call  # type: ignore[assignment]
    monkeypatch.setattr(applier_module.asyncio, "sleep", _spij)

    executed, errors, _notes = await apply_params(
        hass, dict(OPTIONS), {"mode": "eco_charge"}, forced_params=set(), permit=permit,
    )

    assert executed == []
    assert len(proby) == 1, f"ponowienie wykonane mimo utraty przepustki: {proby}"
    assert "S-5b" in errors[0]["error"], f"powód porzucenia musi być widoczny: {errors}"


# ── S-5d ─────────────────────────────────────────────────────────────────────


def _handler() -> VolterCommandHandler:
    hass = FakeHass()
    entry = type("E", (), {"options": {"entity_ems_mode": "select.tryb"}})()
    handler = VolterCommandHandler(
        hass=hass, entry=entry, device_id="dev-1",
        supabase_url="https://example.supabase.co",
        anon_key="anon", api_key="vk_test", executor=AsyncMock(),
    )
    handler._report_result = AsyncMock()
    return handler


@pytest.mark.asyncio
async def test_s5d_async_stop_nie_polyka_anulowania_wolajacego():
    """`except CancelledError: pass` nie odróżnia dwóch zupełnie różnych zdarzeń.

    „Zadanie nasłuchu zakończyło się anulowaniem" (oczekiwane, sami je anulowaliśmy)
    to nie to samo co „ktoś anulował WOŁAJĄCEGO `async_stop`" (np. HA przerywa
    wyładowanie). Połknięcie drugiego przypadku łamie tę samą zasadę, którą ten sam
    commit przyjął w `executor._poczekaj_na_zapis` i w `_execute_command`.
    """
    handler = _handler()

    async def _uparty_naslkuch() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(10)  # sprzątanie trwa dłużej niż limit czekania
            raise

    handler._listen_task = asyncio.create_task(_uparty_naslkuch())
    await asyncio.sleep(0)

    stop = asyncio.create_task(handler.async_stop())
    await asyncio.sleep(0.01)
    stop.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stop

    handler._listen_task.cancel()


@pytest.mark.asyncio
async def test_s5d_zakonczenie_naslkuchu_anulowaniem_nadal_jest_normalne():
    """Druga strona rozdziału: anulowanie ZADANIA nasłuchu zostaje obsłużone cicho.

    To jest przypadek oczekiwany (sami je anulowaliśmy w `async_stop`) i nie może
    wywalić wyładowania integracji — inaczej naprawa S-5d zamieniłaby jedną wadę
    na drugą.
    """
    slad: list[str] = []

    async def _petla() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            slad.append("posprzatane")
            raise

    handler = _handler()
    handler._listen_task = asyncio.create_task(_petla())
    await asyncio.sleep(0)

    await asyncio.wait_for(handler.async_stop(), timeout=5.0)
    assert slad == ["posprzatane"]
