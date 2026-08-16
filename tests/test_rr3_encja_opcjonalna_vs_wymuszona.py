"""RR-3 — regresja z rundy 2 (`docs/analysis/2026-08-16-faza-a-regresje.md`).

RR-3 (WYSOKA): naprawa R-3 (runda 1) kazała `applier.apply_params` zgłaszać BŁĄD dla
KAŻDEGO parametru bez zmapowanej encji — bez rozróżnienia. Ale `mappers.slot_to_params`
emituje `export_limit_enabled` ZAWSZE, w obu gałęziach (`entity_export_limit_switch` jest
`vol.Optional` w `config_flow.py`). Skutek: użytkownik, który świadomie nie mapuje
ogranicznika eksportu, dostaje trwały `ERROR` co przebieg pętli (60 s) — legalna
konfiguracja staje się wieczną awarią.

Naprawa: `GuardResult.forced_params` (guards.py) odróżnia parametr WYMUSZONY przez guard
bezpieczeństwa (I-4 blokujące eksport przy cenie <= 0, I-1 podnoszące eco_soc do rezerwy,
"mode" w przebiegu przemapowanym po `forced_action`) od parametru z normalnego mapowania
planu. `applier.apply_params` zgłasza błąd tylko dla pierwszej grupy — dla drugiej tylko
widoczną notę, bez wpływu na status.

KRYTYCZNE (z instrukcji zadania): naprawa RR-3 nie wolno jej przywrócić pierwotnego R-3 —
każdy test w tym pliku pilnuje OBU stron: „opcjonalna encja niezmapowana" (nie błąd) ORAZ
„encja WYMUSZONA przez guard bezpieczeństwa niezmapowana" (błąd, R-3 musi zostać zamknięte).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter.applier import apply_params
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

OPTIONS_BEZ_EXPORT_SWITCH = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    # entity_export_limit_switch świadomie NIE zmapowany (opcjonalny w config_flow.py)
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


def _schedule_charge(price: float = 0.30) -> dict:
    """Slot `charge` z dodatnią ceną — I-4 NIE wchodzi, I-1 NIE wchodzi (SoC wysoki)."""
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "rr3",
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


# ── STRONA REGRESJI (RR-3): opcjonalna encja niezmapowana NIE MOŻE dać ERROR ─


@pytest.mark.asyncio
async def test_rr3_brak_export_switch_slot_charge_trzy_tiki_zaden_nie_jest_error(fake_entry):
    """Sonda P1 z dokumentu regresji, end-to-end (Schedule -> executor -> applier).

    Bez naprawy: tick 1 -> PARTIAL (1 "error" z niezmapowanego export_limit_enabled),
    tick 2 -> I-6 filtruje `mode`/`eco_soc` (bez zmiany), do applier trafia WYŁĄCZNIE
    `export_limit_enabled` (nigdy nie wchodzi do `executed`, nigdy nie jest committowany
    do throttle'a, więc jest writable KAŻDY przebieg) -> `errors` niepuste, `executed=[]`
    -> `ERROR`. Tick 3, 4, ... -> `ERROR` w nieskończoność.
    """
    hass = _hass_swiezy()
    fake_entry.options = dict(OPTIONS_BEZ_EXPORT_SWITCH)
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_charge())

    statusy = []
    for _ in range(3):
        result = await executor._async_execute_now()
        statusy.append(result.status)

    assert all(s is not Status.ERROR for s in statusy), (
        f"żaden z 3 kolejnych ticków nie może być ERROR — opcjonalna encja "
        f"(export_limit_switch) niezmapowana nie jest błędem: statusy={statusy}"
    )
    # Nie znika bez śladu: musi zostać widoczna nota o pominiętym zapisie.
    ostatni = await executor._async_execute_now()
    assert any(
        n.invariant == "RR-3" and "export_limit_enabled" in n.message
        for n in ostatni.notes
    ), "brak zmapowanej encji musi zostawić widoczną notę, nie zniknąć bez śladu"


@pytest.mark.asyncio
async def test_rr3_applier_normalny_parametr_niezmapowany_daje_note_nie_error():
    """Warstwa `applier` w izolacji: `forced_params=set()` (guard niczego nie
    wymusił) — brak mapowania idzie do `notes`, `errors` zostaje puste."""
    hass = FakeHass()
    options: dict = {}  # entity_eco_mode_soc świadomie NIE zmapowane

    executed, errors, notes = await apply_params(
        hass, options, {"eco_soc": 40.0}, forced_params=set()
    )

    assert executed == []
    assert errors == [], "parametr POZA forced_params nie może trafić do errors"
    assert notes and notes[0]["entity"] == "eco_soc"


# ── DRUGA STRONA (R-3, pierwotny błąd): guard-wymuszony parametr MUSI dać ERROR ──


@pytest.mark.asyncio
async def test_rr3_i4_eksport_wymuszony_niezmapowany_nadal_daje_error(fake_entry):
    """Zachowanie R-3 (pierwotna naprawa) musi przetrwać: cena <= 0, I-4 wymusza
    `export_limit=0` + `export_limit_enabled=True`. Encje eksportu niezmapowane ->
    ERROR, nie cicha nota. To jest DOKŁADNIE ten wektor, przed którym broni R-3 —
    naprawa RR-3 (rozróżnienie forced/normal) nie wolno jej go rozmiękczyć.

    Scenariusz izolowany (jak w `test_r3_r9_write_errors.py`): TYLKO `entity_soc`
    zmapowane, komenda pusta (`{}`) — więc jedynymi parametrami w grze są te, które
    I-4 samo wstawia. Nic innego nie może "zamaskować" braku ochrony sukcesem
    równoległego zapisu.
    """
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    fake_entry.options = {"entity_soc": "sensor.soc"}
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply(
        {}, price_pln_kwh=-0.05, action=Action.SELF_CONSUME, source="test"
    )

    assert result.executed == [], "encje niezmapowane — nic nie mogło pójść do falownika"
    assert result.status is Status.ERROR, (
        "I-4 wymusiło zabezpieczenie eksportu, encja niezmapowana -> to musi być "
        "głośny ERROR, nie PARTIAL/SUCCESS wyglądający jak zadziałane zabezpieczenie"
    )
    assert any(e["entity"] == "export_limit_enabled" for e in result.errors), (
        "R-9: błąd musi być widoczny w GuardResult.errors, nie tylko w logu"
    )


@pytest.mark.asyncio
async def test_rr3_i4_eksport_wymuszony_obok_normalnych_parametrow_daje_partial_z_errorem(
    fake_entry,
):
    """Ten sam wektor R-3, ale w REALNYM przebiegu obok normalnych parametrów planu
    (`mode`, `eco_soc`), które SĄ zmapowane i się wykonują. Status wychodzi `PARTIAL`
    (bo coś się jednak zapisało) — kluczowe jest, że błąd eksportu i tak trafia do
    `errors`, więc zabezpieczenie widocznie NIE zadziałało, mimo że tick jako całość
    nie jest czystym sukcesem."""
    hass = _hass_swiezy()
    fake_entry.options = dict(OPTIONS_BEZ_EXPORT_SWITCH)
    executor = VolterExecutor(hass, fake_entry)
    executor._schedule = Schedule.from_dict(_schedule_charge(price=-0.05))

    result = await executor._async_execute_now()

    assert "export_limit_enabled" not in result.executed, (
        "export_limit_enabled nie mogło pójść do falownika — encja niezmapowana"
    )
    assert any(e["entity"] == "export_limit_enabled" for e in result.errors), (
        "brak zmapowanej encji dla parametru WYMUSZONEGO przez I-4 musi zostać "
        "w errors, nawet gdy reszta zapisu (mode/eco_soc) się powiodła"
    )
    assert result.status is not Status.SUCCESS, (
        "nie wolno pokazać czystego SUCCESS — zabezpieczenie I-4 nie dotarło do falownika"
    )


@pytest.mark.asyncio
async def test_rr3_applier_wymuszony_parametr_niezmapowany_daje_error_nie_note():
    """Symetryczny test do powyższego, w izolacji warstwy `applier`."""
    hass = FakeHass()
    options: dict = {}  # entity_eco_mode_soc świadomie NIE zmapowane

    executed, errors, notes = await apply_params(
        hass, options, {"eco_soc": 40.0}, forced_params={"eco_soc"}
    )

    assert executed == []
    assert notes == [], "parametr W forced_params nie może trafić do notes"
    assert errors and errors[0]["entity"] == "eco_soc"


def test_rr3_i1_eco_soc_wymuszony_oznaczony_w_forced_params():
    """`apply_guards` musi oznaczyć `eco_soc` jako wymuszony, gdy I-1 sam go
    podnosi do rezerwy — inaczej `applier` (przez `forced_params`) potraktowałby
    zniknięcie rezerwy backup jak zwykłą, nieszkodliwą notę."""
    ctx = GuardContext(
        state=DeviceState(soc=15.0, age_s=1.0),
        config=UserConfig(soc_reserve=20.0, mode="autarky"),
        action=Action.DISCHARGE,
    )
    result = apply_guards({"discharge_limit": 30.0}, ctx)

    assert result.status is Status.PARTIAL
    assert result.params.get("eco_soc") == 20.0
    assert "eco_soc" in result.forced_params, (
        "I-1 samo wstawiło eco_soc=rezerwa — to jest wymuszenie bezpieczeństwa, "
        "musi trafić do forced_params"
    )


def test_rr3_i4_eksport_oznaczony_w_forced_params_nawet_bez_zmiany_wartosci():
    """I-4 musi oznaczyć export_limit/export_limit_enabled jako wymuszone NAWET
    gdy plan sam już trafił w bezpieczną wartość (`changed=False`) — ochrona nadal
    wymaga, żeby ta wartość faktycznie dotarła do falownika."""
    ctx = GuardContext(
        state=DeviceState(soc=55.0, age_s=1.0),
        config=UserConfig(soc_reserve=20.0, mode="autarky"),
        action=Action.SELF_CONSUME,
        price_pln_kwh=-0.01,
    )
    # Plan już sam ustawił bezpieczną wartość -> `changed` w I-4 wychodzi False.
    result = apply_guards({"export_limit": 0.0, "export_limit_enabled": True}, ctx)

    assert result.status is Status.SUCCESS
    assert {"export_limit", "export_limit_enabled"} <= result.forced_params


def test_rr3_normalne_parametry_planu_nie_sa_forced_gdy_guardy_milcza():
    """Kontrola: gdy żaden guard bezpieczeństwa nie interweniuje (SoC wysoki, cena
    dodatnia), `forced_params` musi zostać puste — inaczej wszystko byłoby
    "wymuszone" i naprawa RR-3 nie miałaby efektu."""
    ctx = GuardContext(
        state=DeviceState(soc=80.0, age_s=1.0),
        config=UserConfig(soc_reserve=20.0, mode="autarky"),
        action=Action.CHARGE,
        price_pln_kwh=0.30,
    )
    result = apply_guards({"mode": "eco_charge", "eco_soc": 80.0}, ctx)

    assert result.status is Status.SUCCESS
    assert result.forced_params == set()
