"""RR-1 — naprawa R-5 tworzyła trwały lockout toru zapisu (runda 2 przeglądu).

Ustalenie RR-1 (`docs/analysis/2026-08-16-faza-a-regresje.md`): po naprawie R-5
`_prev_soc` aktualizował się WYŁĄCZNIE z odczytów, które przeszły I-9. Jeden rozjazd
baseline'u zamrażał go na starej wartości i każdy kolejny tick też był odrzucany —
bez ścieżki wyjścia innej niż restart HA. Falownik zostawał na nastawie sprzed awarii
bezterminowo, czyli dokładnie scenariusz L-2, przed którym istnieje cały executor.

Naprawa jest KONCEPCYJNA: próg skoku SoC ma sens wyłącznie między świeżymi próbkami.
Po 40 minutach przerwy zmiana o 35 pp jest fizycznie normalna. I-9 patrzy więc na
TEMPO zmiany (pp/minutę), a nie na różnicę bezwzględną, i ma jawną ścieżkę wyjścia:
próbka starsza niż `MAX_STATE_AGE_S` przestaje być punktem odniesienia — bieżący
odczyt zostaje przyjęty jako NOWY BASELINE z notą I-9.

Ten plik pilnuje OBU stron:
  * RR-1 — powrót sensora po długiej przerwie MUSI odblokować tor zapisu,
  * R-5  — błędny odczyt między dwiema ŚWIEŻYMI próbkami NADAL jest degraded i NADAL
           nie staje się zaufanym baseline'em następnego przebiegu.

Specyfikacja: `volter-box/03-produkt/guardy-i-inwarianty.md` (I-9, T-9, T-10).
"""

from __future__ import annotations

import pytest

from custom_components.volter import executor as executor_mod
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import Status
from tests.conftest import FakeHass
from volter_pure.guards import (
    DeviceState,
    GuardContext,
    UserConfig,
    apply_guards,
)
from volter_pure.guards import Status as PureStatus


OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_export_limit": "number.export_limit",
    "entity_export_limit_switch": "switch.export_limit",
    "soc_reserve": 25.0,
}


class _Zegar:
    """Podstawka pod `time` w `executor` — sterowalny zegar monotoniczny.

    RR-1 dotyczy UPŁYWU CZASU między próbkami SoC, a test wykonuje 40 tików
    w ułamku sekundy. Bez sterowalnego zegara scenariusza nie da się odtworzyć.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def przesun(self, sekundy: float) -> None:
        self.t += sekundy


def _hass(soc: str = "50") -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", soc)
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb", "general", {"options": ["general", "eco_charge", "eco_discharge"]}
    )
    hass.states.set("number.eco_soc", "20")
    hass.states.set("number.export_limit", "5000")
    return hass


def _executor(hass: FakeHass, entry, monkeypatch, zegar: _Zegar) -> VolterExecutor:
    entry.options = dict(OPTIONS)
    monkeypatch.setattr(executor_mod, "time", zegar)
    return VolterExecutor(hass, entry)


def _noty(result) -> set[str]:
    return {n.invariant for n in result.notes}


# ── RR-1: sonda P11 — powrót sensora po 40 min musi odblokować zapisy ────────


@pytest.mark.asyncio
async def test_rr1_powrot_sensora_po_40_min_nie_jest_odrzucany(fake_entry, monkeypatch):
    """Sonda P11 w wersji minimalnej: baseline 50% zapisany 40 min temu, sensor wraca
    ze świeżym 85%. |85-50| = 35 pp > MAX_SOC_JUMP_PP, ale przez 40 minut bateria
    naprawdę mogła się naładować — I-9 nie ma prawa tego odrzucić i MUSI przyjąć
    odczyt jako nowy baseline."""
    zegar = _Zegar()
    hass = _hass(soc="85")
    executor = _executor(hass, fake_entry, monkeypatch, zegar)
    executor._prev_soc = 50.0
    executor._prev_soc_ts = zegar.monotonic() - 2400.0  # próbka sprzed 40 minut

    result = await executor.async_apply({"eco_soc": 30.0}, source="test")

    assert result.status is not Status.DEGRADED, (
        "po 40 minutach przerwy zmiana o 35 pp jest fizycznie normalna — I-9 nie może "
        "jej traktować jak niemożliwego skoku"
    )
    assert executor._prev_soc == 85.0, (
        "stara próbka przestaje być punktem odniesienia: bieżący odczyt musi zostać "
        "NOWYM baseline'em, inaczej lockout trwa w nieskończoność"
    )
    assert "I-9" in _noty(result), "przyjęcie nowego baseline'u musi zostawić ślad w nocie I-9"


@pytest.mark.asyncio
async def test_rr1_40_min_niedostepnosci_nie_blokuje_toru_zapisu_na_stale(
    fake_entry, monkeypatch
):
    """Pełna sonda P11: praca normalna → sensor `unavailable` przez 40 minut →
    powrót z 85%. Przed naprawą kolejne tiki były `degraded` w nieskończoność,
    a zapisów było 0. Po naprawie system wraca do pracy i realnie pisze."""
    zegar = _Zegar()
    hass = _hass(soc="50")
    executor = _executor(hass, fake_entry, monkeypatch, zegar)

    # Praca normalna — baseline 50% ustalony i zapisany.
    tick0 = await executor._async_execute_now()
    assert tick0.status is not Status.DEGRADED
    assert executor._prev_soc == 50.0
    assert tick0.executed, "warunek testu: tor zapisu działa przed awarią"

    # Integracja falownika odpada na 40 minut (40 tików pętli po 60 s).
    hass.states.set("sensor.soc", "unavailable")
    for _ in range(40):
        zegar.przesun(60.0)
        awaria = await executor._async_execute_now()
        assert awaria.status is Status.DEGRADED

    # Encja wraca; bateria realnie naładowała się z PV do 85%.
    hass.states.set("sensor.soc", "85")
    # Zmiana rezerwy gwarantuje, że jest CO zapisać (I-6 nie przepuszcza wartości
    # niezmienionych) — chodzi o dowód, że tor zapisu jest odblokowany, nie tylko
    # o brak statusu DEGRADED.
    fake_entry.options["soc_reserve"] = 30.0
    zegar.przesun(60.0)

    powrot = await executor._async_execute_now()

    assert powrot.status is not Status.DEGRADED, (
        "po powrocie sensora system MUSI wrócić do pracy — degraded w nieskończoność "
        "to scenariusz L-2 (falownik na nastawie sprzed awarii bezterminowo)"
    )
    assert powrot.executed, "tor zapisu musi być odblokowany, nie tylko status"
    assert executor._prev_soc == 85.0

    # Kolejny tick pracuje już na nowym baseline — bez nawrotu do degraded.
    zegar.przesun(60.0)
    kolejny = await executor._async_execute_now()
    assert kolejny.status is not Status.DEGRADED


# ── R-5: ochrona, której NIE WOLNO zepsuć ───────────────────────────────────


@pytest.mark.asyncio
async def test_r5_skok_miedzy_swiezymi_probkami_nadal_degraded(fake_entry, monkeypatch):
    """Druga strona RR-1: przy odstępie 10 s skok 45 pp jest fizycznie niemożliwy
    (4,5 pp/s), więc NADAL musi dawać `degraded` — i NADAL nie może stać się
    zaufanym baseline'em następnego przebiegu (to jest istota naprawy R-5)."""
    zegar = _Zegar()
    hass = _hass(soc="95")
    executor = _executor(hass, fake_entry, monkeypatch, zegar)
    executor._prev_soc = 50.0
    executor._prev_soc_ts = zegar.monotonic() - 10.0  # świeża próbka sprzed 10 s

    tick1 = await executor.async_apply({"eco_soc": 30.0}, source="test")
    assert tick1.status is Status.DEGRADED
    assert "I-9" in _noty(tick1)
    assert executor._prev_soc == 50.0, (
        "odczyt odrzucony jako fizycznie niemożliwy nie może stać się baseline'em"
    )

    # Kolejny tick, wciąż w oknie świeżości poprzedniej próbki — ochrona trwa.
    zegar.przesun(60.0)
    tick2 = await executor.async_apply({"eco_soc": 30.0}, source="test")
    assert tick2.status is Status.DEGRADED, (
        "I-9 musi chronić dłużej niż jeden tick, dopóki poprzednia próbka jest świeża"
    )
    assert executor._prev_soc == 50.0
    assert not tick2.executed


# ── Warstwa czysta: tempo, ścieżka wyjścia i zgodność ze starym kontraktem ──


def _ctx(
    soc: float,
    previous_soc: float | None,
    previous_soc_age_s: float | None,
    age_s: float = 10.0,
) -> GuardContext:
    return GuardContext(
        state=DeviceState(
            soc=soc,
            age_s=age_s,
            previous_soc=previous_soc,
            previous_soc_age_s=previous_soc_age_s,
        ),
        config=UserConfig(soc_reserve=20.0),
    )


def test_rr1_guard_dopuszcza_zmiane_proporcjonalna_do_uplynietego_czasu():
    """Tempo, nie różnica bezwzględna: 15 pp w 4 minuty to 3,75 pp/min — mieści się
    w limicie fizycznym, więc zapis przechodzi."""
    result = apply_guards({"eco_soc": 30.0}, _ctx(65.0, 50.0, 240.0))

    assert result.status is not PureStatus.DEGRADED
    assert result.soc_baseline_ok is True


def test_rr1_guard_odrzuca_zmiane_ponad_realne_tempo():
    """Ta sama różnica 15 pp, ale w 60 s (15 pp/min ≈ 9C dla domowego magazynu) —
    fizycznie niemożliwa, więc degraded. Stary test bezwzględny (<= 20 pp) by ją
    przepuścił: naprawa RR-1 nie tylko rozluźnia, ale i zacieśnia tam, gdzie trzeba."""
    result = apply_guards({"eco_soc": 30.0}, _ctx(65.0, 50.0, 60.0))

    assert result.status is PureStatus.DEGRADED
    assert result.params == {}
    assert result.soc_baseline_ok is False


def test_rr1_guard_stara_probka_daje_nowy_baseline_zamiast_odrzucenia():
    """Ścieżka wyjścia: poprzednia próbka starsza niż `max_state_age_s` przestaje być
    punktem odniesienia. Bez tego jeden rozjazd zamraża baseline na stałe."""
    ctx = _ctx(85.0, 50.0, 2400.0)
    result = apply_guards({"eco_soc": 30.0}, ctx)

    assert result.status is not PureStatus.DEGRADED
    assert result.soc_baseline_ok is True
    assert any(n.invariant == "I-9" for n in result.notes)
    assert result.params, "po przyjęciu nowego baseline'u zapisy muszą znów przechodzić"


def test_rr1_guard_nieznany_odstep_zachowuje_stary_prog_bezwzgledny():
    """Kontrakt dla wołających spoza executora (i dla wektora T-10): gdy odstępu
    między próbkami NIE ZNAMY, zostaje konserwatywny próg bezwzględny — brak wiedzy
    o czasie nie może rozmiękczać I-9."""
    result = apply_guards({"eco_soc": 30.0}, _ctx(95.0, 40.0, None))

    assert result.status is PureStatus.DEGRADED
    assert result.params == {}
    assert result.soc_baseline_ok is False
