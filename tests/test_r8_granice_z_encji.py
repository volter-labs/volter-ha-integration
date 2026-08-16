"""R-8 — ustalenie z przeglądu adwersaryjnego Fazy A
(`docs/analysis/2026-08-16-faza-a-review.md`): I-3 w części mocowej to martwy kod.

Ciało warunku dla `max_charge_w` w `guards.apply_guards` to literalne `pass`,
a `ha_state.read_inverter_limits` i tak nigdy nie ustawiało żadnej granicy nastawy.
Wektor T-3 ze specyfikacji („przytnij do granicy, zaloguj przycięcie") nie był
zaimplementowany, a test o nazwie `test_t3_*` asertował odrzucenie przez I-10 —
czyli utrwalał brak I-3 jako zachowanie oczekiwane.

Naprawa dostępna od ręki (bez czekania na mapę nastaw z Etapu 3): encje `number`
w Home Assistancie wystawiają w atrybutach `min`, `max` i `step`. To są REALNE
granice falownika, więc mają pierwszeństwo nad `PARAM_SPECS` (te są wyłącznie
sanity-checkiem, część z flagą `to_confirm`).

Specyfikacja: `volter-box/03-produkt/guardy-i-inwarianty.md` (I-3, wektor T-3).
"""

from __future__ import annotations

import pytest

from custom_components.volter.guards import (
    DeviceState,
    GuardContext,
    InvalidCommand,
    InverterLimits,
    ParamBounds,
    Status,
    UserConfig,
    apply_guards,
    sanitize_params,
)
from custom_components.volter.ha_state import read_inverter_limits
from tests.conftest import FakeHass


def ctx(limits: InverterLimits, *, soc: float = 55.0, reserve: float = 20.0) -> GuardContext:
    return GuardContext(
        state=DeviceState(soc=soc, age_s=10.0),
        limits=limits,
        config=UserConfig(soc_reserve=reserve),
    )


def invariants(result) -> set[str]:
    return {n.invariant for n in result.notes}


# ── T-3 wprost ze specyfikacji ───────────────────────────────────────────────


def test_r8_t3_charge_limit_przyciety_do_granicy_falownika():
    """T-3: `max_charge_w=5000`, komenda `charge_limit=8000` → zapis 5000 z adnotacją I-3.

    RR-2: status zmieniony na `partial` decyzją właściciela — patrz
    `test_rr2_przyciecie_do_granicy_encji_daje_partial`. Sedno R-8 (przycinamy zamiast
    odrzucać przez I-10) zostaje nietknięte: wartość jest ZASTOSOWANA na granicy."""
    limits = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 5000.0)})

    clean = sanitize_params({"charge_limit": 8000.0}, limits)
    result = apply_guards(clean, ctx(limits))

    assert result.params["charge_limit"] == 5000.0
    assert result.status is Status.PARTIAL
    assert "I-3" in invariants(result)


def test_r8_wartosc_ponizej_dolnej_granicy_encji_podniesiona():
    """Encje `number` mają też `min` — nastawa poniżej niego jest dla falownika
    równie nieosiągalna jak nastawa powyżej `max`."""
    limits = InverterLimits(param_bounds={"eco_power": ParamBounds(10.0, 100.0)})

    result = apply_guards(sanitize_params({"eco_power": 3.0}, limits), ctx(limits))

    assert result.params["eco_power"] == 10.0
    assert "I-3" in invariants(result)


def test_r8_przyciecie_jest_raportowane_statusem_partial():
    """RR-2 odwraca wcześniejszą decyzję spójności (było: „przycięcie to `success`
    z adnotacją, litera T-3"). Powód: `success` sprawiał, że chmura nie odróżniała
    „zastosowano 95" od „zastosowano 90 zamiast 95" — status niesie sygnał,
    nota niesie szczegół. Sama nastawa nadal JEST stosowana, tyle że na granicy."""
    limits = InverterLimits(soc_max_hw=90.0)

    result = apply_guards({"eco_soc": 95.0}, ctx(limits))

    assert result.params["eco_soc"] == 90.0
    assert result.status is Status.PARTIAL
    assert "I-3" in invariants(result)


# ── Pierwszeństwo granic z encji nad PARAM_SPECS ─────────────────────────────


def test_r8_granice_z_encji_maja_pierwszenstwo_nad_param_specs():
    """Bez granic z encji `charge_limit=8000` wypada poza zgadywany zakres
    `PARAM_SPECS` (0..200, `to_confirm=True`) i leci fail-closed przez I-10.
    Gdy znamy realną granicę z encji, zgadywanie musi ustąpić — decyduje I-3."""
    with pytest.raises(InvalidCommand) as err:
        sanitize_params({"charge_limit": 8000.0}, InverterLimits())
    assert err.value.invariant == "I-10"

    z_encji = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 5000.0)})
    assert sanitize_params({"charge_limit": 8000.0}, z_encji) == {"charge_limit": 8000.0}


def test_r8_procent_poza_domena_nadal_odrzucany_przez_i10():
    """Granice z encji nie mogą rozmiękczyć I-10 tam, gdzie zakres jest fizyczną
    domeną, a nie zgadywanym limitem sprzętu: `eco_soc=150` to nadal bezsens
    (T-11), niezależnie od tego, co wystawia encja."""
    limits = InverterLimits(param_bounds={"eco_soc": ParamBounds(0.0, 100.0)})

    with pytest.raises(InvalidCommand) as err:
        sanitize_params({"eco_soc": 150.0}, limits)
    assert err.value.invariant == "I-10"


# ── Odczyt granic z encji number ─────────────────────────────────────────────


OPTIONS = {
    "entity_ems_mode": "select.tryb",
    "entity_charge_limit": "number.charge_limit",
    "entity_discharge_limit": "number.discharge_limit",
    "entity_export_limit": "number.export_limit",
    "entity_eco_mode_power": "number.eco_power",
    "entity_eco_mode_soc": "number.eco_soc",
}


def test_r8_read_inverter_limits_czyta_min_max_z_encji_number():
    hass = FakeHass()
    hass.states.set("number.charge_limit", "20", {"min": 0, "max": 100, "step": 1})
    hass.states.set("number.discharge_limit", "30", {"min": 5, "max": 100})
    hass.states.set("number.export_limit", "5000", {"min": 0, "max": 6000})
    hass.states.set("number.eco_power", "50", {"min": 0, "max": 100})
    hass.states.set("number.eco_soc", "20", {"min": 10, "max": 100})

    limits = read_inverter_limits(hass, dict(OPTIONS))

    assert limits.param_bounds["charge_limit"] == ParamBounds(0.0, 100.0)
    assert limits.param_bounds["discharge_limit"] == ParamBounds(5.0, 100.0)
    assert limits.param_bounds["export_limit"] == ParamBounds(0.0, 6000.0)
    assert limits.param_bounds["eco_power"] == ParamBounds(0.0, 100.0)
    assert limits.param_bounds["eco_soc"] == ParamBounds(10.0, 100.0)


def test_r8_encja_bez_atrybutu_max_nie_daje_granicy():
    hass = FakeHass()
    hass.states.set("number.export_limit", "5000", {"min": 0, "step": 1})

    limits = read_inverter_limits(hass, dict(OPTIONS))

    assert "export_limit" not in limits.param_bounds


def test_r8_encja_niedostepna_nie_daje_granicy():
    """`unavailable` znaczy „nie wiem", a nie „granica zero" — zmyślona granica
    z niedostępnej encji przycięłaby nastawy do wartości bez pokrycia w sprzęcie."""
    hass = FakeHass()
    hass.states.set("number.export_limit", "unavailable", {"min": 0, "max": 6000})
    hass.states.set("number.eco_soc", "unknown", {})

    limits = read_inverter_limits(hass, dict(OPTIONS))

    assert "export_limit" not in limits.param_bounds
    assert "eco_soc" not in limits.param_bounds


def test_r8_max_rowne_zero_traktowane_jako_brak_granicy():
    """`max=0` to encja jeszcze niezainicjalizowana albo błędny odczyt. Wzięcie
    tego za prawdę przycięłoby każdą nastawę do zera i zatrzymało instalację —
    fail-safe jest wrócić do sanity-checku z PARAM_SPECS."""
    hass = FakeHass()
    hass.states.set("number.charge_limit", "0", {"min": 0, "max": 0})

    limits = read_inverter_limits(hass, dict(OPTIONS))

    assert "charge_limit" not in limits.param_bounds
    with pytest.raises(InvalidCommand):
        sanitize_params({"charge_limit": 8000.0}, limits)


def test_r8_brak_zmapowanej_encji_nie_daje_granicy():
    limits = read_inverter_limits(FakeHass(), {})

    assert limits.param_bounds == {}


@pytest.mark.asyncio
async def test_r8_diagnose_pokazuje_odczytane_granice(fake_entry):
    """Suchy przebieg musi odpowiadać na „czemu zapis jest inny niż plan" — bez
    wypisania granic nie da się odróżnić przycięcia przez I-3 od pomyłki w planie."""
    from custom_components.volter.executor import VolterExecutor

    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("number.export_limit", "5000", {"min": 0, "max": 6000})
    fake_entry.options = {**OPTIONS, "entity_soc": "sensor.soc"}

    report = await VolterExecutor(hass, fake_entry).async_diagnose()

    assert report["limits"]["param_bounds"] == {"export_limit": {"min": 0.0, "max": 6000.0}}
