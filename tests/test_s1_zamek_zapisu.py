"""S-1 — dwie pętle piszą do falownika równocześnie (brak zamka na torze zapisu).

Ustalenie S-1 z audytu świeżym okiem (`2026-08-16-faza-a-audyt-swiezym-okiem.md`):
`async_apply` jest wołane z DWÓCH niezależnych pętli — ticku co 60 s i
`command_handler` obsługującego komendy z chmury. Przeplot na `await` w
`applier.apply_params` daje TRWAŁY rozjazd: na falowniku zostaje wartość jednego
przebiegu, a `WriteThrottle._last_value` pamięta wartość drugiego. Od tej chwili
I-6 uznaje wartość planu za „bez zmiany" i NIGDY jej nie dopisze — plan i
rzeczywistość rozjeżdżają się na stałe, bez żadnego sygnału.

Sonda A z dokumentu (odtworzona w `test_s1_przeplot_...`): plan chce `eco_soc=77`,
w trakcie zapisu przychodzi z chmury `SET_WORK_MODE` z `eco_soc=55`. Bez zamka
realne service calls idą w kolejności:

    select.tryb(A) → select.tryb(B) → number.eco_soc=77(A) → number.eco_soc=55(B)
    → number.eco_power(A) → switch(A)

Oba przebiegi zwracają `success`, fizycznie zostaje 55, a throttle pamięta 77.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter import executor as executor_module
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Action as HaAction, Status
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
    "soc_reserve": 40.0,
    "user_mode": "autarky",
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
    hass.states.set("number.discharge_limit", "0")
    return hass


def _schedule(mode: str, *, soc_target: float, power_w: float = 5000.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "s1",
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


class _Falownik:
    """Symulator fizycznego urządzenia + rejestr, KTÓRY przebieg wykonał dany zapis.

    Każdy service call oddaje sterowanie (`asyncio.sleep(0)`) — dokładnie tak jak
    realne `hass.services.async_call(..., blocking=True)`. To jest punkt przeplotu,
    w którym S-1 się materializuje. Autora zapisu rozpoznajemy po nazwie taska,
    bo to jedyny sposób, żeby sprawdzić, czy PARAM_ORDER nie został złamany
    MIĘDZY przebiegami.
    """

    def __init__(self, hass: FakeHass) -> None:
        self.hass = hass
        self.stan: dict[str, object] = {}
        self.slad: list[tuple[str, str]] = []  # (przebieg, "encja=wartość")
        self.hook = None  # opcjonalny callback po każdym zapisie

    async def async_call(self, domain, service, data, blocking=False, **_kwargs):
        task = asyncio.current_task()
        kto = task.get_name() if task is not None else "?"
        entity_id = data.get("entity_id", "")
        if domain == "switch":
            wartosc: object = service == "turn_on"
        elif domain == "select":
            wartosc = data.get("option")
        else:
            wartosc = data.get("value")
        # Yield PRZED zapisem i PO nim — realny service call zawiesza korutynę
        # w obu tych punktach (kolejka HA + oczekiwanie na odpowiedź encji).
        await asyncio.sleep(0)
        self.stan[entity_id] = wartosc
        self.slad.append((kto, f"{entity_id}={wartosc}"))
        if self.hook is not None:
            self.hook(kto, entity_id)
        await asyncio.sleep(0)

    def bloki(self) -> list[str]:
        """Ciąg przebiegów w kolejności zapisów, ze sklejonymi powtórzeniami.

        `['A', 'B']` = przebiegi się NIE przeplotły. `['A', 'B', 'A', ...]` =
        gwarancja PARAM_ORDER została złamana między przebiegami.
        """
        out: list[str] = []
        for kto, _ in self.slad:
            if not out or out[-1] != kto:
                out.append(kto)
        return out

    def zapisy(self, kto: str) -> list[str]:
        return [wpis for autor, wpis in self.slad if autor == kto]


def _podepnij(hass: FakeHass) -> _Falownik:
    falownik = _Falownik(hass)
    hass.services.async_call = falownik.async_call  # type: ignore[assignment]
    return falownik


async def _rownolegle(coro_a, coro_b):
    """Uruchom dwa przebiegi tak, jak robią to dwie niezależne pętle produkcyjne.

    `A` startuje pierwszy (tick / harmonogram), `B` wchodzi w trakcie (chmura).
    `wait_for` jest tu ochroną przed DEADLOCKIEM: przy naiwnym `asyncio.Lock`
    rekurencyjne wywołanie z gałęzi `forced_action` (R-1) zawiesza się na zawsze.
    """
    task_a = asyncio.create_task(coro_a, name="A")
    task_b = asyncio.create_task(coro_b, name="B")
    return await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5.0)


# ── Sonda A: przeplot dwóch przebiegów ───────────────────────────────────────


@pytest.mark.asyncio
async def test_s1_przeplot_nie_rozjezdza_falownika_z_pamiecia_throttle(fake_entry):
    """Sonda A wprost z ustalenia S-1 — stan musi być SPÓJNY.

    Przed naprawą: na `number.eco_soc` zostaje 55 (B commitował fizycznie jako
    ostatni), a `_throttle._last_value['eco_soc']` = 77 (A commitował logicznie
    jako ostatni). To rozjazd TRWAŁY — I-6 nigdy już nie dopisze 77.
    """
    hass = _hass(soc="60")
    falownik = _podepnij(hass)
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    await _rownolegle(
        executor.async_set_schedule(_schedule("charge", soc_target=77.0)),
        executor.async_apply(
            {"mode": "eco_charge", "eco_soc": 55.0},
            action=HaAction.CHARGE,
            source="cloud",
        ),
    )

    pamietane = executor._throttle._last_value.get("eco_soc")
    fizyczne = falownik.stan.get("number.eco_soc")
    assert fizyczne == pamietane, (
        f"S-1: throttle pamięta {pamietane}, a na falowniku jest {fizyczne} — "
        f"od tej chwili I-6 uzna wartość planu za 'bez zmiany' i nigdy jej nie dopisze. "
        f"Ślad zapisów: {falownik.slad}"
    )


@pytest.mark.asyncio
async def test_s1_przeplot_nie_lamie_param_order_miedzy_przebiegami(
    fake_entry, monkeypatch
):
    """Drugi skutek S-1: `mode` jednego przebiegu ląduje PO limitach drugiego.

    PARAM_ORDER istnieje, bo w części falowników zmiana trybu resetuje limity
    (I-3 / luka L-6). Gwarancja obowiązuje wewnątrz przebiegu — a przeplot łamie
    ją MIĘDZY przebiegami, czyli dokładnie tam, gdzie nikt jej nie pilnował.

    I-6 (min. odstęp między zapisami tego samego parametru) wyłączony na czas
    testu, bo inaczej drugi przebieg nie zapisałby NIC i przeplot byłby
    niewidoczny — a przedmiotem tego testu jest kolejność, nie throttling.
    """
    monkeypatch.setattr(executor_module, "WRITE_MIN_INTERVAL_S", 0.0)
    hass = _hass(soc="60")
    falownik = _podepnij(hass)
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    await _rownolegle(
        executor.async_set_schedule(_schedule("charge", soc_target=77.0)),
        executor.async_apply(
            {"mode": "eco_discharge", "eco_soc": 55.0, "eco_power": 30.0},
            action=HaAction.DISCHARGE,
            source="cloud",
        ),
    )

    assert falownik.bloki() == ["A", "B"], (
        f"S-1: zapisy dwóch przebiegów przeplotły się — {falownik.slad}"
    )
    # Kontrola sensu testu: oba przebiegi naprawdę coś zapisały, więc powyższa
    # asercja nie przeszła "przez pustkę".
    assert len(falownik.zapisy("A")) >= 2 and len(falownik.zapisy("B")) >= 2


# ── Pułapka: rekurencja forced_action (R-1) nie może wpaść w deadlock ────────


@pytest.mark.asyncio
async def test_s1_przemapowanie_forced_action_nie_zakleszcza_sie(fake_entry):
    """R-1 woła `async_apply` REKURENCYJNIE z wnętrza `async_apply`.

    Naiwny `asyncio.Lock` na publicznej metodzie zawiesiłby ten przebieg na zawsze
    (zamek nie jest reentrantny). `wait_for` w `_rownolegle`/tutaj zamienia
    deadlock w czytelny `TimeoutError` zamiast zawieszenia całej sesji pytest.
    """
    hass = _hass(soc="30")  # poniżej rezerwy 40% → I-1 wymusi self_consume
    falownik = _podepnij(hass)
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    result = await asyncio.wait_for(
        executor.async_set_schedule(_schedule("discharge", soc_target=50.0)),
        timeout=5.0,
    )

    assert falownik.stan.get("select.tryb") == "general", (
        "przemapowanie na tryb bezpieczny (R-1) musi nadal dojść do falownika"
    )
    assert any(n.invariant == "I-1" for n in result.notes)
    assert result.status is Status.PARTIAL


@pytest.mark.asyncio
async def test_s1_przemapowanie_forced_action_pod_rownoczesna_komenda(fake_entry):
    """Ta sama rekurencja, ale z drugą pętlą dobijającą się do zamka.

    Pilnuje, że wydzielenie części bez zamka nie otworzyło furtki: przebieg
    wewnętrzny (przemapowany) nadal musi być nierozdzielny od zewnętrznego.
    """
    hass = _hass(soc="30")
    falownik = _podepnij(hass)
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    await _rownolegle(
        executor.async_set_schedule(_schedule("discharge", soc_target=50.0)),
        executor.async_apply(
            {"mode": "eco_charge", "eco_soc": 55.0},
            action=HaAction.CHARGE,
            source="cloud",
        ),
    )

    assert falownik.bloki() == ["A", "B"] or falownik.bloki() == ["A"], (
        f"S-1: przebieg przemapowany (R-1) przeplótł się z komendą z chmury — "
        f"{falownik.slad}"
    )
    pamietane = executor._throttle._last_value.get("eco_soc")
    assert falownik.stan.get("number.eco_soc") == pamietane


# ── Decyzja: przebieg czekający na zamek widzi ODŚWIEŻONY stan ──────────────


@pytest.mark.asyncio
async def test_s1_przebieg_czekajacy_na_zamek_czyta_stan_po_zamku(fake_entry):
    """Świat mógł się zmienić, zanim czekający przebieg dostał zamek.

    Zamek jest brany PRZED odczytem stanu urządzenia, więc przebieg, który czekał,
    przelicza guardy na rzeczywistości PO cudzym zapisie, a nie na migawce sprzed
    kolejki. Tu: w trakcie przebiegu A SoC spada do 30% (rezerwa 40%), więc
    rozładowanie żądane przez B musi zostać zdławione przez I-1.

    Bez zamka B odczytał stan zanim A skończył — widział SoC 42% i wpychał
    `eco_discharge` do falownika poniżej rezerwy użytkownika.

    Spadek jest celowo mały (42% → 38%, rezerwa 40%): mieści się w progu I-9
    (skok SoC fizycznie możliwy), więc test rozstrzyga o I-1, a nie o odrzuceniu
    całego odczytu jako awarii czujnika.
    """
    hass = _hass(soc="42")
    falownik = _podepnij(hass)
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    def _spadek_soc(kto: str, entity_id: str) -> None:
        # Ostatni zapis przebiegu A (przełącznik limitu eksportu) — od tej chwili
        # bateria jest poniżej rezerwy.
        if kto == "A" and entity_id == "switch.export_limit":
            hass.states.set("sensor.soc", "38")

    falownik.hook = _spadek_soc

    _, wynik_b = await _rownolegle(
        executor.async_set_schedule(_schedule("charge", soc_target=77.0)),
        executor.async_apply(
            {"mode": "eco_discharge", "eco_soc": 55.0, "eco_power": 30.0},
            action=HaAction.DISCHARGE,
            source="cloud",
        ),
    )

    assert falownik.stan.get("select.tryb") != "eco_discharge", (
        f"I-1: rozładowanie poniżej rezerwy nie może pójść do falownika — "
        f"{falownik.slad}"
    )
    assert any(n.invariant == "I-1" for n in wynik_b.notes), (
        f"przebieg czekający na zamek musi przeliczyć guardy na świeżym stanie, "
        f"noty: {[(n.invariant, n.message) for n in wynik_b.notes]}"
    )
