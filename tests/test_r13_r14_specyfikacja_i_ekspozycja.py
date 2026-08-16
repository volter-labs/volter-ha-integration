"""R-13, R-14 — ustalenia z przeglądu adwersaryjnego Fazy A
(`docs/analysis/2026-08-16-faza-a-review.md`).

R-13 (NISKA): cztery drobne odchylenia od litery specyfikacji `guardy-i-inwarianty.md`:
  a) I-1 granica: kod ma `soc <= soc_reserve`, inwariant brzmi `SoC >= soc_reserve` —
     przy SoC RÓWNYM rezerwie inwariant jest spełniony, a kod fałszywie raportował
     naruszenie (PARTIAL) nawet gdy nic nie zmienił.
  b) I-8 off-by-one: `DirectionLimiter._current` startuje z `None`, więc pierwsze
     ustawienie kierunku liczy się jako zmiana — efektywny budżet to 3 odwrócenia/h
     zamiast 4.
  c) T-2 realnie nie jest pokryty: wartości ze specyfikacji (`charge_limit=3000`,
     `discharge_limit=2000`) w prawdziwym torze `sanitize_params -> apply_guards`
     zostają odrzucone przez I-10 (zakres 0..200 A dla `charge_limit`), zanim
     dojdzie do I-2 — test w `test_guards.py` woła `apply_guards` bezpośrednio,
     z pominięciem sanityzacji.
  d) selektor `user_mode`: `translation_key='user_mode'` bez sekcji
     `options.selector.user_mode` w `strings.json`/`translations/en.json`.

R-14 (NISKA, confirmed=false — ekspozycja, nie zaobserwowany błąd): `async_diagnose`
zwraca żywą referencję do `self._last`, a `GuardResult.as_report()` zwraca `self.params`
bez kopii. Konsument modyfikujący odpowiedź serwisu w miejscu podmieniłby stan
wewnętrzny executora — kontrakt "suchy przebieg" nie powinien opierać się na dobrej
woli konsumenta.

Specyfikacja: `volter-box/03-produkt/guardy-i-inwarianty.md` (I-1, I-8, I-10, I-2, T-2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import (
    Action,
    DirectionLimiter,
    GuardResult,
    Status,
)
from tests.conftest import FakeHass
from volter_pure.guards import DeviceState as PureDeviceState
from volter_pure.guards import GuardContext as PureGuardContext
from volter_pure.guards import UserConfig as PureUserConfig
from volter_pure.guards import Status as PureStatus
from volter_pure.guards import apply_guards as pure_apply_guards

INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "volter"


def _pure_ctx(soc: float, reserve: float) -> PureGuardContext:
    return PureGuardContext(
        state=PureDeviceState(soc=soc, age_s=10.0),
        config=PureUserConfig(soc_reserve=reserve),
    )


# ── R-13a: I-1 granica SoC == soc_reserve ────────────────────────────────────


def test_r13a_soc_rowny_rezerwie_spelnia_inwariant():
    """Inwariant I-1 brzmi `SoC >= soc_reserve`. Przy SoC dokładnie równym rezerwie
    inwariant JEST spełniony — guard nie powinien zerować rozładowania ani zgłaszać
    naruszenia."""
    result = pure_apply_guards({"discharge_limit": 30.0}, _pure_ctx(soc=20.0, reserve=20.0))

    assert result.status is PureStatus.SUCCESS
    assert result.params.get("discharge_limit") == 30.0
    assert not any(n.invariant == "I-1" for n in result.notes)


def test_r13a_brak_zmiany_nie_generuje_falszywego_partial():
    """Gdy SoC < rezerwa, ale `eco_soc` w komendzie już wynosi rezerwę i nie ma
    intencji rozładowania, guard niczego nie zmienia — status musi zostać SUCCESS,
    nie PARTIAL. Dotychczasowy kod oznaczał PARTIAL, gdy `eco_soc` PO operacji
    wychodził równy rezerwie, niezależnie od tego, czy cokolwiek naprawdę zmienił."""
    result = pure_apply_guards({"eco_soc": 20.0}, _pure_ctx(soc=15.0, reserve=20.0))

    assert result.status is PureStatus.SUCCESS
    assert result.params.get("eco_soc") == 20.0
    assert not any(n.invariant == "I-1" for n in result.notes)


# ── R-13b: I-8 off-by-one przy pierwszym ustawieniu kierunku ─────────────────


def test_r13b_pierwsze_ustawienie_kierunku_nie_zuzywa_budzetu():
    """`DirectionLimiter._current` startuje z `None` — pierwsze ustawienie kierunku
    NIE jest "zmianą kierunku" (nie ma poprzedniego kierunku, względem którego
    mogłaby zajść zmiana). Budżet 4 zmian/h musi dotyczyć FAKTYCZNYCH odwróceń,
    inaczej efektywny budżet to 3."""
    limiter = DirectionLimiter(max_changes_per_hour=4)
    # Pierwsze ustawienie (CHARGE) darmowe + 4 faktyczne odwrócenia mieszczą się
    # w budżecie — to jest dokładnie 5 elementów sekwencji.
    sequence = [
        Action.CHARGE,
        Action.DISCHARGE,
        Action.CHARGE,
        Action.DISCHARGE,
        Action.CHARGE,
    ]
    for i, action in enumerate(sequence):
        allowed, note = limiter.allows(action, now_ts=float(i))
        assert allowed is True, f"krok {i} ({action}) powinien zmieścić się w budżecie 4 zmian/h"
        assert note is None
        limiter.record(action, now_ts=float(i))


# ── R-13d: sekcja options.selector.user_mode w strings.json / en.json ────────


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_r13d_selektor_user_mode_ma_etykiety(filename: str):
    """`translation_key='user_mode'` w `config_flow.py` bez sekcji
    `options.selector.user_mode` renderuje surowe klucze enuma w UI, a hassfest
    może zgłosić brak."""
    data = json.loads((INTEGRATION_DIR / filename).read_text(encoding="utf-8"))
    user_mode_options = (
        data.get("options", {}).get("selector", {}).get("user_mode", {}).get("options", {})
    )
    for key in ("earn", "autarky", "backup"):
        assert key in user_mode_options, (
            f"{filename}: brak etykiety '{key}' w options.selector.user_mode.options"
        )


# ── R-14: żywa referencja do stanu wewnętrznego executora ────────────────────


def test_r14_as_report_params_to_kopia_nie_zywa_referencja():
    """`GuardResult.as_report()['params']` musi być kopią — konsument modyfikujący
    odpowiedź serwisu w miejscu nie może podmienić `GuardResult.params`."""
    result = GuardResult(params={"eco_soc": 20.0}, status=Status.SUCCESS)

    report = result.as_report()
    report["params"]["eco_soc"] = 999.0

    assert result.params["eco_soc"] == 20.0


OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
}


def _hass_ze_swiezym_stanem() -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", "55")
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb", "general", {"options": ["general", "eco_charge", "eco_discharge"]}
    )
    hass.states.set("number.eco_soc", "20")
    return hass


@pytest.mark.asyncio
async def test_r14_diagnose_last_nie_jest_zywa_referencja(fake_entry):
    """`report['last']` musi być kopią `self._last` — modyfikacja odpowiedzi serwisu
    w miejscu nie może zmienić stanu wewnętrznego executora (do niego sięga kolejny
    realny `async_apply`, m.in. w logu decyzji i w `diagnostics`)."""
    hass = _hass_ze_swiezym_stanem()
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    executor._last = {"source": "schedule", "executed": ["eco_soc"]}

    report = await executor.async_diagnose()
    report["last"]["executed"].append("zmienione-z-zewnatrz")

    assert executor._last["executed"] == ["eco_soc"]
