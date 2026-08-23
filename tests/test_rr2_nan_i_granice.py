"""RR-2 + reszta R-8 — naprawa R-8 rozszczelniła fail-closed I-10 (runda 2 przeglądu).

Ustalenia (`docs/analysis/2026-08-16-faza-a-regresje.md`):

RR-2 (KRYTYCZNA): dla parametrów z `to_confirm=True`, które mają granicę z encji,
`sanitize_params` robiło `clean[key] = float(value); continue` — pomijając JAKĄKOLWIEK
kontrolę. `NaN` przechodzi `isinstance(value, (int, float))`, a przycinanie I-3
(`min(max(nan, lo), hi)`) zwraca `nan`, więc `hass.services.async_call` dostawał
`value: nan`. Osiągalne z chmury: `json.loads` domyślnie przyjmuje literał `NaN`.

R-8 (reszta a): werdykt I-10 zależał od DOSTĘPNOŚCI encji — ta sama komenda dostawała
raz `success` (encja ma `max`), raz `error` (encja `unavailable`, `_bounds` zwraca `None`).
To niedeterminizm widoczny dla użytkownika.

R-8 (reszta b): przycięcie do granicy raportowane jako `success` — chmura nie odróżniała
„zastosowano 3000" od „zastosowano 100 zamiast 3000".

Ten plik pilnuje OBU stron:
  * RR-2 — NaN/inf NIE MOŻE dojść do encji, ani z granicami z encji, ani bez nich,
  * R-8  — wartość powyżej granicy z encji NADAL ma być PRZYCIĘTA (wektor T-3),
           a nie odrzucona przez I-10; granice z encji NADAL mają pierwszeństwo
           nad zgadywanym `PARAM_SPECS`.

Specyfikacja: `volter-box/03-produkt/guardy-i-inwarianty.md` (I-3, I-10, T-3, T-11).
"""

from __future__ import annotations

import json
import math

import pytest

from custom_components.volter import executor as executor_mod
from custom_components.volter.command_handler import VolterCommandHandler
from custom_components.volter.const import PARAM_BOUNDS_CACHE_TTL_S
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.ha_state import ParamBoundsCache, read_inverter_limits
from tests.conftest import FakeHass
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

NAN = float("nan")
INF = float("inf")

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_charge_limit": "number.cl",
    "entity_eco_mode_soc": "number.eco_soc",
    "soc_reserve": 20.0,
}


def _ctx(limits: InverterLimits, *, soc: float = 55.0) -> GuardContext:
    return GuardContext(
        state=DeviceState(soc=soc, age_s=10.0),
        limits=limits,
        config=UserConfig(soc_reserve=20.0),
    )


def _noty(result) -> set[str]:
    return {n.invariant for n in result.notes}


def _hass() -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("sensor.pv", "1000")
    hass.states.set("sensor.grid", "-200")
    hass.states.set("select.tryb", "auto", {"options": ["auto", "charge_battery"]})
    hass.states.set("number.cl", "20", {"min": 0, "max": 100})
    hass.states.set("number.eco_soc", "20", {"min": 0, "max": 100})
    return hass


def _executor(hass: FakeHass, entry) -> VolterExecutor:
    entry.options = dict(OPTIONS)
    return VolterExecutor(hass, entry)


# ── RR-2: sonda P4 — NaN nie może przejść przez I-10 ─────────────────────────


def test_rr2_nan_odrzucony_mimo_granic_z_encji():
    """Sonda P4 w warstwie czystej: encja `number.cl` z `min=0`/`max=100` (stan normalny
    na żywym falowniku). Dziurę otworzyła dokładnie ścieżka skrócona R-8, więc test
    pilnuje jednocześnie, że wartość PRZEKRACZAJĄCA granicę nadal przez nią przechodzi
    (do przycięcia przez I-3), bo to była istota naprawy R-8."""
    limits = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 100.0)})

    with pytest.raises(InvalidCommand) as err:
        sanitize_params({"charge_limit": NAN}, limits)
    assert err.value.invariant == "I-10"

    # Druga strona: R-8 musi zostać zamknięte — 8000 A przechodzi sanityzację,
    # bo o górnej granicy decyduje encja (I-3 przytnie), a nie zgadywany PARAM_SPECS.
    assert sanitize_params({"charge_limit": 8000.0}, limits) == {"charge_limit": 8000.0}


@pytest.mark.parametrize("wartosc", [INF, -INF])
def test_rr2_nieskonczonosc_odrzucona_mimo_granic_z_encji(wartosc):
    """`inf` ma dokładnie ten sam problem co NaN: `isinstance` przepuszcza,
    a przycięcie do granicy w I-3 zwraca skończoną wartość tylko przypadkiem
    (dla `-inf` daje `lo`) — fail-closed ma odrzucić całą komendę."""
    limits = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 100.0)})

    with pytest.raises(InvalidCommand) as err:
        sanitize_params({"charge_limit": wartosc}, limits)
    assert err.value.invariant == "I-10"


def test_rr2_nan_bez_granic_z_encji_nadal_odrzucany():
    """Kontrola z ustalenia: BEZ granic z encji ta sama wartość była odrzucana
    poprawnie. To zachowanie nie może się zmienić przy okazji naprawy."""
    with pytest.raises(InvalidCommand) as err:
        sanitize_params({"charge_limit": NAN}, InverterLimits())
    assert err.value.invariant == "I-10"

    with pytest.raises(InvalidCommand):
        sanitize_params({"eco_soc": NAN}, InverterLimits())


def test_rr2_wartosc_ponizej_domeny_fizycznej_odrzucona_mimo_granic_z_encji():
    """Granica z encji zastępuje ZGADYWANY górny zakres (`to_confirm`), ale nie
    zwalnia z walidacji domeny: prąd ładowania < 0 A to bezsens niezależnie od tego,
    co wystawia encja."""
    limits = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 100.0)})

    with pytest.raises(InvalidCommand) as err:
        sanitize_params({"charge_limit": -5.0}, limits)
    assert err.value.invariant == "I-10"


def test_rr2_gigantyczna_liczba_calkowita_odrzucona_a_nie_wywraca_guarda():
    """Ten sam wektor co NaN, druga jego postać: JSON nie ogranicza precyzji liczb
    całkowitych, a `float(10**400)` rzuca `OverflowError` — wyjątek spoza kontraktu
    guarda. Fail-closed ma ODRZUCIĆ komendę (I-10), a nie wysypać się w środku
    sanityzacji, gdzie łapie to dopiero bariera R-10 w handlerze."""
    limits = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 100.0)})

    with pytest.raises(InvalidCommand) as err:
        sanitize_params({"charge_limit": 10**400}, limits)
    assert err.value.invariant == "I-10"

    with pytest.raises(InvalidCommand):
        sanitize_params({"eco_soc": 10**400}, InverterLimits())


def test_rr2_apply_guards_nie_przycina_nan_do_granicy():
    """Druga linia obrony. Nawet gdyby NaN ominął sanityzację (wołający spoza
    executora), pętla przycinająca I-3 NIE MOŻE go przepuścić: `min(max(nan, lo), hi)`
    zwraca `nan`, więc bez jawnego testu skończoności guard sam podaje NaN dalej."""
    limits = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 100.0)})

    result = apply_guards({"charge_limit": NAN}, _ctx(limits))

    assert result.status is Status.ERROR
    assert result.params == {}
    assert "I-3" in _noty(result)

    # Druga strona: zwykła wartość ponad granicą nadal ma być PRZYCIĘTA, nie odrzucona.
    ok = apply_guards({"charge_limit": 8000.0}, _ctx(limits))
    assert ok.params["charge_limit"] == 100.0


def test_rr2_apply_guards_odrzuca_nan_takze_bez_granic_z_encji():
    """Parametr BEZ granicy z encji nie przechodzi przez pętlę przycinającą, a
    porównanie `soc_max_hw < nan` jest fałszem — bez jawnego testu skończoności NaN
    przeszedłby przez cały `apply_guards` nietknięty."""
    result = apply_guards({"eco_soc": NAN}, _ctx(InverterLimits()))

    assert result.status is Status.ERROR
    assert result.params == {}


@pytest.mark.asyncio
async def test_rr2_nan_nie_dociera_do_encji_end_to_end(fake_entry):
    """Sonda P4 end-to-end: przed naprawą `apply_guards` dawało `status=success`,
    `executed=['charge_limit']`, a `hass.services.async_call` dostawał `value: nan`."""
    hass = _hass()
    executor = _executor(hass, fake_entry)

    result = await executor.async_apply({"charge_limit": NAN}, source="cloud")

    assert result.status is Status.ERROR
    assert result.executed == []
    wartosci = [d.get("value") for _dom, _srv, d in hass.services.calls]
    assert not any(isinstance(v, float) and math.isnan(v) for v in wartosci), (
        f"NaN dotarł do encji falownika: {hass.services.calls}"
    )


@pytest.mark.asyncio
async def test_rr2_literal_nan_z_chmury_odrzucony(fake_entry):
    """Osiągalność z chmury: `command_handler` używa `json.loads`, które domyślnie
    przyjmuje literał `NaN`. Cała droga (transport → handler → executor → applier)
    musi skończyć się błędem, a nie zapisem."""
    hass = _hass()
    executor = _executor(hass, fake_entry)
    handler = VolterCommandHandler(
        hass=hass, entry=fake_entry, device_id="dev-1",
        supabase_url="https://example.supabase.co",
        anon_key="anon", api_key="vk_test", executor=executor,
    )
    raporty: list[tuple] = []

    async def _zapamietaj(request_id, status, **kwargs):
        raporty.append((request_id, status, kwargs))

    handler._report_result = _zapamietaj

    payload = json.loads(
        '{"command": "SET_WORK_MODE", "request_id": "req-nan", '
        '"params": {"charge_limit": NaN}}'
    )
    assert math.isnan(payload["params"]["charge_limit"]), "warunek testu: JSON niesie NaN"

    await handler._execute_command(payload)

    assert raporty and raporty[0][1] == "error"
    assert hass.services.calls == [], f"nic nie mogło pójść do falownika: {hass.services.calls}"


# ── R-8 (reszta a): werdykt I-10 nie może zależeć od dostępności encji ───────


def test_rr2_granice_z_cache_gdy_encja_chwilowo_niedostepna():
    """Ta sama komenda musi dostawać ten sam werdykt niezależnie od tego, czy encja
    akurat odpowiada. Granice `min`/`max` to właściwość SPRZĘTU, nie stan — chwilowe
    `unavailable` nie jest powodem, żeby o nich zapomnieć."""
    hass = FakeHass()
    cache = ParamBoundsCache()
    hass.states.set("number.cl", "20", {"min": 0, "max": 5000})

    limits_ok = read_inverter_limits(hass, dict(OPTIONS), cache, now=100.0)
    assert limits_ok.param_bounds["charge_limit"] == ParamBounds(0.0, 5000.0)
    assert sanitize_params({"charge_limit": 3000.0}, limits_ok) == {"charge_limit": 3000.0}

    hass.states.set("number.cl", "unavailable", {})
    limits_awaria = read_inverter_limits(hass, dict(OPTIONS), cache, now=160.0)

    assert limits_awaria.param_bounds["charge_limit"] == ParamBounds(0.0, 5000.0)
    assert sanitize_params({"charge_limit": 3000.0}, limits_awaria) == {"charge_limit": 3000.0}, (
        "ta sama komenda nie może dostawać raz success, raz error tylko dlatego, "
        "że encja chwilowo nie odpowiada"
    )


def test_rr2_bez_cache_niedostepna_encja_nadal_nie_daje_granicy():
    """Druga strona R-8: `unavailable` znaczy „nie wiem", a nie „granica zero".
    Bez ANI JEDNEGO udanego odczytu nie ma czego zapamiętać — zostaje fail-closed
    z `PARAM_SPECS`, tak jak przed naprawą."""
    hass = FakeHass()
    hass.states.set("number.cl", "unavailable", {"min": 0, "max": 5000})

    limits = read_inverter_limits(hass, dict(OPTIONS), ParamBoundsCache(), now=100.0)

    assert "charge_limit" not in limits.param_bounds
    with pytest.raises(InvalidCommand):
        # 40 kW — poza `PARAM_SPECS` (0..30000 W). Wartość dobrana tak, żeby test
        # mierzył FAIL-CLOSED z `PARAM_SPECS`, a nie przypadkową ciasnotę zakresu:
        # 3000 W mieści się dziś w specyfikacji, bo encja `ems_power_limit` liczy w watach.
        sanitize_params({"charge_limit": 40000.0}, limits)


def test_rr2_cache_granic_wygasa_po_ttl():
    """Cache jest ograniczony w czasie: po `PARAM_BOUNDS_CACHE_TTL_S` wracamy do
    stanu „nie wiem". Wieczna pamięć granicy z wymienionego/przekonfigurowanego
    falownika byłaby groźniejsza niż jej brak."""
    hass = FakeHass()
    cache = ParamBoundsCache()
    hass.states.set("number.cl", "20", {"min": 0, "max": 5000})
    read_inverter_limits(hass, dict(OPTIONS), cache, now=100.0)

    hass.states.set("number.cl", "unavailable", {})
    tuz_przed = read_inverter_limits(
        hass, dict(OPTIONS), cache, now=100.0 + PARAM_BOUNDS_CACHE_TTL_S - 1.0
    )
    po_ttl = read_inverter_limits(
        hass, dict(OPTIONS), cache, now=100.0 + PARAM_BOUNDS_CACHE_TTL_S + 1.0
    )

    assert "charge_limit" in tuz_przed.param_bounds
    assert "charge_limit" not in po_ttl.param_bounds


def test_rr2_swiezy_odczyt_ma_pierwszenstwo_nad_cache():
    """Cache jest awaryjny, nie autorytatywny: gdy encja odpowiada, obowiązuje TO,
    co mówi teraz — inaczej zmiana konfiguracji falownika nigdy by nie dotarła."""
    hass = FakeHass()
    cache = ParamBoundsCache()
    hass.states.set("number.cl", "20", {"min": 0, "max": 5000})
    read_inverter_limits(hass, dict(OPTIONS), cache, now=100.0)

    hass.states.set("number.cl", "20", {"min": 0, "max": 100})
    limits = read_inverter_limits(hass, dict(OPTIONS), cache, now=160.0)

    assert limits.param_bounds["charge_limit"] == ParamBounds(0.0, 100.0)


@pytest.mark.asyncio
async def test_rr2_diagnose_nie_mutuje_cache_granic(fake_entry):
    """Kontrakt suchego przebiegu (R-7/R-14): `async_diagnose` nie zmienia stanu
    wewnętrznego executora — cache granic też nie."""
    hass = _hass()
    hass.states.set("number.cl", "20", {"min": 0, "max": 5000})
    executor = _executor(hass, fake_entry)
    await executor.async_apply({"eco_soc": 30.0}, source="test")
    przed = dict(executor._bounds_cache._entries)
    assert przed, "warunek testu: realny przebieg zapamiętuje granice z encji"

    hass.states.set("number.cl", "20", {"min": 0, "max": 100})
    await executor.async_diagnose()

    assert executor._bounds_cache._entries == przed, (
        "suchy przebieg nie może podmienić zapamiętanych granic realnego toru zapisu"
    )


# ── R-8 (reszta b): przycięcie to PARTIAL, nie success ───────────────────────


def test_rr2_przyciecie_do_granicy_encji_daje_partial():
    """DECYZJA WŁAŚCICIELA: informacja „nie zrealizowałem setpointu" jest dla chmury
    ważniejsza niż litera wektora T-3 („success z adnotacją"). Status niesie sygnał,
    nota niesie szczegół — przycięta wartość NADAL jest stosowana (to strona R-8)."""
    limits = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 100.0)})

    result = apply_guards(sanitize_params({"charge_limit": 3000.0}, limits), _ctx(limits))

    assert result.params["charge_limit"] == 100.0, "wartość musi zostać ZASTOSOWANA na granicy"
    assert result.status is Status.PARTIAL
    assert "I-3" in _noty(result)


def test_rr2_brak_przyciecia_zostaje_success():
    """Druga strona: PARTIAL ma znaczyć „setpoint niezrealizowany". Sama obecność
    granic z encji nie może degradować statusu każdej komendy."""
    limits = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 100.0)})

    result = apply_guards(sanitize_params({"charge_limit": 50.0}, limits), _ctx(limits))

    assert result.params["charge_limit"] == 50.0
    assert result.status is Status.SUCCESS


def test_rr2_przyciecie_eco_soc_do_limitu_sprzetowego_daje_partial():
    """Ta sama zasada dla drugiej gałęzi I-3 (`soc_max_hw`) — chmura nie może
    zgadywać, którym torem poszło przycięcie."""
    result = apply_guards({"eco_soc": 95.0}, _ctx(InverterLimits(soc_max_hw=90.0)))

    assert result.params["eco_soc"] == 90.0
    assert result.status is Status.PARTIAL
    assert "I-3" in _noty(result)


@pytest.mark.asyncio
async def test_rr2_przyciecie_raportowane_do_chmury_jako_partial(fake_entry):
    """End-to-end: chmura ma odróżnić „zastosowano 3000" od „zastosowano 100 zamiast
    3000". Zapis MUSI się odbyć (przycięcie nie jest odrzuceniem) i MUSI być widoczny
    w `executed` — inaczej naprawa R-8 przestaje mieć sens."""
    hass = _hass()
    executor = _executor(hass, fake_entry)

    result = await executor.async_apply({"charge_limit": 3000.0}, source="cloud")

    assert result.status is Status.PARTIAL
    assert result.executed == ["charge_limit"]
    assert ("number", "set_value", {"entity_id": "number.cl", "value": 100.0}) in hass.services.calls
    assert any(n.invariant == "I-3" for n in result.notes)
