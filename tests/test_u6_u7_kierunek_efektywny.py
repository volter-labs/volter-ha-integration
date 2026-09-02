"""U-6 / U-7 — kierunek efektywny rozstrzygany PRZED guardami, jedno źródło prawdy.

Ustalenia **U-6 (KRYTYCZNA)** i **U-7 (WYSOKA)** z aneksu „regresja po Tasku 13"
w `docs/analysis/2026-08-16-kontrakt-mocy.md`. Jedna przyczyna źródłowa, wprowadzona
commitem 93796ae (Task 13):

`mappers._kierunek` był JEDYNYM miejscem wyprowadzającym kierunek efektywny ze slotu
(`mode` + `charge_source` + `discharge_purpose`) — i stał ZA guardami. Executor podawał
do `GuardContext` `action=slot.action`, a `slot.action` dla 41% slotów z mocą wynosi
zawsze `SELF_CONSUME`, bo chmura świadomie nie podnosi ich do `mode='discharge'`.

Skutki, oba zmierzone na pełnym torze (JSON → Store → guardy → service calls):

  * **U-6:** SoC=10%, `soc_reserve`=40%, slot `{mode:'self_consume',
    discharge_purpose:'self', power_w:1476, soc_target:10}` → na falownik szło
    `select.tryb='discharge_battery'` i `number.charge_limit=14.8`, `forced_action=None`.
    Rezerwa backup nie działała wcale — czyli R-1 wskrzeszone nową ścieżką.
  * **U-7:** `act` liczyło się ze `slot.action`, więc `DirectionLimiter` (I-8) nigdy
    nie widział ani `CHARGE`, ani `DISCHARGE` — 12 planów co 120 s dawało 12 przełączeń
    trybu falownika przy `_direction._current` bez zmian.

Naprawa jest strukturalna: kierunek wyprowadza funkcja CZYSTA (`schedule.kierunek_slotu`,
bez importów HA — do przepisania na C w firmware razem z resztą `guards.py`/`schedule.py`),
używana w OBU miejscach: w executorze przed zbudowaniem `GuardContext` (`akcja_efektywna`)
oraz w mapperze przy wyborze trybu. Mapper jest WYKONAWCĄ decyzji, nie jej autorem —
dokładnie tak, jak ustalono przy R-1.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import custom_components.volter.executor as executor_module
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Action as AkcjaHA, Status as StatusHA
from custom_components.volter.schedule import akcja_efektywna as akcja_efektywna_ha
from tests.conftest import FakeHass, ZegarSterowany

import volter_pure.mappers as mappers_module
import volter_pure.schedule as schedule_module
from volter_pure.guards import Action
from volter_pure.schedule import Slot, akcja_efektywna, kierunek_slotu

# Harness ładuje moduły czyste DRUGI RAZ pod pakietem `volter_pure` (patrz conftest),
# więc `volter_pure.guards.Action` i `custom_components.volter.guards.Action` to dwa
# różne obiekty enuma. Testy funkcji czystej używają `Action`, testy pełnego toru
# przez executor — `AkcjaHA`/`StatusHA`. Porównania `is` inaczej zawsze fałszywe.

POCZATEK = datetime(2026, 8, 16, 22, 0, tzinfo=timezone.utc)
KONIEC = datetime(2026, 8, 16, 23, 0, tzinfo=timezone.utc)

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_eco_mode_power": "number.eco_power",
    "entity_charge_limit": "number.charge_limit",
    "entity_soc_upper": "number.soc_upper",
    "entity_export_limit": "number.export_limit",
    "entity_export_limit_switch": "switch.export_limit",
    "soc_reserve": 20.0,
    "user_mode": "autarky",
    "rated_power_w": 10000.0,
}


def _slot(**kwargs) -> Slot:
    baza = {"start": POCZATEK, "end": KONIEC, "action": Action.SELF_CONSUME}
    baza.update(kwargs)
    return Slot(**baza)


def _hass(soc: str = "60") -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", soc)
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb",
        "auto",
        {"options": ["auto", "charge_pv", "discharge_pv", "import_ac", "export_ac",
                     "conserve", "off_grid", "battery_standby", "buy_power",
                     "sell_power", "charge_battery", "discharge_battery"]},
    )
    hass.states.set("number.eco_soc", "20")
    hass.states.set("number.eco_power", "0")
    hass.states.set("number.export_limit", "0", {"min": 0, "max": 30000})
    return hass


def _zapisy(hass: FakeHass) -> dict[str, object]:
    """Ostatnia wartość, która poszła na każdą encję."""
    out: dict[str, object] = {}
    for _domain, service, data in hass.services.calls:
        encja = data.get("entity_id", "")
        wartosc = data.get("option", data.get("value", service))
        # Etap 3: na encję progu idzie GŁĘBOKOŚĆ rozładowania (DoD), bo `eco_mode_soc`
        # w GoodWe jest read-only. Odwracamy z powrotem na próg, żeby asercje mówiły
        # o rezerwie — o tym są te testy — a nie o wielkości odwrotnej.
        if encja == "number.eco_soc" and isinstance(wartosc, (int, float)):
            wartosc = round(100.0 - wartosc, 1)
        out[encja] = wartosc
    return out


def _tryby(hass: FakeHass) -> list[str]:
    """WSZYSTKIE nastawy trybu, w kolejności — U-7 liczy przełączenia, nie stan końcowy."""
    return [
        data["option"]
        for domain, _service, data in hass.services.calls
        if domain == "select" and data.get("entity_id") == "select.tryb"
    ]


def _plan(schedule_id: str, teraz: datetime, **pola) -> dict:
    slot = {
        "from": (teraz - timedelta(minutes=10)).isoformat(),
        "to": (teraz + timedelta(minutes=50)).isoformat(),
        "mode": "self_consume",
        "export_allowed": False,
    }
    slot.update(pola)
    return {
        "schedule_id": schedule_id,
        "slots": [slot],
        "fallback": {"mode": "self_consume", "soc_reserve": 40.0},
    }


# ── Część 1: funkcja czysta — JEDNO źródło prawdy o kierunku ─────────────────


@pytest.mark.parametrize(
    "slot,oczekiwany",
    [
        (_slot(action=Action.DISCHARGE, discharge_purpose="sell"), Action.DISCHARGE),
        (_slot(discharge_purpose="self"), Action.DISCHARGE),
        (_slot(action=Action.CHARGE, charge_source="grid"), Action.CHARGE),
        (_slot(charge_source="grid"), Action.CHARGE),
        # `pv` to świadomy BRAK kierunku (hipoteza H-1: eco_charge dobrałby prąd z sieci).
        # H-1 WYCOFANA (Etap 3): falownik ma tryb CHARGE_PV z Xset=0, więc
        # ładowanie nadwyżką jest kierunkiem jak każdy inny.
        (_slot(action=Action.CHARGE, charge_source="pv"), Action.CHARGE),
        (_slot(charge_source="pv"), Action.CHARGE),
        (_slot(power_w=2500.0), None),
        (_slot(action=Action.IDLE), None),
    ],
)
def test_kierunek_slotu_jest_funkcja_czysta_i_pokrywa_kazdy_ksztalt(slot, oczekiwany):
    """Kierunek efektywny da się policzyć BEZ Home Assistanta i bez mappera."""
    assert kierunek_slotu(slot) is oczekiwany


def test_akcja_efektywna_nigdy_nie_zwraca_none():
    """`GuardContext.action` musi dostać akcję, a nie „może być None" (I-1/I-2/I-8)."""
    assert akcja_efektywna(_slot(discharge_purpose="self")) is Action.DISCHARGE
    assert akcja_efektywna(_slot(charge_source="pv")) is Action.CHARGE
    assert akcja_efektywna(_slot(action=Action.CHARGE, charge_source="pv")) is Action.CHARGE
    assert akcja_efektywna(_slot(action=Action.IDLE)) is Action.IDLE


def test_mapper_i_executor_czytaja_TEN_SAM_kierunek():
    """U-6 wraca w chwili, w której te dwa miejsca się rozjadą — dlatego to jest test.

    Mapper ma tłumaczyć intencję na nastawy, a nie wyprowadzać ją po swojemu.
    """
    assert mappers_module.kierunek_slotu is schedule_module.kierunek_slotu
    assert executor_module.akcja_efektywna is akcja_efektywna_ha


@pytest.mark.parametrize(
    "slot",
    [
        _slot(action=Action.DISCHARGE, discharge_purpose="sell", power_w=5000.0),
        _slot(discharge_purpose="self", power_w=1476.0),
        _slot(action=Action.CHARGE, charge_source="grid", power_w=3000.0),
        _slot(action=Action.CHARGE, charge_source="pv", power_w=3000.0),
        _slot(charge_source="pv", power_w=2000.0),
        _slot(charge_source="grid", power_w=2000.0),
        _slot(power_w=2500.0),
        _slot(action=Action.IDLE, power_w=1000.0),
    ],
)
def test_tryb_falownika_jest_zawsze_tlumaczeniem_akcji_efektywnej(slot):
    """Inwariant U-6: falownik NIE MOŻE dostać trybu innego, niż widziały guardy.

    Guardy pracują na `akcja_efektywna(slot)`, więc jeżeli nastawa `mode` przestanie
    być jej dosłownym tłumaczeniem, ochrona chroni jedną intencję, a urządzenie
    wykonuje drugą — czyli dokładnie wada U-6, tylko od drugiej strony.
    """
    params = mappers_module.slot_to_params(slot)

    oczekiwany = mappers_module.GOODWE_MODE_MAP[akcja_efektywna(slot)]
    if slot.discharge_purpose == "sell" and akcja_efektywna(slot) is Action.DISCHARGE:
        # Trzeci udokumentowany wyjątek (2026-09-02): SPRZEDAŻ idzie `sell_power`,
        # nie `discharge_battery`. To NIE jest rozjazd intencji — guardy widzą
        # DISCHARGE i falownik rozładowuje. Zmienia się tylko to, że tryb jest
        # funkcją pary (akcja, `discharge_purpose`), a nie samej akcji.
        #
        # Powód jest zmierzony: `discharge_battery` ma nastawę STAŁĄ i przy poborze
        # domu większym niż nastawa dokupuje różnicę Z SIECI (2026-09-02 o 21:00 —
        # 0,828 kWh po 1,866 zł w 20 minut, przy baterii w 47 %). `sell_power`
        # pokrywa dom z baterii i eksportuje nastawę ponad to.
        oczekiwany = mappers_module.GOODWE_SELL_MODE
    elif slot.discharge_purpose == "self" and akcja_efektywna(slot) is Action.DISCHARGE:
        # Drugi udokumentowany wyjątek (2026-09-01): rozładowanie NA WŁASNE POTRZEBY
        # zostaje w trybie neutralnym, bo `auto` samo pokrywa dom z baterii i śledzi
        # rzeczywisty pobór — wymuszona nastawa dobierałaby brakującą część z SIECI
        # przy większym poborze i wypychała nadmiar do sieci przy mniejszym.
        #
        # W przeciwieństwie do wyjątku PV ten NIE jest „dokładniejszym tłumaczeniem
        # tej samej intencji": guardy widzą DISCHARGE, a falownik dostaje `auto`.
        # Rozjazd jest świadomy i idzie w STRONĘ BEZPIECZNĄ — guardy chronią intencję
        # ostrzejszą (I-1 zeruje rozładowanie i podnosi rezerwę) niż ta, którą
        # urządzenie faktycznie wykonuje. Kierunku celowo nie zmieniamy, żeby
        # przejście `charge_*` → `auto` dalej zużywało budżet I-8: ono JEST realnym
        # przełączeniem trybu.
        oczekiwany = mappers_module.GOODWE_MODE_MAP[Action.SELF_CONSUME]
    elif slot.charge_source == "pv" and akcja_efektywna(slot) is Action.CHARGE:
        # Ładowanie wyłącznie z nadwyżki PV zostaje w trybie neutralnym: `auto`
        # robi to natywnie, dopóki bateria nie jest pełna. Ten sam rodzaj wyjątku
        # co przy `discharge_purpose='self'` — guardy widzą CHARGE, falownik
        # dostaje `auto`, a rozjazd idzie w stronę bezpieczną (nie wymuszamy nic,
        # czego urządzenie samo by nie zrobiło).
        oczekiwany = mappers_module.GOODWE_MODE_MAP[Action.SELF_CONSUME]
    assert params["mode"] == oczekiwany


# ── Część 2: U-6 — rezerwa blokuje rozładowanie dla KAŻDEGO kształtu ─────────


@pytest.mark.asyncio
async def test_r1_regresja_discharge_sell_ponizej_rezerwy_zablokowany(fake_entry):
    """Regresja R-1: jednoznaczny `mode='discharge'` poniżej rezerwy — bez zmian."""
    hass = _hass(soc="10")
    fake_entry.options = dict(OPTIONS) | {"soc_reserve": 40.0}
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    wynik = await executor.async_set_schedule(
        _plan("r1", teraz, mode="discharge", discharge_purpose="sell",
              power_w=1476.0, soc_target=10.0)
    )

    zapisy = _zapisy(hass)
    assert wynik.forced_action is AkcjaHA.SELF_CONSUME
    assert zapisy.get("select.tryb") != "discharge_battery"
    assert "number.eco_power" not in zapisy
    assert zapisy.get("number.eco_soc") == 40.0


@pytest.mark.asyncio
async def test_u6_self_consume_z_discharge_purpose_ponizej_rezerwy_zablokowany(fake_entry):
    """SONDA U-6 (odtworzenie 1:1): kształt 41% slotów z mocą omijał I-1.

    Zmierzone PRZED naprawą: `select.tryb='discharge_battery'`, `number.charge_limit=14.8`,
    `forced_action=None`, brak noty I-1. Rezerwa backup nie zadziałała.
    """
    hass = _hass(soc="10")
    fake_entry.options = dict(OPTIONS) | {"soc_reserve": 40.0}
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    wynik = await executor.async_set_schedule(
        _plan("u6", teraz, mode="self_consume", discharge_purpose="self",
              power_w=1476.0, soc_target=10.0)
    )

    zapisy = _zapisy(hass)
    assert zapisy.get("select.tryb") != "discharge_battery", (
        "U-6: kierunek opisowy musi być widziany przez I-1 PRZED zapisem, nie po nim"
    )
    assert "number.eco_power" not in zapisy, (
        "moc rozładowania nie ma prawa dojść do falownika poniżej rezerwy backup"
    )
    assert zapisy.get("number.eco_soc") == 40.0
    assert wynik.forced_action is AkcjaHA.SELF_CONSUME
    assert any(n.invariant == "I-1" for n in wynik.notes), (
        "brak noty I-1 = brak śladu, że rezerwa w ogóle zadziałała"
    )


@pytest.mark.asyncio
async def test_u6_ladowanie_z_pv_ponizej_rezerwy_jest_DOZWOLONE(fake_entry):
    """Rezerwa broni WYŁĄCZNIE przed rozładowaniem — ładowanie poniżej niej to cel.

    `charge_source='pv'` nie dowozi nastawy mocy (świadoma decyzja z U-1, hipoteza H-1),
    ale nie wolno go zdusić jako „rozładowanie" ani przemapować przez `forced_action`.
    """
    hass = _hass(soc="10")
    fake_entry.options = dict(OPTIONS) | {"soc_reserve": 40.0}
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    wynik = await executor.async_set_schedule(
        _plan("u6-pv", teraz, mode="self_consume", charge_source="pv",
              power_w=1476.0, soc_target=10.0)
    )

    zapisy = _zapisy(hass)
    assert wynik.forced_action is None, "ładowania nie wolno wymuszać na self_consume"
    # Ładowanie nadwyżką zostaje w trybie neutralnym (`auto` robi to natywnie),
    # a brak nastawy mocy jest MOCNIEJSZĄ gwarancją niż `Xset=0`: nie ma czym
    # pomylić kierunku. Rezerwa i tak jedzie — I-1 nie blokuje ładowania.
    assert zapisy.get("select.tryb") == "auto"
    assert "number.charge_limit" not in zapisy
    assert zapisy.get("number.eco_soc") == 40.0


@pytest.mark.asyncio
async def test_u6_ladowanie_z_sieci_ponizej_rezerwy_dochodzi_z_moca(fake_entry):
    """Druga połowa tej samej reguły: kierunek CHARGE przechodzi guardy nietknięty."""
    hass = _hass(soc="10")
    fake_entry.options = dict(OPTIONS) | {"soc_reserve": 40.0}
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    wynik = await executor.async_set_schedule(
        _plan("u6-grid", teraz, mode="self_consume", charge_source="grid",
              power_w=1476.0, soc_target=10.0)
    )

    zapisy = _zapisy(hass)
    assert wynik.forced_action is None
    assert zapisy.get("select.tryb") == "charge_battery"
    # Etap 3: moc idzie w WATACH na `ems_power_limit`, a nie w procentach na
    # `eco_mode_power` — ta encja jest w integracji mletenay tylko do odczytu.
    assert zapisy.get("number.charge_limit") == pytest.approx(1476.0)


@pytest.mark.asyncio
async def test_r1_regresja_przemapowanie_dokladnie_raz(fake_entry):
    """Regresja R-1: przemapowanie po `forced_action` nie może się zapętlić.

    Slot wraca do mappera RAZ — z kierunkiem i mocą zdjętymi — więc na encję trybu
    idzie dokładnie jedna nastawa (`general`), a nie seria kolejnych prób.
    """
    hass = _hass(soc="10")
    fake_entry.options = dict(OPTIONS) | {"soc_reserve": 40.0}
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    wynik = await executor.async_set_schedule(
        _plan("r1-raz", teraz, mode="self_consume", discharge_purpose="self",
              power_w=1476.0, soc_target=10.0)
    )

    assert _tryby(hass) == ["auto"]
    assert wynik.status is StatusHA.PARTIAL, (
        "plan NIE został wykonany zgodnie z intencją — chmura musi to widzieć"
    )


# ── Część 3: U-7 — I-8 widzi kierunek opisowy ───────────────────────────────


@pytest.mark.asyncio
async def test_u7_naprzemienne_plany_opisowe_zuzywaja_budzet_i8(fake_entry, monkeypatch):
    """SONDA U-7 (odtworzenie): 12 planów co 120 s, naprzemiennie rozładowanie/ładowanie.

    Zmierzone PRZED naprawą: 12 przełączeń trybu falownika, `_direction._current`
    bez zmian — anty-oscylacja nie działała wcale, bo `act` liczyło się ze
    `slot.action` (zawsze `SELF_CONSUME`).

    Budżet I-8 to 4 zmiany na godzinę, a pierwsze ustawienie kierunku zmianą nie jest
    (R-13b) — więc na falownik ma pójść najwyżej 5 nastaw trybu, a nadmiarowe plany
    mają być zdławione.
    """
    zegar = ZegarSterowany()
    monkeypatch.setattr(executor_module, "time", zegar)
    hass = _hass(soc="60")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    statusy = []
    for i in range(12):
        if i % 2 == 0:
            pola = {"discharge_purpose": "self"}
        else:
            pola = {"charge_source": "grid"}
        wynik = await executor.async_set_schedule(
            _plan(f"u7-{i}", teraz, power_w=1476.0, soc_target=50.0, **pola)
        )
        statusy.append(wynik.status)
        zegar.przesun(120.0)

    tryby = _tryby(hass)
    assert len(tryby) <= 5, (
        f"I-8 musi zdławić oscylację kierunku: poszło {len(tryby)} nastaw trybu {tryby}"
    )
    assert StatusHA.THROTTLED in statusy, "po wyczerpaniu budżetu plany mają być dławione"
    assert executor._direction._current is AkcjaHA.DISCHARGE, (
        "DirectionLimiter musi ZNAĆ kierunek efektywny, inaczej niczego nie ogranicza"
    )


@pytest.mark.asyncio
async def test_u7_diagnoza_widzi_ten_sam_kierunek_co_realny_przebieg(fake_entry):
    """R-7: suchy przebieg musi odwzorować decyzję realnego — także dla kierunku opisowego."""
    hass = _hass(soc="10")
    fake_entry.options = dict(OPTIONS) | {"soc_reserve": 40.0}
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    await executor.async_set_schedule(
        _plan("u7-diag", teraz, mode="self_consume", discharge_purpose="self",
              power_w=1476.0, soc_target=10.0)
    )
    raport = await executor.async_diagnose()

    assert raport["guards"]["forced_action"] == AkcjaHA.SELF_CONSUME.value, (
        "diagnoza pokazywałaby zapis, którego I-1 by nie przepuściło"
    )
    assert raport["slot"]["action"] == "self_consume", "dosłowne pole planu"
    assert raport["slot"]["action_efektywna"] == "discharge", (
        "bez tego pola nie da się na żywym sprzęcie odróżnić tego, co plan powiedział, "
        "od tego, co urządzenie z niego zrozumiało"
    )
