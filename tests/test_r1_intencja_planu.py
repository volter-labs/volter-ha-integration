"""R-1 — guardy muszą widzieć intencję planu, nie tylko surowe klucze parametrów.

Ustalenie R-1 z przeglądu adwersaryjnego Fazy A (`2026-08-16-faza-a-review.md`):
`apply_guards` rozpoznawało zamiar ładowania/rozładowania po obecności kluczy
`charge_limit` / `discharge_limit`, a `slot_to_params` tych kluczy nigdy nie emituje —
intencja slotu siedzi w `mode`. Skutek: I-1 (rezerwa SoC) i I-2 (sprzeczna intencja)
były martwe na całej ścieżce harmonogramu, czyli tam, gdzie działa Faza D.

Specyfikacja inwariantów: `Volter-BOX/03-produkt/guardy-i-inwarianty.md` (I-1, I-2).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter import executor as executor_module
from custom_components.volter.executor import VolterExecutor
# UWAGA: conftest ładuje `guards` DWA razy — raz jako część komponentu, raz jako czysty
# `volter_pure`. To dwie różne klasy enuma, więc do testów ścieżki executora bierzemy
# `Action` z komponentu, a do testów czystych guardów z `volter_pure`.
from custom_components.volter.guards import Action as HaAction, Status
from tests.conftest import FakeHass

from volter_pure.guards import (
    Action,
    DeviceState,
    GuardContext,
    InverterLimits,
    Status as PureStatus,
    UserConfig,
    apply_guards,
)

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_eco_mode_power": "number.eco_power",
    "entity_discharge_limit": "number.discharge_limit",
    # Scenariusz z ustalenia: rezerwa backup 40%, tryb autarky.
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


def _schedule(mode: str, *, soc_target: float, power_w: float = 5000.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "r1",
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


def _tryby(hass: FakeHass) -> list[str]:
    """Opcje, które faktycznie poszły na encję select trybu pracy."""
    return [
        data.get("option")
        for domain, service, data in hass.services.calls
        if (domain, service) == ("select", "select_option")
    ]


# ── Ścieżka harmonogramu ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_r1_slot_rozladowania_ponizej_rezerwy_nie_trafia_do_falownika(fake_entry):
    """Scenariusz wprost z ustalenia R-1.

    `soc_reserve=40`, `user_mode=autarky`, `SoC=30%`, slot
    `{mode: discharge, soc_target: 50, power_w: 5000}`. Przed naprawą kończyło się
    to `select.select_option(select.tryb, 'eco_discharge')` ze statusem `success`
    i zerem not I-1 — rezerwa backup użytkownika była fikcją.
    """
    hass = _hass(soc="30")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_set_schedule(_schedule("discharge", soc_target=50.0))

    assert "eco_discharge" not in _tryby(hass), (
        "I-1: przy SoC 30% i rezerwie 40% tryb rozładowania NIE może pójść do falownika"
    )
    assert any(n.invariant == "I-1" for n in result.notes), "brak noty I-1 w wyniku"
    assert result.status is Status.PARTIAL
    # Falownik nie może zostać w trybie z poprzedniego slotu — musi dostać tryb bezpieczny.
    assert _tryby(hass) == ["general"]


@pytest.mark.asyncio
async def test_r1_slot_ladowania_ponizej_rezerwy_jest_dozwolony(fake_entry):
    """Rezerwa broni przed rozładowaniem, nie przed ładowaniem (I-1)."""
    hass = _hass(soc="30")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_set_schedule(_schedule("charge", soc_target=80.0))

    assert _tryby(hass) == ["eco_charge"], "ładowanie poniżej rezerwy musi być dozwolone"
    assert result.status is Status.SUCCESS
    assert result.params["eco_soc"] == 80.0


@pytest.mark.asyncio
async def test_r1_przemapowanie_wykonuje_sie_dokladnie_raz(fake_entry, monkeypatch):
    """Wymuszenie trybu bezpiecznego nie może wpaść w pętlę mapper → guardy → mapper."""
    hass = _hass(soc="30")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    oryginal = executor_module.slot_to_params
    wywolania: list[str] = []

    def _liczacy(slot, **kwargs):
        wywolania.append(slot.action.value)
        return oryginal(slot, **kwargs)

    monkeypatch.setattr(executor_module, "slot_to_params", _liczacy)

    await executor.async_set_schedule(_schedule("discharge", soc_target=50.0))

    assert wywolania == ["discharge", "self_consume"], (
        f"dokładnie jedno przemapowanie, było: {wywolania}"
    )
    assert len(_tryby(hass)) == 1, "tryb zapisany raz, bez pętli zapisów"


# ── Stary kontrakt SET_WORK_MODE (regresja T-1) ──────────────────────────────


@pytest.mark.asyncio
async def test_r1_stary_kontrakt_set_work_mode_dziala_jak_dotad(fake_entry):
    """T-1 ze specyfikacji: surowe parametry bez intencji planu — heurystyka po kluczach.

    `SoC=18%`, `R=20%`, komenda rozładowania → rozładowanie usunięte,
    `eco_soc` podniesiony do rezerwy i FAKTYCZNIE zapisany, status `partial`, powód I-1.
    """
    hass = _hass(soc="18")
    fake_entry.options = dict(OPTIONS)
    fake_entry.options["soc_reserve"] = 20.0
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply({"discharge_limit": 30.0}, source="cloud")

    assert result.status is Status.PARTIAL
    assert "discharge_limit" not in result.params
    assert result.params["eco_soc"] == 20.0
    assert any(n.invariant == "I-1" for n in result.notes)
    assert "eco_soc" in result.executed, "stary kontrakt musi dalej zapisywać eco_soc"


@pytest.mark.asyncio
async def test_r1_komenda_z_chmury_nie_wpycha_trybu_rozladowania_ponizej_rezerwy(fake_entry):
    """Ta sama luka na ścieżce SET_WORK_MODE: bez slotu nie ma czego przemapować,
    ale tryb rozładowania i tak nie może pójść do falownika."""
    hass = _hass(soc="30")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    result = await executor.async_apply(
        {"mode": "eco_discharge", "eco_soc": 10.0},
        action=HaAction.DISCHARGE,
        source="cloud",
    )

    assert _tryby(hass) == []
    assert result.status is Status.PARTIAL
    assert any(n.invariant == "I-1" for n in result.notes)


def test_r1_stary_kontrakt_heurystyka_po_kluczach_bez_akcji():
    """Bez `ctx.action` heurystyka `_DISCHARGE_HINTS` zostaje jedynym źródłem intencji."""
    result = apply_guards(
        {"discharge_limit": 30.0},
        GuardContext(
            state=DeviceState(soc=18.0, age_s=10.0),
            config=UserConfig(soc_reserve=20.0),
        ),
    )

    assert result.status is PureStatus.PARTIAL
    assert "discharge_limit" not in result.params
    assert result.params["eco_soc"] == 20.0
    assert {n.invariant for n in result.notes} == {"I-1"}


# ── Guardy czyste: intencja z planu ──────────────────────────────────────────


def _ctx(action: Action | None, *, soc: float, reserve: float) -> GuardContext:
    return GuardContext(
        state=DeviceState(soc=soc, age_s=10.0),
        limits=InverterLimits(),
        config=UserConfig(soc_reserve=reserve),
        action=action,
    )


def test_r1_i1_widzi_intencje_rozladowania_z_planu():
    """Parametry z `slot_to_params` nie mają `discharge_limit` — intencja jest w `action`."""
    result = apply_guards(
        {"mode": "eco_discharge", "eco_soc": 50.0, "eco_power": 50.0},
        _ctx(Action.DISCHARGE, soc=30.0, reserve=40.0),
    )

    assert result.status is PureStatus.PARTIAL
    assert {n.invariant for n in result.notes} == {"I-1"}
    assert result.forced_action is Action.SELF_CONSUME, (
        "samo usunięcie parametrów nie wystarcza — falownik zostałby w trybie "
        "z poprzedniego slotu"
    )


def test_r1_i1_nie_blokuje_ladowania_z_planu():
    result = apply_guards(
        {"mode": "eco_charge", "eco_soc": 80.0},
        _ctx(Action.CHARGE, soc=30.0, reserve=40.0),
    )

    assert result.status is PureStatus.SUCCESS
    assert result.forced_action is None
    assert result.params["mode"] == "eco_charge"


def test_r1_i2_sprzeczna_intencja_liczona_z_akcji_planu():
    """I-2 też musi pracować na intencji: plan mówi „ładuj", a parametry żądają rozładowania."""
    result = apply_guards(
        {"mode": "eco_charge", "discharge_limit": 30.0},
        _ctx(Action.CHARGE, soc=60.0, reserve=20.0),
    )

    assert result.status is PureStatus.ERROR
    assert result.params == {}
    assert {n.invariant for n in result.notes} == {"I-2"}
