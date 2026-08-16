"""Wektory testowe T-1…T-14 ze specyfikacji guardów.

Źródło: `Volter-BOX/03-produkt/guardy-i-inwarianty.md` §4.

Te testy nie importują Home Assistanta — `guards.py` i `schedule.py` są świadomie czyste,
więc uruchamiają się zwykłym `pytest` bez harnessu HA. Ten sam zestaw przypadków
uruchomi później firmware jako testy hosta.

Uruchomienie:  pytest tests/test_guards.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from volter_pure.guards import (
    Action,
    DeviceState,
    DirectionLimiter,
    GuardContext,
    GuardResult,
    InvalidCommand,
    InverterLimits,
    ParamBounds,
    RequestDeduplicator,
    Status,
    UserConfig,
    WriteThrottle,
    apply_guards,
    infer_action,
    ordered,
    sanitize_params,
)
from volter_pure.schedule import Fallback, Schedule, Slot

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def ctx(
    soc: float = 50.0,
    *,
    reserve: float = 20.0,
    mode: str = "autarky",
    age_s: float = 10.0,
    previous_soc: float | None = None,
    price: float | None = None,
    limits: InverterLimits | None = None,
) -> GuardContext:
    return GuardContext(
        state=DeviceState(soc=soc, age_s=age_s, previous_soc=previous_soc),
        limits=limits or InverterLimits(),
        config=UserConfig(soc_reserve=reserve, mode=mode),
        price_pln_kwh=price,
    )


def invariants(result) -> set[str]:
    return {n.invariant for n in result.notes}


# ── T-1: I-1 rezerwa użytkownika ─────────────────────────────────────────────


def test_t1_soc_ponizej_rezerwy_zeruje_rozladowanie():
    result = apply_guards({"discharge_limit": 30.0}, ctx(soc=18.0, reserve=20.0))

    assert result.status is Status.PARTIAL
    assert "discharge_limit" not in result.params
    assert result.params["eco_soc"] == 20.0
    assert "I-1" in invariants(result)


# ── T-2: I-2 sprzeczna intencja ──────────────────────────────────────────────


def test_t2_guard_bezposrednio_sprzeczna_intencja_odrzucona():
    """Wywołuje `apply_guards` BEZPOŚREDNIO na już-"czystych" parametrach, z
    pominięciem `sanitize_params` — sprawdza wyłącznie logikę I-2 w guardzie,
    w oderwaniu od realnego toru komendy (`sanitize_params -> apply_guards`).

    R-13c: dosłowne wartości z wektora T-2 specyfikacji (`charge_limit=3000`,
    `discharge_limit=2000`) w realnym torze zostałyby odrzucone przez I-10
    (zakres `charge_limit` to 0..200 A) ZANIM guard w ogóle zobaczyłby I-2 — ten
    test celowo tego nie odzwierciedla. Realny tor pokrywa
    `test_t2_pelny_tor_sanitize_i_guardy_sprzeczna_intencja_odrzucona` niżej.
    """
    result = apply_guards({"charge_limit": 20.0, "discharge_limit": 30.0}, ctx())

    assert result.status is Status.ERROR
    assert result.params == {}
    assert "I-2" in invariants(result)


def test_t2_pelny_tor_sanitize_i_guardy_sprzeczna_intencja_odrzucona():
    """R-13c: wektor T-2 pokryty REALNYM torem komendy — `sanitize_params`, potem
    dopiero `apply_guards`. Wartości muszą mieścić się w `PARAM_SPECS` (I-10),
    żeby sprzeczność faktycznie dotarła do I-2, a nie została odrzucona wcześniej
    z mylącym powodem `I-10` zamiast `I-2` (dosłowne wartości z tabeli
    specyfikacji, `charge_limit=3000`/`discharge_limit=2000`, tego testu by NIE
    przeszły — `charge_limit` ma zakres 0..200 A)."""
    limits = InverterLimits()
    clean = sanitize_params({"charge_limit": 20.0, "discharge_limit": 30.0}, limits)

    result = apply_guards(clean, ctx(limits=limits))

    assert result.status is Status.ERROR
    assert result.params == {}
    assert "I-2" in invariants(result)


# ── T-3: I-3 przycięcie do granic sprzętowych ────────────────────────────────


def test_t3_setpoint_powyzej_granicy_falownika_przyciety_a_nie_odrzucony():
    """T-3 dosłownie: `max_charge_w=5000`, komenda `charge_limit=8000` → zapis 5000,
    status `success` z adnotacją o przycięciu, powód I-3.

    R-8: ten test asertował wcześniej ODRZUCENIE przez I-10 — czyli opisywał ówczesną
    implementację (I-3 w części mocowej było martwe), a nie wymaganie ze specyfikacji.
    Granica bierze się dziś z atrybutu `max` encji `number` (`InverterLimits.param_bounds`).
    """
    limits = InverterLimits(param_bounds={"charge_limit": ParamBounds(0.0, 5000.0)})

    result = apply_guards(sanitize_params({"charge_limit": 8000.0}, limits), ctx(limits=limits))

    assert result.params["charge_limit"] == 5000.0
    assert result.status is Status.SUCCESS
    assert "I-3" in invariants(result)


def test_t3_eco_soc_przyciety_do_limitu_sprzetowego():
    result = apply_guards(
        {"eco_soc": 95.0}, ctx(limits=InverterLimits(soc_max_hw=90.0))
    )

    assert result.params["eco_soc"] == 90.0
    assert result.status is Status.SUCCESS
    assert "I-3" in invariants(result)


def test_i10_wartosc_poza_param_specs_odrzucona_gdy_nie_znamy_granic_encji():
    """Osobny przypadek I-10, nie T-3: dopóki encja nie poda `min`/`max`, jedyną
    obroną zostaje zgadywany zakres z `PARAM_SPECS` i wtedy obowiązuje fail-closed."""
    with pytest.raises(InvalidCommand) as err:
        sanitize_params({"charge_limit": 8000.0}, InverterLimits())
    assert err.value.invariant == "I-10"


# ── T-4: I-4 cena ujemna ─────────────────────────────────────────────────────


def test_t4_cena_ujemna_blokuje_eksport_wbrew_planowi():
    result = apply_guards(
        {"export_limit": 5000.0, "export_limit_enabled": False},
        ctx(price=-0.05),
    )

    assert result.params["export_limit"] == 0.0
    assert result.params["export_limit_enabled"] is True
    assert "I-4" in invariants(result)


# ── T-5: I-5 wygaśnięcie planu → fallback ────────────────────────────────────


def test_t5_po_wygasnieciu_planu_wchodzi_fallback_a_nie_stary_setpoint():
    schedule = Schedule(
        slots=[
            Slot(
                start=NOW - timedelta(hours=2),
                end=NOW - timedelta(minutes=30),
                action=Action.DISCHARGE,
                soc_target=10.0,
            )
        ],
        fallback=Fallback(action=Action.SELF_CONSUME, soc_reserve=20.0),
    )

    assert schedule.is_expired(NOW)
    slot, is_fallback = schedule.effective_slot(NOW)
    assert is_fallback is True
    assert slot.action is Action.SELF_CONSUME
    assert slot.soc_target == 20.0


def test_t5b_w_trakcie_planu_uzywamy_slotu_nie_fallbacku():
    schedule = Schedule(
        slots=[
            Slot(
                start=NOW - timedelta(minutes=10),
                end=NOW + timedelta(minutes=50),
                action=Action.CHARGE,
                soc_target=80.0,
            )
        ]
    )

    slot, is_fallback = schedule.effective_slot(NOW)
    assert is_fallback is False
    assert slot.action is Action.CHARGE
    assert slot.soc_target == 80.0


# ── T-6: I-6 throttling zapisów ──────────────────────────────────────────────


def test_t6_niezmieniona_wartosc_nie_jest_zapisywana_ponownie():
    throttle = WriteThrottle(min_interval_s=60.0)

    allowed, _ = throttle.filter({"eco_soc": 30.0}, now_ts=0.0)
    assert allowed == {"eco_soc": 30.0}
    throttle.commit(allowed, now_ts=0.0)

    allowed, notes = throttle.filter({"eco_soc": 30.0}, now_ts=10.0)
    assert allowed == {}
    assert notes and notes[0].invariant == "I-6"


def test_t6b_zapis_czesciej_niz_min_interval_pominiety():
    throttle = WriteThrottle(min_interval_s=60.0)
    throttle.commit({"eco_soc": 30.0}, now_ts=0.0)

    allowed, notes = throttle.filter({"eco_soc": 35.0}, now_ts=10.0)
    assert allowed == {}
    assert notes and notes[0].invariant == "I-6"

    allowed, _ = throttle.filter({"eco_soc": 35.0}, now_ts=61.0)
    assert allowed == {"eco_soc": 35.0}


# ── T-7: I-7 rezerwa w trybie Backup ─────────────────────────────────────────


def test_t7_tryb_backup_nie_pozwala_obnizyc_rezerwy():
    result = apply_guards(
        {"eco_soc": 10.0}, ctx(soc=80.0, reserve=50.0, mode="backup")
    )

    assert result.status is Status.ERROR
    assert result.params == {}
    assert "I-7" in invariants(result)


# ── T-8: I-8 anty-oscylacja ──────────────────────────────────────────────────


def test_t8_piata_zmiana_kierunku_w_godzinie_zignorowana():
    """R-13b: `_current` startuje z `None`, więc PIERWSZE ustawienie kierunku nie
    jest jeszcze "zmianą" (nie ma poprzedniego kierunku, względem którego mogłaby
    zajść zmiana) — nie zużywa budżetu. Sekwencja niżej to: 1 darmowe ustawienie
    startowe + 4 faktyczne odwrócenia, dokładnie w limicie N=4; dopiero PIĄTE
    odwrócenie ma zostać zignorowane. Test wcześniej utrwalał off-by-one (budżet
    efektywnie 3 zamiast 4), bo liczył ustawienie startowe jako pierwszą zmianę.
    """
    limiter = DirectionLimiter(max_changes_per_hour=4)
    sequence = [
        Action.CHARGE,  # ustawienie startowe — darmowe
        Action.DISCHARGE,  # zmiana 1
        Action.CHARGE,  # zmiana 2
        Action.DISCHARGE,  # zmiana 3
        Action.CHARGE,  # zmiana 4 — ostatnia w budżecie
    ]

    for i, action in enumerate(sequence):
        allowed, note = limiter.allows(action, now_ts=float(i))
        assert allowed is True, f"zmiana {i} powinna przejść"
        assert note is None
        limiter.record(action, now_ts=float(i))

    # Piąta faktyczna zmiana kierunku (DISCHARGE) przekracza budżet N=4.
    allowed, note = limiter.allows(Action.DISCHARGE, now_ts=6.0)
    assert allowed is False
    assert note is not None and note.invariant == "I-8"


def test_t8b_po_wyjsciu_z_okna_zmiany_znowu_dozwolone():
    """R-13b: ustawienie startowe (CHARGE) darmowe, potem 2 faktyczne odwrócenia
    wyczerpują budżet `max_changes_per_hour=2` — trzecie jest blokowane, dopóki
    okno 1h się nie przesunie."""
    limiter = DirectionLimiter(max_changes_per_hour=2, window_s=3600.0)
    for i, action in enumerate([Action.CHARGE, Action.DISCHARGE, Action.CHARGE]):
        limiter.record(action, now_ts=float(i))

    assert limiter.allows(Action.DISCHARGE, now_ts=10.0)[0] is False
    assert limiter.allows(Action.DISCHARGE, now_ts=4000.0)[0] is True


# ── T-9 / T-10: I-9 świeżość i wiarygodność telemetrii ───────────────────────


def test_t9_przestarzaly_odczyt_wstrzymuje_zapisy():
    result = apply_guards({"eco_soc": 30.0}, ctx(age_s=400.0))

    assert result.status is Status.DEGRADED
    assert result.params == {}
    assert "I-9" in invariants(result)


def test_t9b_brak_odczytu_soc_wstrzymuje_zapisy():
    context = GuardContext(state=DeviceState(soc=None, age_s=5.0))
    result = apply_guards({"eco_soc": 30.0}, context)

    assert result.status is Status.DEGRADED
    assert result.params == {}


def test_t10_niemozliwy_skok_soc_wstrzymuje_zapisy():
    result = apply_guards({"eco_soc": 30.0}, ctx(soc=95.0, previous_soc=40.0))

    assert result.status is Status.DEGRADED
    assert result.params == {}
    assert "I-9" in invariants(result)


# ── T-11: I-10 fail-closed ───────────────────────────────────────────────────


def test_t11_wartosc_poza_zakresem_odrzuca_cala_komende():
    with pytest.raises(InvalidCommand) as err:
        sanitize_params({"eco_soc": 150.0, "eco_power": 50.0}, InverterLimits())
    assert err.value.invariant == "I-10"


def test_t11b_nieznany_parametr_jest_ignorowany_a_nie_wywala_komendy():
    clean = sanitize_params({"eco_soc": 30.0, "cos_nowego": 1}, InverterLimits())
    assert clean == {"eco_soc": 30.0}


def test_t11c_mode_walidowany_wobec_listy_opcji_falownika():
    limits = InverterLimits(allowed_modes=("general", "eco_charge", "eco_discharge"))

    assert sanitize_params({"mode": "eco_charge"}, limits) == {"mode": "eco_charge"}
    with pytest.raises(InvalidCommand):
        sanitize_params({"mode": "turbo"}, limits)


# ── T-12: L-4 idempotencja ───────────────────────────────────────────────────


def test_t12_powtorzony_request_id_rozpoznany_jako_duplikat():
    dedup = RequestDeduplicator()

    assert dedup.is_duplicate("req-1") is False
    dedup.remember("req-1")
    assert dedup.is_duplicate("req-1") is True
    assert dedup.is_duplicate("req-2") is False


# ── T-13: L-6 jawna kolejność zapisu ─────────────────────────────────────────


def test_t13_mode_zapisywany_przed_limitami():
    keys = [k for k, _ in ordered({"charge_limit": 10, "export_limit": 0, "mode": "eco_charge"})]

    assert keys.index("mode") < keys.index("charge_limit")
    assert keys.index("charge_limit") < keys.index("export_limit")


def test_t13b_kolejnosc_jest_deterministyczna_dla_nieznanych_parametrow():
    keys = [k for k, _ in ordered({"zzz": 1, "aaa": 2, "mode": "general"})]
    assert keys == ["mode", "aaa", "zzz"]


# ── T-14: ponawianie zapisu ──────────────────────────────────────────────────
# T-14 (retry x2 -> DEGRADED) dotyczy `applier.py`, który importuje Home Assistanta.
# Do przetestowania po dołożeniu harnessu `pytest-homeassistant-custom-component`,
# wzorem `volcast-ha-integration/tests/conftest.py`.


# ── Dodatkowe: przypadek szczęśliwy i wnioskowanie kierunku ──────────────────


def test_zdrowa_komenda_przechodzi_bez_zmian():
    result = apply_guards({"eco_soc": 60.0, "eco_power": 40.0}, ctx(soc=55.0))

    assert result.status is Status.SUCCESS
    assert result.params == {"eco_soc": 60.0, "eco_power": 40.0}
    assert result.notes == []


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"charge_limit": 10}, Action.CHARGE),
        ({"discharge_limit": 10}, Action.DISCHARGE),
        ({"mode": "eco_charge"}, Action.CHARGE),
        ({"mode": "eco_discharge"}, Action.DISCHARGE),
        ({"mode": "general"}, Action.SELF_CONSUME),
    ],
)
def test_wnioskowanie_kierunku(params, expected):
    assert infer_action(params) is expected


def test_n4_executed_jest_niezalezne_od_params():
    """Raport musi rozróżniać 'przeszło guardy' od 'zapisane w falowniku'."""
    result = GuardResult(params={"mode": "general", "eco_soc": 30.0}, status=Status.SUCCESS)

    assert result.executed == []

    result.executed = ["mode"]
    report = result.as_report()
    assert report["executed"] == ["mode"]
    assert set(report["params"]) == {"mode", "eco_soc"}
