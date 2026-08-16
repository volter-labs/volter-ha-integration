"""RR-5, RR-6, RR-7 — regresje rundy 2 wprowadzone naprawami R-1/R-7 z rundy 1
(`docs/analysis/2026-08-16-faza-a-regresje.md`), plus domknięcie reszty R-12.

Każdy test w tym pliku pilnuje OBU stron: że sonda z dokumentu regresji faktycznie
FAILuje na kodzie sprzed naprawy, i że naprawa NIE cofa ochrony, którą pierwotna
naprawka (R-1/R-7/R-12) miała zapewnić.

RR-5 (ŚREDNIA): na ścieżce `forced_action` rekurencyjny `async_apply` woła
`_remember` w przebiegu WEWNĘTRZNYM, a przebieg ZEWNĘTRZNY woła je ponownie —
dwa różne klucze decyzji nadpisują się nawzajem, więc anty-spam z R-12 nigdy nie
się stabilizuje (sonda P2).

RR-6 (ŚREDNIA): `act = action or infer_action(...)` w I-8 bierze akcję OD
WOŁAJĄCEGO, więc `DirectionLimiter.record()` zapisuje kierunek, który guard I-1
właśnie zablokował (sonda P6).

RR-7 (NISKA): `DirectionLimiter.allows()` woła `_prune`, które PRZYPISUJE
`self._history` — mutacja stanu w `async_diagnose`, który obiecuje "suchy
przebieg" (sonda P3).

R-12 (reszta): pętla logująca notatki guardów (INFO) i "Zapis wstrzymany"
(WARNING) stoi PRZED sprawdzeniem `result.rejected` w `async_apply` — loguje
bezwarunkowo na KAŻDYM ticku ścieżki DEGRADED, mimo że linia "Decyzja" już ma
anty-spam z R-12.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Action, DirectionLimiter, Status
from custom_components.volter.schedule import Schedule
from tests.conftest import FakeHass

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_eco_mode_power": "number.eco_power",
    "entity_discharge_limit": "number.discharge_limit",
    "entity_export_limit_switch": "switch.export_limit",
    # Scenariusz z sondy P2/P6: rezerwa backup 40%, tryb autarky.
    "soc_reserve": 40.0,
    "user_mode": "autarky",
}


def _hass(soc: str = "30") -> FakeHass:
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
    hass.states.set("number.discharge_limit", "0")
    return hass


def _tryby(hass: FakeHass) -> list[str]:
    """Opcje, które faktycznie poszły na encję select trybu pracy."""
    return [
        data.get("option")
        for domain, service, data in hass.services.calls
        if (domain, service) == ("select", "select_option")
    ]


def _schedule(mode: str, *, soc_target: float, power_w: float = 5000.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "rr567",
        "slots": [
            {
                "from": (now - timedelta(minutes=10)).isoformat(),
                "to": (now + timedelta(minutes=50)).isoformat(),
                "mode": mode,
                "power_w": power_w,
                "soc_target": soc_target,
                "export_allowed": True,
            }
        ],
        "fallback": {"mode": "self_consume", "soc_reserve": 40.0},
    }


# ── RR-5: forced_action nie może zatruwać anty-spamu z R-12 ─────────────────


@pytest.mark.asyncio
async def test_rr5_forced_action_nie_spamuje_po_ustabilizowaniu(fake_entry, caplog):
    """Sonda P2: `soc_reserve=40`, `SoC=30`, slot `discharge` — stan utrzymujący
    się godzinami. Rekursywny `async_apply` (przemapowanie na tryb bezpieczny)
    woła `_remember` wewnątrz, a przebieg zewnętrzny woła je ponownie z innym
    statusem — dwa różne klucze nadpisują się nawzajem, więc `decision !=
    _last_decision` jest prawdziwe na KAŻDYM ticku, bez końca (dawało [2,2,2,2]
    INFO na 4 tiki zamiast wygasić się po ustabilizowaniu)."""
    hass = _hass(soc="30")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    def decyzje_info() -> list[str]:
        return [
            r.message for r in caplog.records
            if r.levelno == logging.INFO and "Decyzja:" in r.message
        ]

    with caplog.at_level(logging.INFO):
        # Tick 1: instaluje plan i wykonuje go od razu (I-1 wymusza self_consume).
        await executor.async_set_schedule(_schedule("discharge", soc_target=50.0))
        # Tick 2: to samo w kółko (jak pętla wykonawcza co 60s) — I-6 zacznie
        # filtrować niezmienione wartości, więc `executed` się zmieni względem
        # ticku 1 (to LEGALNA, zamierzona różnica decyzji — patrz R-12).
        await executor._async_execute_now()
        po_dwoch_tikach = len(decyzje_info())
        assert po_dwoch_tikach >= 1, "warunek testu: pierwszy tick musi coś zalogować"

        # Tick 3 i 4: DOKŁADNIE ten sam ustabilizowany stan co tick 2 (nic się nie
        # zmieniło od strony instalacji) — anty-spam z R-12 musi się tu domknąć.
        await executor._async_execute_now()
        await executor._async_execute_now()

        assert len(decyzje_info()) == po_dwoch_tikach, (
            "ustabilizowany stan (ta sama wymuszona akcja co tick wcześniej) nie może "
            "dokładać nowych INFO na każdym kolejnym ticku — RR-5: dwa wywołania "
            "_remember (wewnętrzne rekurencyjne i zewnętrzne scalone) nadpisywały "
            "sobie nawzajem _last_decision, więc porównanie 'decision != "
            "_last_decision' było prawdziwe zawsze, niezależnie od stabilizacji stanu"
        )

    # Strona ochrony R-1: mimo zmiany w logowaniu, plan wymuszony NADAL nie może
    # pójść do falownika jako rozładowanie — tylko tryb bezpieczny.
    assert "eco_discharge" not in _tryby(hass)
    assert executor._direction._current is Action.SELF_CONSUME


# ── RR-6: I-8 musi widzieć EFEKTYWNĄ akcję, nie żądaną ───────────────────────


@pytest.mark.asyncio
async def test_rr6_bez_slotu_forced_self_consume_rejestruje_efektywna_akcje(fake_entry):
    """Sonda P6: `SoC=30`, `soc_reserve=40`, `SET_WORK_MODE` bez slotu z komendą
    `{'mode': 'eco_discharge', 'discharge_limit': 50}`. I-1 blokuje rozładowanie
    (`forced_action=SELF_CONSUME`), do falownika nie idzie nic rozładowującego —
    ale `act = action or infer_action(...)` bierze akcję OD WOŁAJĄCEGO (DISCHARGE),
    więc `DirectionLimiter.record()` zapamiętuje kierunek, który guard właśnie
    zablokował."""
    hass = _hass(soc="30")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply(
        {"mode": "eco_discharge", "discharge_limit": 50.0},
        action=Action.DISCHARGE,
        source="cloud",
    )

    assert _tryby(hass) == [], "nic rozładowującego nie mogło pójść do falownika"
    assert result.status is Status.PARTIAL
    assert executor._direction._current is not Action.DISCHARGE, (
        "DirectionLimiter musi zapamiętać EFEKTYWNĄ akcję (self_consume, wymuszoną "
        "przez I-1), a nie żądaną (discharge) — inaczej: (1) późniejsze faktyczne "
        "przejście w rozładowanie nie policzy się jako zmiana kierunku, (2) pierwsze "
        "przejście w ładowanie zje slot budżetu na odwrócenie, które realnie nie zaszło"
    )
    assert executor._direction._current is Action.SELF_CONSUME


@pytest.mark.asyncio
async def test_rr6_ochrona_ze_slotem_dalej_rejestruje_poprawnie(fake_entry):
    """Strona ochrony: ścieżka ZE slotem (harmonogram) już wcześniej poprawnie
    rejestrowała efektywną akcję — przez rekurencyjny `async_apply` z jawnym
    `action=result.forced_action`. Zmiana w gałęzi BEZ slotu nie może tego zepsuć."""
    hass = _hass(soc="30")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    await executor.async_set_schedule(_schedule("discharge", soc_target=50.0))

    assert executor._direction._current is Action.SELF_CONSUME
    assert "eco_discharge" not in _tryby(hass)


@pytest.mark.asyncio
async def test_rr6_ochrona_akcja_niewymuszona_nadal_dziala(fake_entry):
    """Strona ochrony: gdy guardy NIC nie wymuszają (`forced_action is None`),
    zachowanie musi zostać identyczne jak przed RR-6 — efektywna akcja to nadal
    ta żądana przez wołającego."""
    hass = _hass(soc="80")  # SoC wysoko ponad rezerwą — I-1 nic nie blokuje
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply(
        {"mode": "eco_discharge", "discharge_limit": 50.0},
        action=Action.DISCHARGE,
        source="cloud",
    )

    assert result.forced_action is None, "warunek testu: guardy nic nie wymuszają"
    assert executor._direction._current is Action.DISCHARGE


# ── RR-7: async_diagnose nie może mutować DirectionLimitera ─────────────────


R7_OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
}


def _hass_r7(soc: str = "55") -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", soc)
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb", "general", {"options": ["general", "eco_charge", "eco_discharge"]}
    )
    hass.states.set("number.eco_soc", "20")
    return hass


def _schedule_charge() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "rr7",
        "slots": [
            {
                "from": (now - timedelta(minutes=10)).isoformat(),
                "to": (now + timedelta(minutes=50)).isoformat(),
                "mode": "charge",
                "power_w": 3000,
                "soc_target": 80.0,
                "price_pln_kwh": 0.30,
                "export_allowed": False,
            }
        ],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    }


@pytest.mark.asyncio
async def test_rr7_async_diagnose_nie_mutuje_historii_direction_limitera(fake_entry):
    """Sonda P3: `_history` przed = `[(t-7200, DISCHARGE)]`, po samym
    `async_diagnose()` = `[]`. `DirectionLimiter.allows()` woła `_prune`, które
    PRZYPISUJE `self._history` (nową, przyciętą listę) — nawet gdy wpis jest i
    tak poza oknem i logiczny wynik się nie zmienia, samo PRZYPISANIE jest
    mutacją stanu, którą kontrakt 'suchy przebieg' obiecuje wykluczyć."""
    hass = _hass_r7()
    fake_entry.options = dict(R7_OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_charge())

    stary_wpis = (time.monotonic() - 7200, Action.DISCHARGE)
    executor._direction._history = [stary_wpis]
    executor._direction._current = Action.DISCHARGE

    await executor.async_diagnose()

    assert executor._direction._history == [stary_wpis], (
        "async_diagnose (suchy przebieg) nie może mutować _history DirectionLimitera"
    )


@pytest.mark.asyncio
async def test_rr7_ochrona_diagnose_dalej_widzi_throttled(fake_entry):
    """Strona ochrony R-7: naprawa nie-mutującej ścieżki nie może cofnąć
    pierwotnej własności R-7 — `async_diagnose` nadal musi raportować
    `throttled`, gdy I-8 by zablokowało zmianę kierunku (T-8)."""
    hass = _hass_r7()
    fake_entry.options = dict(R7_OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_charge())
    executor._direction = DirectionLimiter(max_changes_per_hour=0)
    executor._direction._current = Action.DISCHARGE
    history_before = list(executor._direction._history)
    current_before = executor._direction._current

    report = await executor.async_diagnose()

    assert report["guards"]["status"] == "throttled"
    assert any(n["invariant"] == "I-8" for n in report["guards"]["notes"])
    assert report["would_write"] == {}
    assert executor._direction._history == history_before
    assert executor._direction._current == current_before


# ── R-12 (reszta): notatki i "Zapis wstrzymany" idą przez ten sam anty-spam ──


R12_OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
}


def _hass_swiezy(soc: str = "55") -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", soc)
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb", "general", {"options": ["general", "eco_charge", "eco_discharge"]}
    )
    hass.states.set("number.eco_soc", "20")
    return hass


@pytest.mark.asyncio
async def test_r12_reszta_degraded_notatki_i_warning_nie_spamuja_co_tick(fake_entry, caplog):
    """Domknięcie RESZTY R-12: pętla logująca notatki guardów (INFO) i "Zapis
    wstrzymany" (WARNING) stała PRZED sprawdzeniem `result.rejected`
    (`executor.py:207`) — logowała bezwarunkowo na KAŻDYM ticku ścieżki DEGRADED,
    mimo że linia "Decyzja" już miała anty-spam z R-12. Wszystkie linie decyzyjne
    mają podlegać tej samej regule: INFO/WARNING przy ZMIANIE, DEBUG przy powtórce."""
    hass = _hass_swiezy(soc="55")
    fake_entry.options = dict(R12_OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    stary = datetime.now(timezone.utc) - timedelta(minutes=30)
    hass.states.set("sensor.soc", "55", last_updated=stary)
    hass.states.set("sensor.pv", "0", last_updated=stary)
    hass.states.set("sensor.grid", "0", last_updated=stary)

    def rekordy_i9() -> list[logging.LogRecord]:
        return [r for r in caplog.records if "guard I-9" in r.message]

    def warningi_wstrzymane() -> list[logging.LogRecord]:
        return [r for r in caplog.records if "Zapis wstrzymany" in r.message]

    with caplog.at_level(logging.DEBUG):
        r1 = await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )
        r2 = await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )
        r3 = await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )

        assert r1.status is Status.DEGRADED
        assert r2.status is Status.DEGRADED
        assert r3.status is Status.DEGRADED

        i9 = rekordy_i9()
        assert len(i9) == 3, (
            "notatka I-9 musi nadal pojawić się na KAŻDYM ticku (choćby na DEBUG) — "
            "to nie jest zniknięcie logu, tylko zmiana poziomu przy powtórce"
        )
        assert [r.levelno for r in i9] == [logging.INFO, logging.DEBUG, logging.DEBUG], (
            "pierwsze wystąpienie tej przyczyny DEGRADED musi iść na INFO, kolejne "
            "identyczne (bez zmiany decyzji) na DEBUG — dokładnie jak linia 'Decyzja'"
        )

        wstrzymane = warningi_wstrzymane()
        assert len(wstrzymane) == 3
        assert [r.levelno for r in wstrzymane] == [
            logging.WARNING, logging.DEBUG, logging.DEBUG,
        ], "WARNING 'Zapis wstrzymany' podlega tej samej regule anty-spamu"


@pytest.mark.asyncio
async def test_r12_reszta_ochrona_zmiana_przyczyny_znowu_loguje_info(fake_entry, caplog):
    """Strona ochrony: to NIE ma być trwałe wyciszenie — jeśli przyczyna DEGRADED
    się zmieni (inny komunikat I-9), musi to wygenerować NOWY INFO/WARNING, tak
    samo jak linia 'Decyzja' reaguje na zmianę treści decyzji."""
    hass = _hass_swiezy(soc="55")
    fake_entry.options = dict(R12_OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    def rekordy_i9_info() -> list[logging.LogRecord]:
        return [
            r for r in caplog.records
            if r.levelno == logging.INFO and "guard I-9" in r.message
        ]

    with caplog.at_level(logging.DEBUG):
        # Przyczyna 1: SoC poza fizyką.
        hass.states.set("sensor.soc", "150")
        r1 = await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )
        assert r1.status is Status.DEGRADED

        # Przyczyna 2: brak odczytu SoC — INNY komunikat I-9, więc mimo że oba są
        # DEGRADED, to inna DECYZJA — musi znów zalogować na INFO.
        hass.states.set("sensor.soc", "unknown")
        r2 = await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )
        assert r2.status is Status.DEGRADED

        assert len(rekordy_i9_info()) == 2, (
            "zmiana przyczyny DEGRADED (inny komunikat I-9) musi zalogować NOWY INFO, "
            "nie zostać wyciszona jako 'ta sama decyzja'"
        )
