"""S-4 i S-5 — audyt świeżym okiem (`2026-08-16-faza-a-audyt-swiezym-okiem.md`).

S-4 (WYSOKA, JEDYNE ustalenie NISZCZĄCE SPRZĘT): próg rezerwy w I-1
(`guards.py`, `if state.soc < cfg.soc_reserve`) nie ma histerezy. I-6 nie chroni,
bo `eco_soc` REALNIE zmienia wartość w każdym tiku (raz `soc_target` z planu, raz
rezerwa użytkownika), a I-8 nie łapie, bo akcja pozostaje `SELF_CONSUME` — to nie
jest zmiana kierunku.

    Sonda B: `soc_reserve=20`, slot `self_consume` z `soc_target=10`, sensor SoC
    oscyluje 19/21/19/21 (bateria stoi dokładnie na progu). Sześć tików:
    `eco_soc` zapisany 5 razy wartościami [20, 10, 20, 10, 20].
    Jeden zapis do NVM na tick = ~1440 zapisów na dobę, bezterminowo.

S-5 (WYSOKA): `asyncio.CancelledError` to `BaseException`, więc `except Exception`
w `command_handler._execute_command` go nie łapie, a `executor.async_stop()` nie
czeka na trwający zapis. Ścieżka jest realna przy KAŻDEJ zmianie opcji integracji:
`_async_update_listener` → `async_reload` → `async_unload_entry` →
`command_handler.async_stop()` robi `_listen_task.cancel()` bez `await`.

    Sonda E: zapis planu `charge` zatrzymany na pierwszym service callu
    (`select.tryb='charge_battery'`). Po anulowaniu: `mode` zmieniony,
    `eco_soc`/`eco_power`/`export_limit_enabled` NIGDY nie zapisane,
    `diagnostics['last'] == {}`, do chmury nie idzie nic. Falownik w `eco_charge`
    z limitami z poprzedniego slotu — czyli w stanie, któremu ma zapobiegać
    `PARAM_ORDER`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.volter import executor as executor_module
from custom_components.volter.command_handler import VolterCommandHandler
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.const import RESERVE_LATCH_ENGAGED_MIN_S
from custom_components.volter.guards import ReserveHysteresis
from tests.conftest import FakeHass, ZegarSterowany

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_eco_mode_power": "number.eco_power",
    "entity_charge_limit": "number.charge_limit",
    "entity_soc_upper": "number.soc_upper",
    "entity_export_limit_switch": "switch.export_limit",
    "soc_reserve": 20.0,
    "user_mode": "autarky",
    "rated_power_w": 10000.0,
}


def _hass(soc: str = "19") -> FakeHass:
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
    return hass


def _plan(mode: str, *, soc_target: float) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "s4",
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


def _zapisy_eco_soc(hass: FakeHass) -> list[float]:
    """Progi SoC FAKTYCZNIE zapisane do NVM, wyrażone jako PRÓG — nie jako głębokość.

    Etap 3: na encję idzie głębokość rozładowania (DoD), bo `eco_mode_soc` w GoodWe
    jest tylko do odczytu. Odwracamy tu z powrotem na próg, żeby asercje niżej dalej
    mówiły o tym, o czym są — o rezerwie użytkownika — a nie o wielkości odwrotnej.
    Sam kierunek przeliczenia pilnuje `test_wylacznik_sterowania`/`applier`.
    """
    return [
        round(100.0 - data["value"], 1)
        for domain, service, data in hass.services.calls
        if domain == "number" and data.get("entity_id") == "number.eco_soc"
    ]


# ── S-4, sonda B: oscylacja na progu rezerwy ─────────────────────────────────


@pytest.mark.asyncio
async def test_s4_oscylacja_na_progu_daje_jeden_zapis_do_nvm(fake_entry, monkeypatch):
    """Sonda B wprost z ustalenia S-4 — LICZYMY ZAPISY, nie statusy.

    Sześć przebiegów pętli przy baterii stojącej na progu (sensor SoC oscyluje
    19/21 w granicach własnej rozdzielczości). Bez histerezy każdy przebieg
    przerzuca `eco_soc` między `soc_target` planu (10) a rezerwą (20), więc I-6
    przepuszcza KAŻDY zapis: wartość naprawdę się zmienia. Po naprawie próg
    zatrzaskuje się na pierwszym przekroczeniu i puszcza dopiero wyraźnie ponad
    rezerwą, więc do falownika idzie DOKŁADNIE JEDEN zapis.

    I-6 (minimalny odstęp) wyłączony na czas testu, żeby test rozstrzygał
    o histerezie, a nie o odstępie czasowym — realne tiki są co 60 s, czyli
    dokładnie na granicy `WRITE_MIN_INTERVAL_S` i throttle ich nie zatrzymuje.
    """
    monkeypatch.setattr(executor_module, "WRITE_MIN_INTERVAL_S", 0.0)
    hass = _hass(soc="19")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    # Przebieg 1 — bateria poniżej rezerwy, I-1 podnosi eco_soc do 20.
    await executor.async_set_schedule(_plan("self_consume", soc_target=10.0))

    # Przebiegi 2..6 — sensor oscyluje wokół progu, bateria fizycznie stoi.
    for soc in ("21", "19", "21", "19", "21"):
        hass.states.set("sensor.soc", soc)
        await executor._async_tick()

    zapisy = _zapisy_eco_soc(hass)
    assert len(zapisy) == 1, (
        f"S-4: {len(zapisy)} zapisów eco_soc {zapisy} przy baterii stojącej na progu — "
        f"to ~1440 zapisów do pamięci nieulotnej falownika na dobę, bezterminowo"
    )
    assert zapisy == [20.0], (
        "zatrzask I-1 ma utrzymać rezerwę użytkownika, a nie soc_target planu"
    )


@pytest.mark.asyncio
async def test_s4_zatrzask_puszcza_dopiero_wyraznie_ponad_progiem(fake_entry, monkeypatch):
    """Guard raz uruchomiony NIE odpuszcza przy pierwszym odczycie powyżej progu.

    Rezerwa 20%, szerokość histerezy 3 pp: przy 21% i 22% zatrzask trzyma
    (to nadal jest szum czujnika o rozdzielczości 1%).

    S-4b: samo przekroczenie pasma też nie zwalnia — do zwolnienia potrzebne są OBIE
    rzeczy naraz, pasmo i minimalny czas trwania stanu. Samo pasmo przepuszczało
    w całości każdą oscylację od siebie szerszą (19/24 = znowu 1440 zapisów na dobę),
    bo dla każdego pasma istnieje większa amplituda.
    """
    monkeypatch.setattr(executor_module, "WRITE_MIN_INTERVAL_S", 0.0)
    zegar = ZegarSterowany()
    monkeypatch.setattr(executor_module, "time", zegar)
    hass = _hass(soc="19")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    await executor.async_set_schedule(_plan("self_consume", soc_target=10.0))
    assert _zapisy_eco_soc(hass) == [20.0]

    for soc in ("21", "22"):
        zegar.przesun(60.0)
        hass.states.set("sensor.soc", soc)
        await executor._async_tick()
    assert _zapisy_eco_soc(hass) == [20.0], (
        "odczyt tuż nad progiem mieści się w szumie czujnika — zatrzask musi trzymać"
    )

    zegar.przesun(60.0)
    hass.states.set("sensor.soc", "23")
    await executor._async_tick()
    assert _zapisy_eco_soc(hass) == [20.0], (
        "S-4b: pasmo bez czasu trwania stanu nie ogranicza niczego — zatrzask musi "
        "przetrwać minimalny czas, bo inaczej wystarczy szersza oscylacja"
    )

    # Nawet po upływie minimalnego czasu pasmo nadal rozstrzyga: 22% to szum.
    zegar.przesun(RESERVE_LATCH_ENGAGED_MIN_S)
    hass.states.set("sensor.soc", "22")
    await executor._async_tick()
    assert _zapisy_eco_soc(hass) == [20.0], "pasmo obowiązuje bezterminowo"

    zegar.przesun(60.0)
    hass.states.set("sensor.soc", "23")
    await executor._async_tick()
    assert _zapisy_eco_soc(hass) == [20.0, 10.0], (
        "po realnym naładowaniu ponad próg + histereza i po minimalnym czasie trwania "
        "stanu plan musi znowu dojść do falownika"
    )


@pytest.mark.asyncio
async def test_s4_zatrzask_zaklada_sie_ponownie_po_zejsciu_pod_prog(fake_entry, monkeypatch):
    """Histereza nie może być jednorazowa — po zwolnieniu próg musi znowu działać."""
    monkeypatch.setattr(executor_module, "WRITE_MIN_INTERVAL_S", 0.0)
    # 22% -> 19% to spadek 3 pp, czyli w granicach I-9 (podłoga tolerancji 5 pp) —
    # test ma rozstrzygać o I-1, a nie o odrzuceniu całego odczytu jako awarii czujnika.
    hass = _hass(soc="22")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    await executor.async_set_schedule(_plan("self_consume", soc_target=10.0))
    assert _zapisy_eco_soc(hass) == [10.0], "powyżej rezerwy plan idzie bez zmian"

    hass.states.set("sensor.soc", "19")
    await executor._async_tick()
    assert _zapisy_eco_soc(hass) == [10.0, 20.0], "I-1 musi zadziałać po zejściu pod próg"


def test_s4_histereza_jest_szersza_niz_rozdzielczosc_sensora():
    """Szerokość pasma to decyzja projektowa — pilnujemy jej wprost.

    Rozdzielczość sensora SoC to zwykle 1 pp. Pasmo równe rozdzielczości nie
    rozwiązałoby sondy B (oscylacja 19/21 nadal przekraczałaby próg zwolnienia),
    więc musi być od niej wyraźnie szersze.

    S-4b dokłada drugi wymiar: pasmo rozstrzyga BEZTERMINOWO (22.9% nie zwalnia nigdy),
    ale przekroczenie pasma zwalnia zatrzask dopiero po minimalnym czasie trwania stanu.
    """
    h = ReserveHysteresis()
    assert h.band_pp >= 3.0
    t = 0.0

    assert h.engaged(soc=19.0, reserve=20.0, now=t) is True
    assert h.engaged(soc=21.0, reserve=20.0, now=t + 60) is True, "1 pp ponad progiem to szum"

    # Po dowolnie długim czasie pasmo nadal trzyma — czas nie rozmiękcza pasma.
    t += h.engaged_min_s + 120
    assert h.engaged(soc=22.9, reserve=20.0, now=t) is True
    assert h.engaged(soc=23.0, reserve=20.0, now=t + 60) is False, (
        "reserve + pasmo + minimalny czas trwania stanu zwalniają zatrzask"
    )

    # Zatrzask zakłada się ponownie: wyraźne zejście pod rezerwę omija czas trwania
    # stanu zwolnionego, bo I-1 jest ochroną, a nie wygładzaniem.
    assert h.engaged(soc=16.9, reserve=20.0, now=t + 120) is True

    # Zejście PŁYTKIE (w paśmie) czeka na swój czas — to ono jest szumem czujnika.
    assert h.engaged(soc=40.0, reserve=20.0, now=t + h.cycle_min_s + 180) is False
    assert h.engaged(soc=19.9, reserve=20.0, now=t + h.cycle_min_s + 240) is False
    assert h.engaged(
        soc=19.9, reserve=20.0, now=t + h.cycle_min_s + 240 + h.released_min_s
    ) is True, "zatrzask zakłada się ponownie"


def test_s4_kopia_dla_suchego_przebiegu_nie_mutuje_oryginalu():
    """`snapshot()` — ten sam kontrakt co `ParamBoundsCache.snapshot` (RR-2).

    Suchy przebieg (`async_diagnose`) pracuje na kopii, więc zatrzaśnięcie progu
    podczas diagnozy nie może zmienić zachowania kolejnego REALNEGO przebiegu.
    """
    h = ReserveHysteresis()
    kopia = h.snapshot()
    assert kopia.engaged(soc=19.0, reserve=20.0) is True

    # Oryginał nietknięty: odczyt tuż nad rezerwą nadal jest "powyżej rezerwy".
    assert h.engaged(soc=21.0, reserve=20.0) is False

    # W drugą stronę: kopia dziedziczy ZAŁOŻONY zatrzask, więc diagnoza pokazuje
    # tę samą decyzję co realny tor zapisu.
    assert h.engaged(soc=19.0, reserve=20.0) is True
    assert h.snapshot().engaged(soc=21.0, reserve=20.0) is True


@pytest.mark.asyncio
async def test_s4_diagnoza_nie_zatrzaskuje_progu(fake_entry, monkeypatch):
    """`async_diagnose` obiecuje ZERO mutacji stanu (R-7/R-14/RR-2/RR-7).

    Zatrzask rezerwy jest stanem executora dokładnie tak samo jak `DirectionLimiter`
    i cache granic — suchy przebieg przy SoC pod progiem nie może go założyć,
    bo wtedy diagnoza ZMIENIAŁABY zachowanie kolejnego realnego przebiegu.
    """
    monkeypatch.setattr(executor_module, "WRITE_MIN_INTERVAL_S", 0.0)
    hass = _hass(soc="19")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = None

    from custom_components.volter.schedule import Schedule

    executor._schedule = Schedule.from_dict(_plan("self_consume", soc_target=10.0))

    await executor.async_diagnose()
    assert hass.services.calls == [], "suchy przebieg nie pisze"

    hass.states.set("sensor.soc", "21")
    await executor._async_tick()
    assert _zapisy_eco_soc(hass) == [10.0], (
        "diagnoza zatrzasnęła próg — realny przebieg powyżej rezerwy wykonał cudzą decyzję"
    )


# ── S-5, sonda E: anulowanie w trakcie sekwencji zapisu ──────────────────────


def _przechwyc_zapisy(hass: FakeHass, przy_pierwszym_select) -> None:
    """Podmień service call tak, żeby anulować przebieg dokładnie na `select.tryb`."""
    stan = {"select": False}

    async def _call(domain, service, data, blocking=False, **_kw):
        hass.services.calls.append((domain, service, dict(data)))
        await asyncio.sleep(0)
        if domain == "select" and not stan["select"]:
            stan["select"] = True
            przy_pierwszym_select()
            await asyncio.sleep(0)

    hass.services.async_call = _call  # type: ignore[assignment]


def _zapisane_encje(hass: FakeHass) -> set[str]:
    return {data.get("entity_id", "") for _d, _s, data in hass.services.calls}


@pytest.mark.asyncio
async def test_s5_anulowanie_nie_zostawia_falownika_w_stanie_polowicznym(fake_entry):
    """Sonda E wprost z ustalenia S-5.

    Anulowanie trafia w przebieg dokładnie po zapisie `mode` — czyli w moment,
    w którym falownik jest już w `eco_charge`, ale limity są jeszcze z POPRZEDNIEGO
    slotu. Tego stanu ma zabraniać `PARAM_ORDER`, a anulowanie go produkowało.
    Po naprawie sekwencja nastaw jest nierozerwalna (`asyncio.shield`), a
    `async_stop` czeka na jej dokończenie.
    """
    hass = _hass(soc="60")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    zadanie: asyncio.Task | None = None
    _przechwyc_zapisy(hass, lambda: zadanie.cancel())  # type: ignore[union-attr]

    zadanie = asyncio.create_task(
        executor.async_set_schedule(_plan("charge", soc_target=80.0))
    )
    with pytest.raises(asyncio.CancelledError):
        await zadanie

    # S-5: `async_stop` MUSI poczekać na trwający zapis — bez tego HA wyładowuje
    # integrację w środku sekwencji PARAM_ORDER.
    await asyncio.wait_for(executor.async_stop(), timeout=5.0)

    assert _zapisane_encje(hass) >= {
        "select.tryb",
        "number.soc_upper",
        "number.charge_limit",
        "switch.export_limit",
    }, (
        f"S-5: anulowanie przerwało sekwencję nastaw — falownik został w trybie "
        f"z nowego slotu i limitach ze starego. Zapisy: {hass.services.calls}"
    )


@pytest.mark.asyncio
async def test_s5_anulowanie_zostawia_slad_w_last(fake_entry):
    """Sonda E, druga część: `diagnostics['last'] == {}` po anulowaniu.

    Stan połowiczny był niewidoczny NIGDZIE — ani w HA, ani w chmurze. Anulowanie
    musi zostawić ślad, bo inaczej jedyne narzędzie do odpowiedzi na pytanie
    „co poszło do falownika" milczy dokładnie wtedy, gdy poszło coś nietypowego.
    """
    hass = _hass(soc="60")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    zadanie: asyncio.Task | None = None
    _przechwyc_zapisy(hass, lambda: zadanie.cancel())  # type: ignore[union-attr]

    zadanie = asyncio.create_task(
        executor.async_set_schedule(_plan("charge", soc_target=80.0))
    )
    with pytest.raises(asyncio.CancelledError):
        await zadanie
    await asyncio.wait_for(executor.async_stop(), timeout=5.0)

    ostatni = executor.diagnostics["last"]
    assert ostatni, "S-5: anulowanie nie zostawiło żadnego śladu w _last"
    assert ostatni.get("cancelled") is True, f"brak znacznika anulowania: {ostatni}"
    assert any(
        n.get("invariant") == "S-5" for n in ostatni.get("notes", [])
    ), f"brak noty tłumaczącej anulowanie: {ostatni.get('notes')}"
    # Slot ładowania niesie GÓRNY próg (`soc_upper`) i moc (`charge_limit`) —
    # dolnego progu nie emituje, bo `soc_target` znaczy tu "do ilu naładować".
    assert "soc_upper" in ostatni.get("executed", []), (
        f"ślad musi mówić PRAWDĘ o tym, co doszło do falownika: {ostatni}"
    )


@pytest.mark.asyncio
async def test_s5_stop_bez_trwajacego_zapisu_jest_natychmiastowy(fake_entry):
    """Koszt `shield` nie może być płacony, gdy nie ma czego chronić.

    `async_stop` czeka wyłącznie na TRWAJĄCY zapis — przy bezczynnym executorze
    wyładowanie integracji ma być natychmiastowe.
    """
    hass = _hass(soc="60")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    await executor.async_start()
    await asyncio.wait_for(executor.async_stop(), timeout=1.0)


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
async def test_s5_anulowanie_komendy_jest_raportowane_do_chmury():
    """`except Exception` nie łapie `CancelledError` — do chmury nie szło NIC.

    Chmura rozlicza dobę zakładając, że komenda się wykonała. Anulowana komenda
    musi zostać zaraportowana, a samo anulowanie MUSI polecieć dalej (nie wolno
    go połknąć — to złamałoby wyładowanie integracji).
    """
    executor = AsyncMock()
    executor.async_apply.side_effect = asyncio.CancelledError()
    handler = _handler(executor)

    with pytest.raises(asyncio.CancelledError):
        await handler._execute_command(
            {"command": "SET_WORK_MODE", "request_id": "req-c", "params": {"eco_soc": 30}}
        )

    handler._report_result.assert_awaited()
    args, kwargs = handler._report_result.await_args
    assert args[0] == "req-c"
    assert args[1] != "success", f"anulowana komenda nie jest sukcesem: {args}"


@pytest.mark.asyncio
async def test_s5_anulowana_komenda_nie_trafia_do_dedupu():
    """Anulowanie jest przejściowe (reload integracji) — retransmisja musi działać.

    Ta sama zasada co R-2/RR-4: nic nie poszło do falownika z winy anulowania,
    więc `request_id` nie może zablokować ponowienia.
    """
    executor = AsyncMock()
    executor.async_apply.side_effect = asyncio.CancelledError()
    handler = _handler(executor)

    with pytest.raises(asyncio.CancelledError):
        await handler._execute_command(
            {"command": "SET_WORK_MODE", "request_id": "req-c", "params": {"eco_soc": 30}}
        )

    assert not handler._dedup.is_duplicate("req-c")


@pytest.mark.asyncio
async def test_s5_async_stop_czeka_na_anulowany_listen_task():
    """`_listen_task.cancel()` bez `await` nie daje szansy na obsługę anulowania.

    Bez oczekiwania kod sprzątający (raport do chmury z poprzedniego testu) nigdy
    się nie wykona — anulowanie jest wysłane i natychmiast porzucone.
    """
    slad: list[str] = []

    async def _petla() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            slad.append("posprzatane")
            raise

    handler = _handler(AsyncMock())
    handler._listen_task = asyncio.create_task(_petla())
    await asyncio.sleep(0)

    await asyncio.wait_for(handler.async_stop(), timeout=5.0)

    assert slad == ["posprzatane"], (
        "async_stop nie poczekał na anulowany task — obsługa anulowania nie wykonała się"
    )
