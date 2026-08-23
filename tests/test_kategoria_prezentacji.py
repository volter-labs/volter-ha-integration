"""Karta ma nazywać godzinę tak samo, jak nazywa ją aplikacja.

Kontrakt urządzenia ma cztery kierunki, bo tyle wystarcza guardom i falownikowi.
Aplikacja Volter pokazuje osiem kategorii wizualnych. Karta, mając tylko kierunek,
te kategorie ZGADYWAŁA — i myliła się w sposób, który zmieniał wymowę planu:
`self_consume` niosący `discharge_purpose='self'` (bateria pokrywa dom) trafiał
przez `akcja_efektywna` na „Rozładowanie", w tym samym kolorze co sprzedaż.

Plan był ten sam — porównanie godzina po godzinie na danych produkcyjnych dało
jedną rozbieżność trybu na 34 godziny. Rozjeżdżała się WYŁĄCZNIE prezentacja.

`plan_mode` i annotacje sieciowe są tylko do prezentacji: guardy, mapper
i executor dalej czytają `action`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.volter.entity import plan_do_json
from custom_components.volter.guards import Action
from custom_components.volter.schedule import Schedule, Slot

TERAZ = datetime.now(timezone.utc)


def _slot(**over) -> Slot:
    baza = TERAZ.replace(minute=0)
    dane = {
        "start": baza, "end": baza + timedelta(hours=1),
        "action": Action.SELF_CONSUME,
    }
    dane.update(over)
    return Slot(**dane)


def test_from_dict_czyta_pola_opisowe():
    s = Slot.from_dict({
        "from": "2026-08-24T10:00:00+00:00", "to": "2026-08-24T11:00:00+00:00",
        "mode": "self_consume", "discharge_purpose": "self",
        "plan_mode": "SELF_CONSUME", "grid_import_kwh": 1.4, "grid_export_kwh": 0.0,
    })

    assert s.plan_mode == "SELF_CONSUME"
    assert s.grid_import_kwh == 1.4
    assert s.grid_export_kwh == 0.0


def test_plan_sprzed_rozszerzenia_dalej_sie_wczytuje():
    """Plan utrwalony w `Store` przed tą zmianą nie może po aktualizacji zniknąć."""
    s = Slot.from_dict({
        "from": "2026-08-24T10:00:00+00:00", "to": "2026-08-24T11:00:00+00:00",
        "mode": "charge", "charge_source": "grid",
    })

    assert s.plan_mode is None
    assert s.grid_import_kwh is None


def test_zly_typ_pola_opisowego_nie_wywraca_planu():
    """Pole opisowe nie steruje niczym, więc jego zły typ nie może być powodem
    odrzucenia całego planu — inaczej regresja w prezentacji wyłączałaby
    sterowanie."""
    s = Slot.from_dict({
        "from": "2026-08-24T10:00:00+00:00", "to": "2026-08-24T11:00:00+00:00",
        "mode": "idle", "plan_mode": 42, "grid_import_kwh": "duzo",
    })

    assert s.plan_mode is None
    assert s.grid_import_kwh is None


def test_to_dict_przenosi_pola_opisowe_do_store():
    s = _slot(plan_mode="EXPORT_PV", grid_import_kwh=0.0, grid_export_kwh=2.1)

    d = s.to_dict()

    assert d["plan_mode"] == "EXPORT_PV"
    assert d["grid_export_kwh"] == 2.1
    assert Slot.from_dict(d | {"mode": "self_consume"}).plan_mode == "EXPORT_PV"


def test_karta_dostaje_tryb_planu_a_nie_tylko_kierunek():
    plan = Schedule(slots=[_slot(
        action=Action.SELF_CONSUME, discharge_purpose="self",
        plan_mode="SELF_CONSUME", grid_import_kwh=1.4, grid_export_kwh=0.0,
        power_w=1476.0,
    )])

    s = plan_do_json(plan, TERAZ)[0]

    # Kierunek zostaje — na nim stoi wykonanie (U-6).
    assert s["akcja"] == "discharge"
    # ...ale karta dostaje też to, czym ta godzina JEST w planie.
    assert s["tryb_planu"] == "SELF_CONSUME"
    assert s["import_kwh"] == 1.4
    assert s["eksport_kwh"] == 0.0


# --- display_kind: kategoria liczona RAZ w chmurze (Faza C §8) ---------------------
#
# Etykieta z `plan_mode` niosła DECYZJĘ planera, nie przepływ: cztery z ośmiu godzin
# `BATTERY_DISCHARGE_SELL` eksportowały 6–41 Wh. Chmura liczy teraz kategorię
# z przepływów i delty SoC (której karta nie ma) i wysyła ją jako `display_kind`.
# Karta czyta ją wprost; własna reguła zostaje tylko dla planów sprzed tego pola.


def test_from_dict_czyta_display_kind_miekko():
    s = Slot.from_dict({
        "from": "2026-08-24T00:00:00+00:00", "to": "2026-08-24T01:00:00+00:00",
        "mode": "discharge", "discharge_purpose": "sell",
        "plan_mode": "BATTERY_DISCHARGE_SELL", "display_kind": "BATTERY_DISCHARGE_SELF",
    })
    assert s.plan_mode == "BATTERY_DISCHARGE_SELL"
    assert s.display_kind == "BATTERY_DISCHARGE_SELF"
    # Zły typ nie odrzuca planu — to pole nie steruje niczym.
    assert Slot.from_dict({
        "from": "2026-08-24T00:00:00+00:00", "to": "2026-08-24T01:00:00+00:00",
        "mode": "idle", "display_kind": 7,
    }).display_kind is None


def test_display_kind_przezywa_store():
    s = _slot(plan_mode="CHARGE_FROM_PV", display_kind="EXPORT_PV")
    assert Slot.from_dict(s.to_dict()).display_kind == "EXPORT_PV"


def test_karta_dostaje_kategorie_z_chmury():
    plan = Schedule(slots=[_slot(
        action=Action.DISCHARGE, discharge_purpose="sell",
        plan_mode="BATTERY_DISCHARGE_SELL", display_kind="BATTERY_DISCHARGE_SELF",
        grid_import_kwh=0.0, grid_export_kwh=0.008,
    )])
    s = plan_do_json(plan, TERAZ)[0]
    assert s["tryb_planu"] == "BATTERY_DISCHARGE_SELL"
    assert s["kategoria"] == "BATTERY_DISCHARGE_SELF"
