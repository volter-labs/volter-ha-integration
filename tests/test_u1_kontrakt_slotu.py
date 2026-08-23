"""U-1 / N-8 — kontrakt slotu: kierunek i pułap eksportu przechodzą granicę chmura↔HA.

Ustalenie **U-1 (WYSOKA)** z `docs/analysis/2026-08-16-kontrakt-mocy.md`. Kontrola chmury
zmierzyła na sondzie 48 h: kierunek **14 z 34 slotów z mocą** (41% slotów, 5,266 z 36,808 kWh
AC = 14,3% planowanej energii) jedzie WYŁĄCZNIE w polach `charge_source` / `discharge_purpose`
przy `mode='self_consume'` — bo `device-schedule.ts` świadomie NIE podnosi tych slotów do
`mode='discharge'` (podniesienie włączyłoby guard anty-oscylacyjny I-8 tam, gdzie GoodWe
drenuje baterię natywnie).

`Slot.from_dict` tych pól nie czytało, a `mappers.slot_to_params` mapowało
`Action.SELF_CONSUME → 'auto'` i MIMO TO zapisywało `eco_power` — czyli falownik
dostawał moc bez informacji, w którą stronę. Efektywne pokrycie po stronie urządzenia
spadało z 34/34 do 20/34 godzin aktywnych bateryjnie.

**N-8 / D-5 / A-4** — druga połowa tego samego kontraktu: `export_limit_w`. Mapper przy
`export_allowed=True` WYŁĄCZAŁ ogranicznik eksportu (`export_limit_enabled=False`), czyli
zdejmował limit OSD — a nie ma do tego prawa. Plan niesie realny pułap w watach i to on
ma trafić na encję `number`, przy ograniczniku pozostawionym WŁĄCZONYM.

Kontrakt po stronie chmury: `supabase/functions/energy-optimizer/device-schedule.ts`
(`DeviceSlot.charge_source` / `.discharge_purpose` / `.export_limit_w`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter.executor import VolterExecutor
from tests.conftest import FakeHass

from volter_pure.guards import Action
from volter_pure.mappers import slot_to_params
from volter_pure.schedule import InvalidSchedule, Schedule, Slot

POCZATEK = datetime(2026, 8, 16, 22, 0, tzinfo=timezone.utc)
KONIEC = datetime(2026, 8, 16, 23, 0, tzinfo=timezone.utc)


def _slot(**kwargs) -> Slot:
    """Slot z domyślnymi granicami czasu — testy mówią wyłącznie o polach kontraktu."""
    baza = {"start": POCZATEK, "end": KONIEC, "action": Action.SELF_CONSUME}
    baza.update(kwargs)
    return Slot(**baza)


# ── Część 1: `Slot` czyta nowe pola (schedule.py) ────────────────────────────


def test_slot_niesie_charge_source_i_discharge_purpose():
    """Test wprost z planu (Task 13, Step 1)."""
    slot = Slot.from_dict({
        "from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
        "mode": "charge", "charge_source": "grid",
        "power_w": 3000, "soc_target": 80, "export_allowed": False,
    })

    assert slot.action is Action.CHARGE
    assert slot.charge_source == "grid"
    assert slot.discharge_purpose is None
    assert slot.to_dict()["charge_source"] == "grid"


def test_slot_bez_nowych_pol_dziala_jak_dotad():
    """Wsteczna zgodność: stary harmonogram z `helpers.storage` musi się wczytać.

    To NIE jest przypadek teoretyczny — plan utrwalony przez poprzednią wersję
    integracji jest wczytywany przy pierwszym starcie po aktualizacji HACS.
    """
    slot = Slot.from_dict({
        "from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
        "mode": "discharge", "soc_target": 20,
    })

    assert slot.charge_source is None
    assert slot.discharge_purpose is None
    assert slot.export_limit_w is None


def test_slot_niesie_export_limit_w():
    """N-8/D-5: pułap eksportu w watach, nie tylko boolean „wolno/nie wolno"."""
    slot = Slot.from_dict({
        "from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
        "mode": "self_consume", "export_allowed": True, "export_limit_w": 6000,
    })

    assert slot.export_limit_w == 6000.0
    assert slot.to_dict()["export_limit_w"] == 6000.0


def test_slot_z_kierunkiem_przy_self_consume_czyta_oba_pola():
    """Dokładny kształt 14 spornych slotów z sondy U-1."""
    slot = Slot.from_dict({
        "from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
        "mode": "self_consume", "discharge_purpose": "self", "power_w": 3226.0,
    })

    assert slot.action is Action.SELF_CONSUME
    assert slot.discharge_purpose == "self"
    assert slot.power_w == 3226.0


def test_slot_przezywa_pelny_obieg_przez_store():
    """`to_dict` → `from_dict` musi zachować nowe pola — inaczej restart HA je gubi."""
    oryginal = _slot(
        action=Action.DISCHARGE,
        discharge_purpose="sell",
        power_w=1518.0,
        soc_target=35.0,
        export_allowed=True,
        export_limit_w=8000.0,
    )

    kopia = Slot.from_dict(oryginal.to_dict())

    assert kopia == oryginal


# ── Część 2: S-2 — nowe pola też są fail-closed ──────────────────────────────


@pytest.mark.parametrize("wartosc", ["wiatr", "PV", "", 1, True])
def test_s2_nieznane_charge_source_uniewaznia_plan(wartosc):
    """S-2: nowe pole ma tę samą bramę co stare — nieznana wartość to błąd kształtu.

    Cicha akceptacja byłaby GROŹNIEJSZA niż przy polach liczbowych: `charge_source`
    rozstrzyga, czy wolno pobierać energię z sieci do baterii, więc literówka w chmurze
    kupowałaby prąd, którego użytkownik nie zamawiał.
    """
    with pytest.raises(InvalidSchedule) as err:
        Slot.from_dict({
            "from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
            "mode": "charge", "charge_source": wartosc,
        })

    assert err.value.field == "charge_source"


@pytest.mark.parametrize("wartosc", ["oddaj", "SELF", 3.0, False])
def test_s2_nieznane_discharge_purpose_uniewaznia_plan(wartosc):
    with pytest.raises(InvalidSchedule) as err:
        Slot.from_dict({
            "from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
            "mode": "discharge", "discharge_purpose": wartosc,
        })

    assert err.value.field == "discharge_purpose"


def test_s2_export_limit_w_jako_string_uniewaznia_plan():
    """Dokładnie wektor sondy C, tylko na nowym polu: liczba zserializowana jako string."""
    with pytest.raises(InvalidSchedule) as err:
        Slot.from_dict({
            "from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
            "mode": "self_consume", "export_allowed": True, "export_limit_w": "8000",
        })

    assert err.value.field == "export_limit_w"


def test_s2_ujemny_export_limit_w_uniewaznia_plan():
    """Ujemny pułap eksportu odrzucamy PRZY PARSOWANIU, nie dopiero w I-10.

    S-2 w czystej postaci: `sanitize_params` odrzuciłoby taką wartość (dolna granica
    `PARAM_SPECS['export_limit']` = 0 W), ale dopiero PO utrwaleniu planu w `Store` —
    czyli każdy kolejny tick wywalałby całą komendę, bezterminowo i po restarcie HA.
    """
    with pytest.raises(InvalidSchedule) as err:
        Slot.from_dict({
            "from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
            "mode": "self_consume", "export_allowed": True, "export_limit_w": -100,
        })

    assert err.value.field == "export_limit_w"


def test_s2_zly_slot_z_nowym_polem_uniewaznia_caly_harmonogram():
    """Fail-closed obowiązuje tak samo dla nowych pól: jeden zły slot = brak planu."""
    with pytest.raises(InvalidSchedule) as err:
        Schedule.from_dict({
            "schedule_id": "u1",
            "slots": [
                {"from": "2026-08-16T22:00:00Z", "to": "2026-08-16T23:00:00Z",
                 "mode": "self_consume"},
                {"from": "2026-08-16T23:00:00Z", "to": "2026-08-17T00:00:00Z",
                 "mode": "charge", "charge_source": "sieć"},
            ],
        })

    assert err.value.field == "slots[1].charge_source"


# ── Część 3: U-1 — mapper przekazuje kierunek do falownika ───────────────────


def test_u1_self_consume_z_discharge_purpose_idzie_jako_eco_discharge():
    """SONDA U-1 (odtworzenie): 41% slotów z mocą jedzie tym kształtem.

    Przed naprawą: `mode='auto'` PLUS `charge_limit=32.3` — moc bez komendy kierunku.
    """
    params = slot_to_params(
        _slot(discharge_purpose="self", power_w=3226.0, soc_target=20.0),
        rated_power_w=10000.0,
    )

    assert params["mode"] == "discharge_battery", (
        "kierunek z discharge_purpose musi dotrzeć do falownika — inaczej moc nie ma sensu"
    )
    assert params["charge_limit"] == pytest.approx(3226.0)


def test_u1_self_consume_bez_kierunku_nie_dostaje_mocy():
    """BEZWZGLĘDNY warunek zadania: nie wolno zapisać `eco_power` bez kierunku.

    To jest pierwotna wada U-1 w czystej postaci: `general` + `eco_power` znaczy dla
    falownika „mam nastawę mocy, ale nie wiem, czy ładować, czy rozładowywać".
    """
    params = slot_to_params(_slot(power_w=3226.0, soc_target=20.0), rated_power_w=10000.0)

    assert params["mode"] == "auto"
    assert "charge_limit" not in params


def test_u1_odrzucenie_mocy_jest_glosne_ale_nie_zalewa_logu(caplog, monkeypatch):
    """Moc bez kierunku MUSI zostawić ślad — ale nie 60 WARNING/h (RR-10).

    Ta ścieżka jest osiągalna dla planu utrwalonego w `Store` przed rozszerzeniem
    kontraktu: `mode='self_consume'` + `power_w`, bez pól kierunku. Slot obowiązuje
    godzinę, a pętla wykonawcza chodzi co 60 s — bez deduplikacji to dokładnie ten
    zalew logu, który zamknęło ustalenie RR-10 (WARNING jest w logu HA bardziej
    widoczny niż INFO i stał poza anty-spamem).
    """
    import volter_pure.mappers as mappers_module

    monkeypatch.setattr(mappers_module, "_ostatnie_ostrzezenie_o_mocy", None)
    stary_slot = _slot(power_w=2500.0)

    with caplog.at_level("WARNING", logger=mappers_module._LOGGER.name):
        slot_to_params(stary_slot)
        slot_to_params(stary_slot)
        slot_to_params(_slot(start=KONIEC, end=KONIEC + timedelta(hours=1), power_w=2500.0))

    ostrzezenia = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(ostrzezenia) == 2, (
        "ten sam slot ma ostrzegać RAZ, nowy slot — ponownie; "
        f"padło {len(ostrzezenia)} ostrzeżeń"
    )
    assert "U-1" in ostrzezenia[0].getMessage()


def test_u1_idle_nie_dostaje_mocy():
    """IDLE to świadoma komenda „stój" — moc bez kierunku jest tu równie bezsensowna."""
    params = slot_to_params(_slot(action=Action.IDLE, power_w=1000.0))

    # Etap 3: IDLE ma własny tryb falownika (BATTERY_STANDBY, "PBattery = 0"),
    # a nie neutralne AUTO — "stój" i "pracuj wg licznika" to różne komendy.
    # IDLE emituje planer przy ujemnych cenach, gdy bateria jest pełna.
    assert params["mode"] == "battery_standby"
    assert "charge_limit" not in params


def test_u1_discharge_sell_zostaje_eco_discharge_z_moca():
    """Jednoznaczny tryb `discharge` działa jak dotąd — naprawa jest addytywna."""
    params = slot_to_params(
        _slot(action=Action.DISCHARGE, discharge_purpose="sell", power_w=5000.0),
        rated_power_w=10000.0,
    )

    assert params["mode"] == "discharge_battery"
    assert params["charge_limit"] == pytest.approx(5000.0)


# ── Część 4: `charge_source` — pv NIE WOLNO zamienić na pobór z sieci ────────


def test_u1_charge_z_sieci_idzie_jako_eco_charge_z_moca():
    params = slot_to_params(
        _slot(action=Action.CHARGE, charge_source="grid", power_w=3000.0, soc_target=80.0),
        rated_power_w=10000.0,
    )

    assert params["mode"] == "charge_battery"
    assert params["charge_limit"] == pytest.approx(3000.0)
    # `soc_target` na slocie ładowania znaczy "DO ilu naładować" — czyli GÓRNY próg.
    # Wcześniej szedł w `eco_soc`, czyli w próg DOLNY: dokładnie odwrotnie.
    assert params["soc_upper"] == 80.0


def test_u1_charge_z_pv_nie_wlacza_ladowania_z_sieci():
    """Wymóg 4 zadania: `charge_source='pv'` NIE MOŻE dać trybu ładującego z sieci.

    `eco_charge` w GoodWe realizuje zadaną moc ładowania — brakującą część dobiera
    Z SIECI. Dla slotu `CHARGE_FROM_PV` byłby to zakup prądu, o który planer nie prosił:
    on ma osobny tryb `CHARGE_FROM_GRID` na wypadek, gdy zakup jest zamierzony.
    """
    params = slot_to_params(
        _slot(action=Action.CHARGE, charge_source="pv", power_w=3000.0, soc_target=80.0),
        rated_power_w=10000.0,
    )

    # Etap 3, H-1 WYCOFANA: falownik ma osobny tryb CHARGE_PV — "When set to 0,
    # only PV power is used". Ładowanie nadwyżką da się więc wyrazić WPROST,
    # zamiast uciekać w tryb neutralny i tracić intencję planu.
    assert params["mode"] != "charge_battery", "pv nie może włączyć ładowania z sieci"
    assert params["mode"] == "charge_pv"
    assert params["charge_limit"] == 0.0, (
        "Xset=0 w CHARGE_PV znaczy 'nie dobieraj z sieci' — to jest ta gwarancja"
    )
    # Cel ładowania to GÓRNY próg SoC, nie dolny.
    assert params["soc_upper"] == 80.0


def test_u1_self_consume_z_charge_source_pv_tez_nie_laduje_z_sieci():
    """Ta sama reguła dla slotów kolapsujących do `self_consume` (nadwyżka PV)."""
    params = slot_to_params(_slot(charge_source="pv", power_w=2000.0))

    assert params["mode"] == "charge_pv"
    assert params["charge_limit"] == 0.0


def test_u1_charge_bez_charge_source_zachowuje_stare_zachowanie():
    """Wsteczna zgodność: plan sprzed rozszerzenia kontraktu nie może stracić ładowania."""
    params = slot_to_params(
        _slot(action=Action.CHARGE, power_w=3000.0), rated_power_w=10000.0
    )

    assert params["mode"] == "charge_battery"
    assert params["charge_limit"] == pytest.approx(3000.0)


# ── Część 5: N-8 — pułap eksportu z planu na encję `number` ──────────────────


def test_n8_export_limit_w_ustawia_number_a_nie_zdejmuje_ogranicznika():
    """N-8: mapper nie ma prawa znieść limitu OSD — ma go USTAWIĆ na pułap z planu."""
    params = slot_to_params(_slot(export_allowed=True, export_limit_w=6000.0))

    assert params["export_limit"] == 6000.0
    assert params["export_limit_enabled"] is True, (
        "ogranicznik zostaje WŁĄCZONY — wyłączenie go zdejmuje limit przyłączeniowy OSD"
    )


def test_n8_brak_zgody_na_eksport_dalej_daje_twarde_zero():
    """Zachowanie bez zmian — to jedyna gałąź, w której zero jest intencją planu."""
    params = slot_to_params(_slot(export_allowed=False, export_limit_w=6000.0))

    assert params["export_limit"] == 0.0
    assert params["export_limit_enabled"] is True


def test_n8_zgoda_bez_pulapu_musi_zwolnic_ogranicznik():
    """Reszta N-8, świadomie NIEZAMKNIĘTA: bez pułapu z planu nie znamy limitu OSD.

    Poprzedni slot mógł zostawić `export_limit=0` z włączonym ogranicznikiem, więc
    pozostawienie encji w spokoju blokowałoby eksport bezterminowo. Zwolnienie
    ogranicznika jest tu mniejszym złem, ale zostaje udokumentowaną luką dla Etapu 3.
    """
    params = slot_to_params(_slot(export_allowed=True, export_limit_w=None))

    assert params["export_limit_enabled"] is False
    assert "export_limit" not in params


# ── Część 6: przebieg end-to-end przez executor ──────────────────────────────

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


def _hass(soc: str = "60") -> FakeHass:
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
    hass.states.set("number.export_limit", "0", {"min": 0, "max": 30000})
    return hass


def _zapisy(hass: FakeHass) -> dict[str, object]:
    """Ostatnia wartość, która poszła na każdą encję."""
    out: dict[str, object] = {}
    for _domain, service, data in hass.services.calls:
        klucz = data.get("entity_id", "")
        out[klucz] = data.get("option", data.get("value", service))
    return out


@pytest.mark.asyncio
async def test_u1_diagnoza_pokazuje_nowe_pola_slotu(fake_entry):
    """D-5/A-4: `diagnose` to jedyne okno na to, co urządzenie ZROZUMIAŁO z planu.

    Bez tych trzech pól raport nie odróżnia „slot bez kierunku" od „kierunek przyszedł,
    ale mapper go zgubił" — czyli nie da się nim potwierdzić hipotez H-1/H-2 na żywym
    falowniku w Etapie 3, a po to ten raport istnieje.
    """
    hass = _hass(soc="60")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    await executor.async_set_schedule({
        "schedule_id": "u1-diag",
        "slots": [{
            "from": (teraz - timedelta(minutes=10)).isoformat(),
            "to": (teraz + timedelta(minutes=50)).isoformat(),
            "mode": "self_consume",
            "discharge_purpose": "self",
            "power_w": 3226.0,
            "export_allowed": True,
            "export_limit_w": 6000.0,
        }],
    })

    slot_info = (await executor.async_diagnose())["slot"]

    assert slot_info["discharge_purpose"] == "self"
    assert slot_info["charge_source"] is None
    assert slot_info["export_limit_w"] == 6000.0
    assert slot_info["power_w"] == 3226.0


@pytest.mark.asyncio
async def test_u1_wymuszona_akcja_kasuje_kierunek_opisowy(fake_entry):
    """Znalezione przy naprawie U-1, NIE było w dokumencie: nowe pole obchodzi I-1.

    `executor` po `forced_action` przemapowuje slot przez `replace(slot, action=...)` —
    czyli podmienia TRYB, a `discharge_purpose` zostaje. Po rozszerzeniu kontraktu
    mapper czyta kierunek właśnie z tego pola, więc slot rozładowania zduszony przez
    I-1 (SoC 30% < rezerwa 40%) wracałby do falownika jako `eco_discharge` tylnymi
    drzwiami. Rezerwa backup byłaby fikcją dokładnie tak, jak przed naprawą R-1.
    """
    hass = _hass(soc="30")
    fake_entry.options = dict(OPTIONS) | {"soc_reserve": 40.0}
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    await executor.async_set_schedule({
        "schedule_id": "u1-forced",
        "slots": [{
            "from": (teraz - timedelta(minutes=10)).isoformat(),
            "to": (teraz + timedelta(minutes=50)).isoformat(),
            "mode": "discharge",
            "discharge_purpose": "sell",
            "power_w": 5000.0,
            "soc_target": 50.0,
            "export_allowed": True,
        }],
        "fallback": {"mode": "self_consume", "soc_reserve": 40.0},
    })

    zapisy = _zapisy(hass)
    assert zapisy.get("select.tryb") != "discharge_battery", (
        "I-1: kierunek opisowy nie może wskrzesić rozładowania zduszonego przez guard"
    )
    assert "number.eco_power" not in zapisy, (
        "moc bez kierunku (slot przemapowany na self_consume) nie ma prawa iść do falownika"
    )


@pytest.mark.asyncio
async def test_u1_end_to_end_slot_self_consume_z_kierunkiem_pisze_moc(fake_entry):
    """Pełna ścieżka: JSON z chmury → Store → guardy → service calls falownika.

    Bez naprawy `select.tryb` dostawał `general`, a `number.eco_power` mimo to wartość.
    """
    hass = _hass(soc="60")
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)
    teraz = datetime.now(timezone.utc)

    await executor.async_set_schedule({
        "schedule_id": "u1-e2e",
        "slots": [{
            "from": (teraz - timedelta(minutes=10)).isoformat(),
            "to": (teraz + timedelta(minutes=50)).isoformat(),
            "mode": "self_consume",
            "discharge_purpose": "self",
            "power_w": 3226.0,
            "soc_target": 20.0,
            "price_pln_kwh": 1.6,
            "export_allowed": True,
            "export_limit_w": 6000.0,
        }],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    })

    zapisy = _zapisy(hass)
    assert zapisy.get("select.tryb") == "discharge_battery"
    assert zapisy.get("number.charge_limit") == pytest.approx(3226.0)
    assert zapisy.get("number.export_limit") == 6000.0
    assert zapisy.get("switch.export_limit") == "turn_on"
