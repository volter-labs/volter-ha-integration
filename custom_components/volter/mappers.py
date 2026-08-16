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
GOODWE_MODE_MAP: dict[Action, str] = {
    Action.CHARGE: "eco_charge",
    Action.DISCHARGE: "eco_discharge",
    Action.SELF_CONSUME: "general",
    Action.IDLE: "general",
}

#: RR-10: tożsamość ostatniego odrzucenia mocy (U-1), żeby ostrzeżenie padło RAZ na
#: slot, a nie na każdy tick pętli wykonawczej. Slot obowiązuje godzinę, pętla chodzi
#: co 60 s — bez tego jeden stary plan z `Store` dawałby 60 WARNING/h, czyli dokładnie
#: ten zalew logu, który zamknęło ustalenie RR-10. Zmienna modułowa (a nie pole obiektu),
#: bo mapper jest funkcją czystą i ma zostać przepisywalny 1:1 na `static` w firmware.
_ostatnie_ostrzezenie_o_mocy: tuple | None = None

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
    percent = max(0.0, min(100.0, (float(slot.power_w) / rated_power_w) * 100.0))
    return round(percent, 1)


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
    params: dict[str, Any] = {"mode": tryb}

    if slot.soc_target is not None:
        # `eco_soc` zapisujemy niezależnie od trybu: to nastawa PAMIĘTANA przez falownik,
        # a I-1 (rezerwa) wymaga, żeby bezpieczna wartość FAKTYCZNIE tam trafiła (RR-8).
        params["eco_soc"] = float(slot.soc_target)

    moc = _moc_dla_kierunku(slot, tryb, kierunek, rated_power_w)
    if moc is not None:
        params["eco_power"] = moc

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
