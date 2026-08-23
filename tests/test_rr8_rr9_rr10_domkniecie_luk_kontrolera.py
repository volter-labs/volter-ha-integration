"""RR-8, RR-9, RR-10 — trzy luki znalezione przez DRUGIEGO kontrolera w naprawach
rundy 2 (S-4/S-5), zlecone do domknięcia w tej sesji
(`docs/analysis/2026-08-16-faza-a-audyt-swiezym-okiem.md`, sekcja "Ustalenia
drugiego kontrolera").

Każdy test w tym pliku pilnuje OBU stron: że sonda z instrukcji zadania faktycznie
FAILuje na kodzie sprzed naprawy, i że naprawa NIE cofa ochrony, którą naprawiła
runda 2 (RR-3, R-3, RR-5, R-12).

RR-8 (WYSOKA): `apply_guards` dodawał `eco_soc` do `forced_params` TYLKO gdy
`eco_soc_raised` (guard musiał podnieść wartość). Gdy plan SAM już niósł
`eco_soc >= soc_reserve`, ochrona I-1 znikała po cichu przy niezmapowanej
encji — dokładnie ten sam wzorzec luki, który dla I-4 naprawiono NIEZALEŻNIE
od flagi `changed` (guards.py, blok I-4).

RR-9 (WYSOKA): przebieg, który zażądał zapisów (`writable` niepuste) i nie
zapisał ŻADNEGO (bo wszystkie parametry trafiły w gałąź "encja niezmapowana,
normalny parametr planu" -> `notes`, nie `errors`), zostawał `status=success`
— pierwotne sformułowanie R-3 ("niezmapowana encja to cichy no-op raportowany
jako sukces") przywrócone dla całej klasy parametrów nie-wymuszonych.

RR-10 (ŚREDNIA): WARNING "Guardy wymusiły akcję… przemapowuję slot"
(`executor.py`, gałąź `forced_action`) stał POZA anty-spamem `_remember` —
logowany bezwarunkowo na KAŻDYM ticku (zmierzone 60 WARNING/h w stanie
ustabilizowanym). Dodatkowo klucz anty-spamu notatek opierał się na DOSŁOWNEJ
treści komunikatu — dwie najczęstsze ścieżki DEGRADED (I-9: wiek odczytu,
skok SoC) wstawiają tam wartość zmienną w czasie, więc klucz nigdy się nie
powtarzał i anty-spam nigdy się nie stabilizował.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import (
    Action,
    DeviceState,
    GuardContext,
    Status,
    UserConfig,
    apply_guards,
)
from custom_components.volter.schedule import Schedule
from tests.conftest import FakeHass

# ═════════════════════════════════════════════════════════════════════════
# RR-8 — I-1 musi oznaczyć eco_soc jako forced_params NIEZALEŻNIE od
# eco_soc_raised (plan mógł już sam nieść wartość rezerwy)
# ═════════════════════════════════════════════════════════════════════════


def test_rr8_i1_eco_soc_forced_nawet_gdy_plan_juz_niesie_wartosc_rezerwy():
    """Sonda z instrukcji zadania: soc_reserve=40, SoC=30, plan niesie
    eco_soc=40.0 (RÓWNE rezerwie — nie trzeba go "podnosić", `eco_soc_raised`
    wychodzi False). Bez naprawy `forced_params` zostaje puste, mimo że I-1
    realnie broni tej wartości — ochrona znika po cichu, gdy encja eco_soc
    nie jest zmapowana."""
    ctx = GuardContext(
        state=DeviceState(soc=30.0, age_s=1.0),
        config=UserConfig(soc_reserve=40.0, mode="autarky"),
        action=Action.DISCHARGE,
    )
    result = apply_guards({"eco_soc": 40.0}, ctx)

    assert result.forced_action is Action.SELF_CONSUME
    assert result.params.get("eco_soc") == 40.0
    assert "eco_soc" in result.forced_params, (
        "RR-8: eco_soc już równy rezerwie (nie 'podniesiony' przez guard) to WCIĄŻ "
        "ochrona I-1 — musi trafić do forced_params, tak jak I-4 robi to "
        "niezależnie od flagi `changed`, inaczej znika po cichu przy braku "
        "zmapowanej encji"
    )


def test_rr8_ochrona_normalny_plan_bez_ingerencji_i1_nadal_nie_jest_forced():
    """Strona ochrony: gdy SoC jest ponad rezerwą (I-1 w ogóle nie wchodzi),
    `eco_soc` nadal NIE MOŻE trafić do `forced_params` — inaczej naprawa RR-8
    oznaczałaby WSZYSTKO jako wymuszone i naprawa RR-3 (nota, nie error, dla
    normalnych parametrów planu) przestałaby cokolwiek znaczyć."""
    ctx = GuardContext(
        state=DeviceState(soc=80.0, age_s=1.0),
        config=UserConfig(soc_reserve=40.0, mode="autarky"),
        action=Action.CHARGE,
    )
    result = apply_guards({"mode": "charge_battery", "eco_soc": 80.0}, ctx)

    assert result.status is Status.SUCCESS
    assert result.forced_params == set()


OPTIONS_RR8_BEZ_ECO_SOC = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "soc_reserve": 40.0,
    # entity_eco_mode_soc świadomie NIE zmapowane
}


def _hass_rr8(soc: str = "30") -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", soc)
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb", "auto", {"options": ["auto", "charge_pv", "discharge_pv", "import_ac", "export_ac", "conserve", "off_grid", "battery_standby", "buy_power", "sell_power", "charge_battery", "discharge_battery"]}
    )
    return hass


def _schedule_discharge(soc_target: float) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "rr8",
        "slots": [
            {
                "from": (now - timedelta(minutes=10)).isoformat(),
                "to": (now + timedelta(minutes=50)).isoformat(),
                "mode": "discharge",
                "power_w": 2000,
                "soc_target": soc_target,
                "export_allowed": True,
            }
        ],
        "fallback": {"mode": "self_consume", "soc_reserve": 40.0},
    }


@pytest.mark.asyncio
async def test_rr8_end_to_end_eco_soc_niezmapowany_daje_error_nie_note(fake_entry):
    """Odtworzenie end-to-end (Schedule -> executor -> applier): soc_reserve=40,
    SoC=30, slot `discharge` z `soc_target=40` (RÓWNY rezerwie), encja eco_soc
    NIEZMAPOWANA. Bez naprawy: `status=partial`, `errors=[]`, nota RR-3 zamiast
    błędu — rezerwa backup nie dociera do falownika i nikt się o tym nie
    dowiaduje."""
    hass = _hass_rr8(soc="30")
    fake_entry.options = dict(OPTIONS_RR8_BEZ_ECO_SOC)
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_discharge(soc_target=40.0))

    result = await executor._async_execute_now()

    # `mode` JEST zmapowane (entity_ems_mode) i się zapisuje — status całościowo
    # nie może więc zostać czystym ERROR (patrz analogiczny wzorzec dla I-4:
    # `test_rr3_i4_eksport_wymuszony_obok_normalnych_parametrow_daje_partial_z_errorem`).
    # Sedno naprawy RR-8 to to, że `eco_soc` w ogóle TRAFIA do `errors` — bez niej
    # znikał bez śladu jako zwykła nota RR-3, a rezerwa backup nie docierała do
    # falownika bez żadnego sygnału.
    assert result.status is not Status.SUCCESS, (
        "RR-8: eco_soc niezmapowany, ale CHRONIONY przez I-1 — nie wolno pokazać "
        "czystego SUCCESS, zabezpieczenie nie dotarło do falownika"
    )
    assert any(e["entity"] == "eco_soc" for e in result.errors), (
        "RR-8: błąd musi być widoczny w GuardResult.errors, nie tylko zniknąć "
        "w notes jako zwykła opcjonalna encja (RR-3)"
    )


# ═════════════════════════════════════════════════════════════════════════
# RR-9 — zero zapisów + zero błędów nie może dać SUCCESS
# ═════════════════════════════════════════════════════════════════════════


def _schedule_charge_normalny(price: float = 0.30) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "rr9",
        "slots": [
            {
                "from": (now - timedelta(minutes=10)).isoformat(),
                "to": (now + timedelta(minutes=50)).isoformat(),
                "mode": "charge",
                "power_w": 3000,
                "soc_target": 80.0,
                "price_pln_kwh": price,
                "export_allowed": True,
            }
        ],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    }


@pytest.mark.asyncio
async def test_rr9_zadanych_4_zapisow_zero_wykonanych_nie_moze_byc_success(fake_entry):
    """Sonda z instrukcji zadania: opcje niosą TYLKO monitoring (+ decoy
    discharge limit, którego mapper i tak nigdy nie emituje) — żadna z
    czterech encji sterujących (mode/eco_soc/charge_limit/export_limit_switch)
    nie jest zmapowana. Slot `charge`, SoC wysoki (I-1 nie wchodzi), cena
    dodatnia (I-4 nie wchodzi) — żaden guard bezpieczeństwa nic nie wymusza.
    Bez naprawy: `status=success`, `executed=[]`, `errors=[]`, 4 noty — do
    falownika NIC nie idzie, a chmura widzi sukces."""
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    fake_entry.options = {
        "entity_soc": "sensor.soc",
        "entity_pv_power": "sensor.pv",
        "entity_grid_power": "sensor.grid",
        "entity_discharge_limit": "number.discharge_limit",
        # entity_ems_mode, entity_eco_mode_soc, entity_charge_limit,
        # entity_soc_upper, entity_export_limit_switch świadomie NIE zmapowane.
        # Etap 3: `entity_charge_limit` przestał być decoy — mapper emituje na nią
        # nastawę mocy, więc zmapowanie jej zepsułoby premisę tego testu.
    }
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_charge_normalny())

    result = await executor._async_execute_now()

    assert result.executed == [], "warunek testu: nic nie mogło pójść do falownika"
    assert result.errors == [], "warunek testu: żaden z parametrów nie jest wymuszony"
    assert len(result.notes) >= 4, (
        "warunek testu: wszystkie 4 parametry (mode/eco_soc/eco_power/"
        "export_limit_enabled) muszą zostawić widoczną notę o braku mapowania"
    )
    assert result.status is not Status.SUCCESS, (
        "RR-9: zażądaliśmy zapisu 4 parametrów i ANI JEDEN nie poszedł do "
        "falownika — SUCCESS kłamie. Chmura rozliczy dobę zakładając, że plan "
        "się wykonał"
    )
    assert result.status is not Status.ERROR, (
        "RR-9: nie wolno wrócić do trwałego ERROR z pierwotnego R-3 — to była "
        "regresja, którą RR-3 świadomie naprawiła dla parametrów bez udziału "
        "guarda bezpieczeństwa (opcjonalne, świadomie niezmapowane encje)"
    )


@pytest.mark.asyncio
async def test_rr9_ochrona_gdy_cos_sie_zapisalo_status_nie_jest_wymuszany(fake_entry):
    """Strona ochrony: gdy CHOĆ JEDEN parametr faktycznie poszedł do falownika,
    naprawa RR-9 nie może nic zmieniać — to już nie jest przypadek "zero
    zapisów"."""
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb", "auto", {"options": ["auto", "charge_pv", "discharge_pv", "import_ac", "export_ac", "conserve", "off_grid", "battery_standby", "buy_power", "sell_power", "charge_battery", "discharge_battery"]}
    )
    hass.states.set("number.eco_soc", "20")
    fake_entry.options = {
        "entity_soc": "sensor.soc",
        "entity_pv_power": "sensor.pv",
        "entity_grid_power": "sensor.grid",
        "entity_ems_mode": "select.tryb",
        "entity_eco_mode_soc": "number.eco_soc",
        # entity_eco_mode_power, entity_export_limit_switch NIE zmapowane
    }
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_charge_normalny())

    result = await executor._async_execute_now()

    assert result.executed, "warunek testu: mode/eco_soc musiały pójść do falownika"
    assert result.status is Status.SUCCESS


# ═════════════════════════════════════════════════════════════════════════
# RR-10 — anty-spam oparty na tożsamości zdarzenia, nie na treści komunikatu
# ═════════════════════════════════════════════════════════════════════════


OPTIONS_RR10 = {
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


def _hass_rr10(soc: str = "30") -> FakeHass:
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


def _schedule_rr10(soc_target: float = 50.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "rr10",
        "slots": [
            {
                "from": (now - timedelta(minutes=10)).isoformat(),
                "to": (now + timedelta(minutes=50)).isoformat(),
                "mode": "discharge",
                "power_w": 5000,
                "soc_target": soc_target,
                "export_allowed": True,
            }
        ],
        "fallback": {"mode": "self_consume", "soc_reserve": 40.0},
    }


@pytest.mark.asyncio
async def test_rr10_warning_przemapowania_nie_spamuje_po_ustabilizowaniu(fake_entry, caplog):
    """`soc_reserve=40`, `SoC=30`, slot `discharge` — I-1 wymusza `self_consume`
    na KAŻDYM ticku, stan ustabilizowany (nic się nie zmienia). Bez naprawy:
    `_LOGGER.warning("Guardy wymusiły akcję…")` stoi POZA anty-spamem
    `_remember` i loguje WARNING bezwarunkowo na każdym ticku (zmierzone
    60 WARNING/h w stanie ustabilizowanym)."""
    hass = _hass_rr10(soc="30")
    fake_entry.options = dict(OPTIONS_RR10)
    executor = VolterExecutor(hass, fake_entry)

    def przemapowania() -> list[logging.LogRecord]:
        return [r for r in caplog.records if "Guardy wymusi" in r.message]

    with caplog.at_level(logging.DEBUG):
        await executor.async_set_schedule(_schedule_rr10())  # tick 1
        await executor._async_execute_now()  # tick 2
        po_dwoch = len(przemapowania())
        assert po_dwoch >= 1, "warunek testu: pierwszy tick musi coś zalogować"

        await executor._async_execute_now()  # tick 3
        await executor._async_execute_now()  # tick 4

        wpisy = przemapowania()
        assert len(wpisy) == 4, (
            "nota musi pojawić się na KAŻDYM ticku (choćby na DEBUG) — to nie jest "
            "zniknięcie logu, tylko zmiana poziomu przy powtórce"
        )
        poziomy_warning = sum(1 for r in wpisy if r.levelno == logging.WARNING)
        assert poziomy_warning <= 1, (
            "RR-10: 'Guardy wymusiły akcję… przemapowuję slot' logowało WARNING "
            "bezwarunkowo na KAŻDYM ticku (60 WARNING/h w stanie ustabilizowanym) "
            "— musi podlegać TEJ SAMEJ regule anty-spamu co pozostałe notatki "
            "(pierwsze wystąpienie widoczne, powtórki na DEBUG)"
        )


def test_rr10_i9_stale_reading_notatka_stabilizuje_mimo_rosnacego_wieku(fake_entry, caplog):
    """`I-9` „odczyt starszy niż…" niesie `wiek` w treści komunikatu — ten sam
    powód DEGRADED (telemetria wciąż martwa) rośnie w treści na każdym ticku,
    bo czas płynie. Bez naprawy klucz anty-spamu oparty na dosłownej treści
    NIGDY się nie powtarza, więc `_remember` loguje INFO w kółko."""
    executor = VolterExecutor(FakeHass(), fake_entry)

    ctx1 = GuardContext(state=DeviceState(soc=55.0, age_s=305.0), config=UserConfig())
    ctx2 = GuardContext(state=DeviceState(soc=55.0, age_s=610.0), config=UserConfig())

    r1 = apply_guards({}, ctx1)
    r2 = apply_guards({}, ctx2)

    assert r1.status is Status.DEGRADED and r2.status is Status.DEGRADED
    msg1 = next(n.message for n in r1.notes if n.invariant == "I-9")
    msg2 = next(n.message for n in r2.notes if n.invariant == "I-9")
    assert msg1 != msg2, "warunek testu: 'wiek' w treści komunikatu MUSI się różnić"

    with caplog.at_level(logging.DEBUG):
        executor._remember("test", r1, [], [], Action.SELF_CONSUME)
        executor._remember("test", r2, [], [], Action.SELF_CONSUME)

    i9 = [r for r in caplog.records if "guard I-9" in r.message]
    assert len(i9) == 2
    assert [r.levelno for r in i9] == [logging.INFO, logging.DEBUG], (
        "RR-10: ta sama PRZYCZYNA DEGRADED (I-9 'odczyt starszy niż') musi "
        "stabilizować się do DEBUG mimo że WIEK w treści komunikatu rośnie co "
        "tick — klucz anty-spamu ma się opierać na TOŻSAMOŚCI zdarzenia "
        "(inwariant + parametr), nie na dosłownej treści"
    )


def test_rr10_ochrona_rozne_przyczyny_degraded_nadal_logowane_osobno(fake_entry, caplog):
    """Strona ochrony (powielona z `test_r12_reszta_ochrona_zmiana_przyczyny_
    znowu_loguje_info`, żeby ten plik sam w sobie dowodził, że RR-10 nie cofa
    R-12): DWIE RÓŻNE przyczyny DEGRADED (SoC poza fizyką vs brak odczytu) mają
    RÓŻNY `key`, więc obie muszą zalogować INFO, mimo że RR-10 wprowadza
    identity-key."""
    executor = VolterExecutor(FakeHass(), fake_entry)

    ctx_niefizyczny = GuardContext(state=DeviceState(soc=150.0, age_s=1.0), config=UserConfig())
    ctx_brak = GuardContext(state=DeviceState(soc=None, age_s=1.0), config=UserConfig())

    r1 = apply_guards({}, ctx_niefizyczny)
    r2 = apply_guards({}, ctx_brak)

    with caplog.at_level(logging.DEBUG):
        executor._remember("test", r1, [], [], Action.SELF_CONSUME)
        executor._remember("test", r2, [], [], Action.SELF_CONSUME)

    i9_info = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "guard I-9" in r.message
    ]
    assert len(i9_info) == 2, (
        "różne przyczyny DEGRADED (różny `key`) muszą się logować osobno na INFO"
    )
