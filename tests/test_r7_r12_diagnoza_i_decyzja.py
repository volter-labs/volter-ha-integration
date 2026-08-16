"""R-7, R-12 — ustalenia z przeglądu adwersaryjnego Fazy A
(`docs/analysis/2026-08-16-faza-a-review.md`).

R-7 (WYSOKA): `async_diagnose` nie odpytuje `self._direction` (I-8) i nie sprawdza,
czy encje są zmapowane. Raportuje `guards.status='success'` i pełne `would_write`
w sytuacjach, w których realny `async_apply` zwróciłby `THROTTLED` albo nie
zapisał nic (encja bez mapowania w opcjach integracji, R-3). To narzędzie służy
do diagnozy smoke testu Fazy C — musi mówić prawdę dokładnie w tych przypadkach,
w których "nic się nie dzieje". KRYTYCZNE: sprawdzenie I-8 nie może zmienić stanu
`DirectionLimiter` — używamy `allows`, nigdy `record`.

R-12 (ŚREDNIA): blok aktualizujący `_last_decision` stoi po wszystkich wczesnych
returnach `async_apply`. `_last_decision` nie jest aktualizowany przy
DEGRADED/THROTTLED/ERROR/błędzie sanityzacji, więc powrót do normalnej pracy po
awarii nie generuje żadnego INFO (decyzja sprzed awarii wciąż jest "ostatnia
znana", więc identyczna decyzja po awarii wygląda jak "bez zmian"). Klucz decision
ma też uwzględniać `executed`, żeby przebieg bez zapisu był odróżnialny od
przebiegu, który zapisał wszystko.

Specyfikacja: `volter-box/03-produkt/guardy-i-inwarianty.md` (I-8, I-9, T-8, T-9).
"""

from __future__ import annotations

import logging
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
}


def _hass_ze_swiezym_stanem(soc: str = "55") -> FakeHass:
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
        "schedule_id": "r7",
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


# ── R-7: async_diagnose musi odpytać I-8 (DirectionLimiter) ─────────────────


@pytest.mark.asyncio
async def test_r7_diagnose_ignoruje_i8_throttled(fake_entry):
    """Wektor T-8: piąta zmiana kierunku w ciągu godziny jest zignorowana, status
    `throttled`, zero zapisów. Ustawiamy `max_changes_per_hour=0`, żeby KAŻDA
    zmiana kierunku była zablokowana od razu — deterministyczne odwzorowanie T-8
    bez budowania historii. Realny `async_apply` zwróciłby THROTTLED, więc
    `async_diagnose` nie może raportować `status='success'` i pełnego `would_write`."""
    hass = _hass_ze_swiezym_stanem()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_charge())
    executor._direction = DirectionLimiter(max_changes_per_hour=0)
    history_before = list(executor._direction._history)
    current_before = executor._direction._current

    report = await executor.async_diagnose()

    assert report["guards"]["status"] == "throttled", (
        "diagnose musi pokazać, że I-8 zablokowałoby zmianę kierunku — inaczej mówi "
        "'success' dokładnie tam, gdzie realny async_apply zwróciłby THROTTLED"
    )
    assert any(n["invariant"] == "I-8" for n in report["guards"]["notes"])
    assert report["would_write"] == {}, "THROTTLED = zero zapisów (T-8)"

    # KRYTYCZNE: sprawdzenie I-8 w diagnose to `allows`, nigdy `record` — suchy
    # przebieg nie wolno mu zafałszować kolejnego realnego wykonania.
    assert executor._direction._history == history_before
    assert executor._direction._current == current_before


@pytest.mark.asyncio
async def test_r7_diagnose_nie_zmienia_zadnego_stanu(fake_entry):
    """Potwierdzona własność z ustalenia R-7 (i wymóg zadania): zero zapisów, zero
    mutacji WriteThrottle, DirectionLimiter, _prev_soc i _last — musi przetrwać
    dołożenie sprawdzenia I-8."""
    hass = _hass_ze_swiezym_stanem()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_charge())
    executor._prev_soc = 42.0
    prev_soc_before = executor._prev_soc
    last_before = dict(executor._last)
    throttle_last_value_before = dict(executor._throttle._last_value)
    direction_history_before = list(executor._direction._history)
    direction_current_before = executor._direction._current

    await executor.async_diagnose()

    assert hass.services.calls == [], "diagnose nie może wołać żadnego serwisu"
    assert executor._prev_soc == prev_soc_before
    assert executor._last == last_before
    assert executor._throttle._last_value == throttle_last_value_before
    assert executor._direction._history == direction_history_before
    assert executor._direction._current == direction_current_before


# ── R-7: async_diagnose musi sprawdzić, czy encje są zmapowane ──────────────


@pytest.mark.asyncio
async def test_r7_diagnose_ignoruje_niezmapowana_encje(fake_entry):
    """`entity_eco_mode_soc` świadomie NIE jest w opcjach integracji. Realny
    `async_apply` przekazałby `eco_soc` do `applier.apply_params`, ten zwróciłby
    błąd R-3 (encja niezmapowana) i NIC by nie zapisał dla tego parametru —
    `would_write` nie może pokazywać zapisu, który nigdy by nie poszedł."""
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set("select.tryb", "general", {"options": ["general", "eco_charge"]})
    fake_entry.options = {
        "entity_soc": "sensor.soc",
        "entity_pv_power": "sensor.pv",
        "entity_grid_power": "sensor.grid",
        "entity_ems_mode": "select.tryb",
        # entity_eco_mode_soc świadomie NIE zmapowane
    }
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_charge())

    report = await executor.async_diagnose()

    assert "eco_soc" not in report["would_write"], (
        "eco_soc nie ma zmapowanej encji — realny async_apply zgłosiłby R-3 i nic by "
        "nie zapisał; would_write nie może udawać, że ten zapis by poszedł"
    )
    assert "mode" in report["would_write"], "mode JEST zmapowany — musi zostać"


# ── R-12: _last_decision na wszystkich ścieżkach wyjścia ─────────────────────


@pytest.mark.asyncio
async def test_r12_last_decision_aktualizowany_na_wczesnym_returnie_degraded(fake_entry):
    """Scenariusz wprost z ustalenia R-12: `_last_decision` musi się zmienić także
    na ścieżce DEGRADED (I-9, wczesny return), inaczej powrót do normalnej pracy
    po awarii nie generuje żadnego nowego INFO, bo decyzja sprzed awarii wciąż
    jest "ostatnia znana" i identyczna decyzja po awarii wygląda jak "bez zmian"."""
    hass = _hass_ze_swiezym_stanem(soc="55")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    # Tick 1: normalna praca, SUCCESS — zapisuje _last_decision.
    await executor.async_apply({"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test")
    decision_before_outage = executor._last_decision
    assert decision_before_outage is not None

    # Tick 2: telemetria przestarzała -> I-9 -> DEGRADED, wczesny return.
    stary = datetime.now(timezone.utc) - timedelta(minutes=30)
    hass.states.set("sensor.soc", "55", last_updated=stary)
    hass.states.set("sensor.pv", "0", last_updated=stary)
    hass.states.set("sensor.grid", "0", last_updated=stary)
    result = await executor.async_apply(
        {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
    )
    assert result.status is Status.DEGRADED

    assert executor._last_decision != decision_before_outage, (
        "_last_decision musi się zaktualizować na wczesnym returnie DEGRADED — "
        "inaczej powrót do normalnej pracy po awarii (ta sama decyzja co przed nią) "
        "wygląda jak 'bez zmian' i nie generuje żadnego INFO"
    )


@pytest.mark.asyncio
async def test_r12_powrot_po_awarii_generuje_info(fake_entry, caplog):
    """Ten sam scenariusz, ale sprawdzony na poziomie logu: tick1 SUCCESS loguje
    'Decyzja', tick2 DEGRADED, tick3 wraca do DOKŁADNIE tej samej decyzji co
    tick1 — musi znów zalogować 'Decyzja' na INFO, bo między nimi była awaria."""
    hass = _hass_ze_swiezym_stanem(soc="55")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    def decyzje_info() -> list[str]:
        return [
            r.message for r in caplog.records
            if r.levelno == logging.INFO and "Decyzja:" in r.message
        ]

    with caplog.at_level(logging.INFO):
        await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )
        assert len(decyzje_info()) == 1

        stary = datetime.now(timezone.utc) - timedelta(minutes=30)
        hass.states.set("sensor.soc", "55", last_updated=stary)
        hass.states.set("sensor.pv", "0", last_updated=stary)
        hass.states.set("sensor.grid", "0", last_updated=stary)
        await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )

        # Powrót: telemetria znów świeża, identyczna komenda jak w ticku 1. Czyścimy
        # pamięć I-6, żeby zapis eco_soc poszedł ponownie identycznie jak w ticku 1
        # (inaczej throttle I-6 pominąłby niezmienioną wartość i `executed` różniłoby
        # się od ticku 1 z innego powodu niż awaria — to zaciemniłoby scenariusz).
        hass.states.set("sensor.soc", "55")
        hass.states.set("sensor.pv", "1200")
        hass.states.set("sensor.grid", "-300")
        executor._throttle._last_value.clear()
        executor._throttle._last_write_ts.clear()
        await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )

        # 3 przejścia = 3 różne decyzje = 3 logi INFO: sukces (tick1), degraded
        # (tick2), znów sukces o parametrach IDENTYCZNYCH jak w ticku1 (tick3).
        # Kluczowe jest trzecie: bez naprawy R-12 `_last_decision` zostałoby
        # zamrożone na wartości z ticku1 (bo tick2 nigdy go nie zaktualizował),
        # więc tick3 wyglądałby na "bez zmian" i NIE zalogowałby INFO.
        wiadomosci = decyzje_info()
        assert len(wiadomosci) == 3, (
            "powrót do normalnej pracy po awarii musi wygenerować NOWE 'Decyzja' "
            "na INFO, mimo że parametry i status są identyczne jak przed awarią"
        )
        assert wiadomosci[0] == wiadomosci[2], (
            "tick1 i tick3 to ta sama decyzja tekstowo — potwierdza, że różnica "
            "wynika WYŁĄCZNIE z przejścia przez DEGRADED między nimi, a nie z innych "
            "parametrów"
        )


@pytest.mark.asyncio
async def test_r12_degraded_nie_spamuje_co_tick(fake_entry, caplog):
    """Ustalenie R-12: ścieżka DEGRADED powinna korzystać z tego samego anty-spamu
    co ścieżka normalna — dwa kolejne tiki z TĄ SAMĄ przyczyną DEGRADED logują
    'Decyzja' na INFO tylko raz, nie za każdym razem."""
    hass = _hass_ze_swiezym_stanem(soc="55")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    stary = datetime.now(timezone.utc) - timedelta(minutes=30)
    hass.states.set("sensor.soc", "55", last_updated=stary)
    hass.states.set("sensor.pv", "0", last_updated=stary)
    hass.states.set("sensor.grid", "0", last_updated=stary)

    def decyzje_info() -> list[str]:
        return [
            r.message for r in caplog.records
            if r.levelno == logging.INFO and "Decyzja:" in r.message
        ]

    with caplog.at_level(logging.INFO):
        r1 = await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )
        r2 = await executor.async_apply(
            {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
        )

        assert r1.status is Status.DEGRADED and r2.status is Status.DEGRADED
        assert len(decyzje_info()) == 1, (
            "ta sama przyczyna DEGRADED dwa tiki z rzędu — druga 'Decyzja' musi iść "
            "na DEBUG, nie spamować INFO co 60s"
        )


@pytest.mark.asyncio
async def test_r12_klucz_decyzji_uwzglednia_executed(fake_entry):
    """Klucz `decision` ma uwzględniać też `executed` — przebieg, który policzył
    te same parametry i status, ale NIC nie zapisał (bo throttle I-6 odfiltrował
    wszystko po stronie identycznych wartości), musi być odróżnialny od przebiegu,
    który faktycznie zapisał."""
    hass = _hass_ze_swiezym_stanem(soc="55")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    result1 = await executor.async_apply(
        {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
    )
    decision_after_write = executor._last_decision
    assert result1.executed, "warunek testu: pierwszy zapis musi faktycznie pójść"
    assert "executed=" in decision_after_write

    # I-6 pomija identyczny zapis w odstępie < 60s -> ten sam status/params, ale
    # `executed` puste. Klucz decyzji musi to odróżnić.
    result2 = await executor.async_apply(
        {"eco_soc": 30.0}, action=Action.SELF_CONSUME, source="test"
    )
    assert result2.executed == [], "warunek testu: I-6 musi pominąć niezmieniony zapis"
    assert executor._last_decision != decision_after_write, (
        "params/status są te same co w ticku 1, ale executed puste zamiast pełnego — "
        "klucz decyzji musi to odróżnić, inaczej 'nic nie zapisano' i 'zapisano "
        "wszystko' są nierozróżnialne na poziomie INFO"
    )
