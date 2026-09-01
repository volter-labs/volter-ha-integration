"""Mapowanie intencji planu na konkretne nastawy falownika.

⚠️ **TEN PLIK JEST HIPOTEZĄ DO POTWIERDZENIA W ETAPIE 1 WIRINGU.**

Sterowanie baterią w GoodWe nie jest jednym rejestrem „ładuj 3 kW" — odbywa się przez
tryb pracy plus grupy eco mode. Poniższe mapowanie to najlepsze przypuszczenie na podstawie
encji, które wystawia integracja `mletenay/home-assistant-goodwe-inverter`. Każdą pozycję
trzeba zweryfikować na żywym falowniku i zapisać wynik w
`Volter-BOX/08-sciezka-a/mapa-nastaw-goodwe.md`.

Zamierzone pytania do weryfikacji:
  * jak wymusić ładowanie z sieci o zadanej mocy i do zadanego SoC,
  * jak wymusić rozładowanie / sprzedaż,
  * jak wrócić do czystej autokonsumpcji,
  * czy zmiana trybu resetuje limity (od tego zależy kolejność zapisu, PARAM_ORDER),
  * realne granice wartości (do inwariantu I-3),
  * opóźnienie od zapisu do zmiany zachowania (do TTL i I-8).

⚠️ **DWIE NOWE HIPOTEZY (U-1, Task 13) — do potwierdzenia w Etapie 3:**

  H-1: `eco_charge` realizuje ZADANĄ moc ładowania, dobierając brakującą część Z SIECI.
       Na tym stoi rozróżnienie `charge_source='grid'` (wolno) vs `'pv'` (nie wolno).
       Jeśli okaże się, że eco charge nigdy nie importuje z sieci, dzisiejsze zejście
       do `general` dla `'pv'` jest nadmiarowo ostrożne i można je zdjąć.
  H-2: slot `mode='self_consume'` niosący kierunek w `discharge_purpose`/`charge_source`
       trzeba oddać falownikowi jako `eco_discharge`/`eco_charge`, bo w `general` nastawa
       `eco_power` nie ma adresata. Jeśli falownik ma osobny rejestr mocy dla trybu
       general, to TEN rejestr jest właściwym celem i mapowanie trybu można wycofać.

Docelowo `allowed_modes` czytamy z atrybutu `options` encji select, a nie z tej stałej.
"""

from __future__ import annotations

import logging
from typing import Any

from .guards import Action
from .schedule import Slot, akcja_efektywna, kierunek_slotu

_LOGGER = logging.getLogger(__name__)

#: Nazwy opcji encji select trybu pracy. TODO(Etap-1): potwierdzić dokładne stringi
#: — integracja mletenay może używać innych etykiet lub tłumaczeń.
#: Tryby EMS falownika GoodWe, potwierdzone w `goodwe/inverter.py::EMSMode`
#: (biblioteka, z której korzysta integracja mletenay) — nie zgadywane.
#:
#:   AUTO (1)              "Self-use. PBattery = PInv - Pmeter - Ppv"
#:                         bateria sterowana licznikiem = czysta autokonsumpcja
#:   CHARGE_BATTERY (11)   "Xset is the charging power of the battery. PV preferred;
#:                          when PV insufficient, it will buy power from the grid"
#:   DISCHARGE_BATTERY (12) "Xset is the discharge power of the battery,
#:                          battery discharge has priority"
#:   BATTERY_STANDBY (8)   "PBattery = 0. The battery does not charge and discharge"
#:
#: `Xset` to nastawa `ems_power_limit` (W) — i jest to moc PO STRONIE BATERII,
#: dokładnie ta wielkość, którą niesie `slot.power_w` (z `soc_profile.delta_kwh`).
#: Dlatego nie ma tu żadnego przeliczania na procenty mocy znamionowej.
#:
#: `discharge_purpose` (self vs sell) NIE potrzebuje osobnego trybu: oba to
#: DISCHARGE_BATTERY, a różnicę robi limit eksportu, który kontrakt już niesie.
GOODWE_MODE_MAP: dict[Action, str] = {
    Action.CHARGE: "charge_battery",
    Action.DISCHARGE: "discharge_battery",
    Action.SELF_CONSUME: "auto",
    Action.IDLE: "battery_standby",
}

#: Ładowanie WYŁĄCZNIE z PV. CHARGE_PV (2): "Xmax is to allow the power to be taken
#: from the grid... When set to 0, only PV power is used". Czyli tryb + moc 0.
#: Świadomie nie CONSERVE (6), mimo że też ładuje tylko z PV — CONSERVE blokuje
#: rozładowanie poza pracą wyspową, co jest skutkiem ubocznym, którego plan nie zamawiał.
GOODWE_TRYB_LADOWANIE_PV = "charge_pv"

#: RR-10: tożsamość ostatniego odrzucenia mocy (U-1), żeby ostrzeżenie padło RAZ na
#: slot, a nie na każdy tick pętli wykonawczej. Slot obowiązuje godzinę, pętla chodzi
#: co 60 s — bez tego jeden stary plan z `Store` dawałby 60 WARNING/h, czyli dokładnie
#: ten zalew logu, który zamknęło ustalenie RR-10. Zmienna modułowa (a nie pole obiektu),
#: bo mapper jest funkcją czystą i ma zostać przepisywalny 1:1 na `static` w firmware.
_ostatnie_ostrzezenie_o_mocy: tuple | None = None

#: RR-10, druga ścieżka: ładowanie bez znanej mocy (staging bez `delta_kwh`).
_ostatnie_ostrzezenie_o_braku_mocy: tuple | None = None

#: Moc znamionowa falownika w W — potrzebna, bo eco_power jest w procentach.
#: TODO(Etap-1): wziąć z konfiguracji instalacji, nie ze stałej.
DEFAULT_RATED_POWER_W = 10000.0


def _tryb_falownika(slot: Slot, modes: dict[Action, str]) -> str:
    """Tryb falownika = TŁUMACZENIE akcji efektywnej na nazwę opcji encji select.

    U-6: to jest cała rola mappera — przekład, nie decyzja. Skoro guardy pracują na
    `schedule.akcja_efektywna(slot)`, to tryb wysłany na falownik musi być funkcją
    DOKŁADNIE tej samej wartości; własna gałąź decyzyjna w mapperze mogłaby się z nią
    rozjechać, a rozjazd tych dwóch miejsc jest dosłowną treścią U-6 (guard chroni
    jedną intencję, falownik wykonuje inną).

    Brak jednoznacznego kierunku daje akcję neutralną (`SELF_CONSUME`, a dla jawnego
    „stój" `IDLE`) — dotyczy to także slotu `charge` z PV: bez zgody na pobór z sieci
    nie ma komendy ładowania (patrz `schedule.kierunek_slotu`).

    Tryb bierzemy zawsze z `modes`, nigdy z literału — `mode_map` jest parametrem
    właśnie po to, żeby dało się podmienić nazwy opcji encji select po Etapie 3.
    """
    return modes[akcja_efektywna(slot)]


def _moc_dla_kierunku(
    slot: Slot, tryb: str, kierunek: Action | None, rated_power_w: float
) -> float | None:
    """Nastawa `eco_power` (%) albo `None`, gdy mocy nie wolno zapisać.

    BEZWZGLĘDNA reguła U-1: `eco_power` idzie WYŁĄCZNIE razem z jednoznaczną komendą
    kierunku. Pierwotna wada wyglądała dokładnie tak: `mode='general'` PLUS `eco_power`
    — falownik dostawał moc bez informacji, czy ładować, czy rozładowywać, więc liczba
    z planera nie znaczyła nic, a wyglądała jak wykonany plan.

    Odrzucenie mocy jest GŁOŚNE (WARNING), bo moc przy trybie bez kierunku znaczy, że
    chmura i urządzenie inaczej rozumieją kontrakt — cisza zostawiłaby to bez śladu.
    RR-10: głośne, ale RAZ na slot — powtórka schodzi do DEBUG, bo ta sama przyczyna
    powtarzana co 60 s przestaje być informacją, a staje się szumem.
    """
    global _ostatnie_ostrzezenie_o_mocy

    if slot.power_w is None or rated_power_w <= 0:
        return None
    if kierunek is None:
        komunikat = (
            "Slot %s–%s niesie power_w=%s, ale tryb %r nie ma jednoznacznego kierunku "
            "(charge_source=%r, discharge_purpose=%r) — moc POMINIĘTA [U-1]"
        )
        argumenty = (
            slot.start.isoformat(), slot.end.isoformat(), slot.power_w, tryb,
            slot.charge_source, slot.discharge_purpose,
        )
        klucz = (slot.start, slot.end, tryb, slot.power_w)
        if klucz == _ostatnie_ostrzezenie_o_mocy:
            _LOGGER.debug(komunikat, *argumenty)
        else:
            _ostatnie_ostrzezenie_o_mocy = klucz
            _LOGGER.warning(komunikat, *argumenty)
        return None
    # `Xset` jest w watach po stronie baterii — tej samej, w której planer liczy
    # `delta_kwh`. Żadnego przeliczania na procenty: encja `ems_power_limit`
    # przyjmuje waty, a `eco_mode_power` (procenty) jest w integracji mletenay
    # TYLKO DO ODCZYTU, więc nigdy nie była właściwym celem.
    watty = max(0.0, float(slot.power_w))
    if rated_power_w > 0:
        # Nie zamawiaj więcej, niż falownik potrafi. Twardą granicę i tak nałoży
        # I-3 z atrybutów encji (R-8); to jest tania sanityzacja przed guardami.
        watty = min(watty, float(rated_power_w))
    return round(watty, 0)


def slot_to_params(
    slot: Slot,
    *,
    rated_power_w: float = DEFAULT_RATED_POWER_W,
    mode_map: dict[Action, str] | None = None,
) -> dict[str, Any]:
    """Przetłumacz slot harmonogramu na nastawy encji HA.

    Zwraca surowe parametry — muszą jeszcze przejść przez `guards.sanitize_params`
    i `guards.apply_guards`, dokładnie tak jak komenda z chmury. Mapper nie jest
    zwolniony z guardów.
    """
    modes = mode_map or GOODWE_MODE_MAP
    # U-6: kierunek bierzemy z `schedule.kierunek_slotu` — JEDNEGO źródła prawdy,
    # z którego korzysta też executor PRZED guardami. Własne wyprowadzanie kierunku
    # w mapperze (za guardami) było dokładnie przyczyną U-6 i U-7: I-1 i I-8 nie
    # miały jak zobaczyć kierunku opisowego, bo powstawał on dopiero po nich.
    kierunek = kierunek_slotu(slot)
    tryb = _tryb_falownika(slot, modes)
    # Ładowanie wyłącznie z PV ma własny tryb: CHARGE_BATTERY dobrałby brakującą
    # część z sieci, czego plan przy `charge_source='pv'` wprost nie chce.
    tylko_pv = kierunek is Action.CHARGE and slot.charge_source == "pv"
    if tylko_pv:
        tryb = GOODWE_TRYB_LADOWANIE_PV
    # Rozładowanie NA WŁASNE POTRZEBY zostaje w trybie neutralnym.
    #
    # W `auto` falownik nie pobiera z sieci, jeśli nie musi: pokrywa dom z PV,
    # potem z baterii. Czyli `auto` JUŻ realizuje „rozładuj na własne potrzeby",
    # i robi to lepiej niż nastawa, bo śledzi rzeczywisty pobór. Wymuszenie
    # `discharge_battery` z mocą planu psuje to w obie strony: przy większym
    # poborze falownik dobiera brakującą część Z SIECI mimo pełnej baterii,
    # przy mniejszym wypycha nadmiar do sieci jako eksport, którego plan nie
    # zamawiał. Dodatkowo `power_w` takiego slotu to średnia godzinowa
    # z `delta_kwh`, a nie chwilowy pobór — nastawa nie ma jak trafić w profil
    # obciążenia. Kontrolę nad podłogą zachowuje `eco_soc`, który działa też
    # w `auto` (niżej: przy DISCHARGE `soc_target` idzie właśnie w `eco_soc`).
    #
    # Zgodne z ustaleniem chmury (BATTERY_DISCHARGE_SELF scalone z SELF_CONSUME
    # — GoodWe robi bateria→dom natywnie) i z firmware Boxa (`vb_map_slot`).
    #
    # I-1 zostaje nienaruszone: rezerwę podnosi niezależnie od akcji, a `kierunek`
    # celowo NIE zmieniamy — przejście `charge_*` → `auto` jest realnym
    # przełączeniem trybu i ma zużywać budżet anty-oscylacyjny I-8.
    # WYŁĄCZNIE jawny `self`. Brak celu przy jednoznacznym `mode='discharge'` to
    # nie to samo co „na własne potrzeby": to stary albo niepełny kontrakt, a plan
    # wprost prosi o rozładowanie. Zejście na tryb neutralny gubiłoby tam komendę.
    samo_zuzycie = kierunek is Action.DISCHARGE and slot.discharge_purpose == "self"
    if samo_zuzycie:
        tryb = modes[Action.SELF_CONSUME]
    params: dict[str, Any] = {"mode": tryb}

    if slot.soc_target is not None:
        # `soc_target` znaczy co innego w zależności od kierunku:
        #   ładowanie   -> DO ilu naładować  = GÓRNY próg (`soc_upper`)
        #   pozostałe   -> DO ilu rozładować = DOLNY próg (`eco_soc` -> DoD)
        # Wcześniej szło zawsze w `eco_soc`, więc cel ładowania trafiał na próg
        # dolny — czyli dokładnie odwrotnie, niż chciał plan.
        if kierunek is Action.CHARGE:
            params["soc_upper"] = float(slot.soc_target)
        else:
            # Zapisujemy niezależnie od trybu: to nastawa PAMIĘTANA przez falownik,
            # a I-1 (rezerwa) wymaga, żeby bezpieczna wartość FAKTYCZNIE tam trafiła (RR-8).
            params["eco_soc"] = float(slot.soc_target)

    if samo_zuzycie:
        # Świadomie BEZ nastawy mocy — patrz uzasadnienie przy `samo_zuzycie`.
        pass
    elif tylko_pv:
        # CHARGE_PV: `Xset` to moc, jaką wolno DOBRAĆ Z SIECI, a nie moc ładowania.
        # Zero znaczy "tylko PV" — jedyna wartość, która realizuje intencję planu.
        # Mocy z planu świadomie nie przepisujemy: nadwyżki PV nie da się wymusić.
        params["charge_limit"] = 0.0
    else:
        moc = _moc_dla_kierunku(slot, tryb, kierunek, rated_power_w)
        if moc is not None:
            params["charge_limit"] = moc
        elif kierunek is not None:
            # PUŁAPKA: `charge_battery` bez świeżego `Xset` pracowałby na wartości
            # z POPRZEDNIEGO slotu — a po slocie PV jest tam 0, czyli "ładuj 0 W".
            # Throttle I-6 nie pomoże, bo pomija wartość niezmienioną. Skoro planer
            # nie podał mocy (staging nie zapisuje jeszcze `delta_kwh`), nie zgadujemy
            # jej: wymuszone ładowanie z sieci nieznaną mocą kosztuje realne pieniądze.
            # Neutralny tryb nic nie kosztuje, a plan i tak wróci z mocą po porcie.
            # RR-10: głośno, ale RAZ na slot. Slot trwa godzinę, a pętla chodzi
            # co 60 s — bez klucza tożsamości ta sama przyczyna dawałaby
            # 60 WARNING/h, czyli szum zamiast informacji.
            global _ostatnie_ostrzezenie_o_braku_mocy
            # Ta sama pułapka dotyczy SPRZEDAŻY: `discharge_battery` bez świeżego
            # `Xset` opróżniałby baterię mocą z poprzedniego slotu (po slocie
            # ładowania bywa tam kilka kW), a throttle I-6 tego nie złapie, bo
            # pomija wartość NIEZMIENIONĄ.
            komunikat_bm = (
                "Slot %s–%s to %s bez znanej mocy — zamiast %r wysyłam tryb "
                "neutralny %r, żeby nie sterować nastawą z poprzedniego slotu"
            )
            argumenty_bm = (
                slot.start.isoformat(), slot.end.isoformat(),
                "ładowanie" if kierunek is Action.CHARGE else "rozładowanie",
                tryb, modes[Action.SELF_CONSUME],
            )
            klucz_bm = (slot.start, slot.end, tryb)
            if klucz_bm == _ostatnie_ostrzezenie_o_braku_mocy:
                _LOGGER.debug(komunikat_bm, *argumenty_bm)
            else:
                _ostatnie_ostrzezenie_o_braku_mocy = klucz_bm
                _LOGGER.warning(komunikat_bm, *argumenty_bm)
            params["mode"] = modes[Action.SELF_CONSUME]

    # Eksport (N-8 / D-5 / A-4):
    #   * brak zgody w planie -> twardy limit 0 z włączonym ogranicznikiem;
    #   * zgoda Z PUŁAPEM -> pułap na encję `number`, ogranicznik ZOSTAJE włączony.
    #     Wyłączanie go było wprost naruszeniem N-8: mapper zdejmował limit
    #     przyłączeniowy OSD, którego nie ma prawa dotykać, i robił to za każdym razem,
    #     gdy plan pozwalał eksportować;
    #   * zgoda BEZ pułapu (stary plan, albo planer nie policzył) -> jedyne wyjście to
    #     zwolnić ogranicznik, bo poprzedni slot mógł zostawić na encji twarde 0 W
    #     i eksport byłby zablokowany bezterminowo. RESZTA N-8 zostaje tu świadomie
    #     otwarta: limitu OSD nie znamy, więc nie mamy czym go odtworzyć.
    if not slot.export_allowed:
        params["export_limit"] = 0.0
        params["export_limit_enabled"] = True
    elif slot.export_limit_w is not None:
        params["export_limit"] = float(slot.export_limit_w)
        params["export_limit_enabled"] = True
    else:
        params["export_limit_enabled"] = False

    return params


__all__ = ["DEFAULT_RATED_POWER_W", "GOODWE_MODE_MAP", "slot_to_params"]
