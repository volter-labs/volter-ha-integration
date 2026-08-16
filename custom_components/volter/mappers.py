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

Docelowo `allowed_modes` czytamy z atrybutu `options` encji select, a nie z tej stałej.
"""

from __future__ import annotations

from typing import Any

from .guards import Action
from .schedule import Slot

#: Nazwy opcji encji select trybu pracy. TODO(Etap-1): potwierdzić dokładne stringi
#: — integracja mletenay może używać innych etykiet lub tłumaczeń.
GOODWE_MODE_MAP: dict[Action, str] = {
    Action.CHARGE: "eco_charge",
    Action.DISCHARGE: "eco_discharge",
    Action.SELF_CONSUME: "general",
    Action.IDLE: "general",
}

#: Moc znamionowa falownika w W — potrzebna, bo eco_power jest w procentach.
#: TODO(Etap-1): wziąć z konfiguracji instalacji, nie ze stałej.
DEFAULT_RATED_POWER_W = 10000.0


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
    params: dict[str, Any] = {"mode": modes[slot.action]}

    if slot.soc_target is not None:
        params["eco_soc"] = float(slot.soc_target)

    if slot.power_w is not None and rated_power_w > 0:
        percent = max(0.0, min(100.0, (float(slot.power_w) / rated_power_w) * 100.0))
        params["eco_power"] = round(percent, 1)

    # Eksport: brak zgody w planie -> twardy limit 0 z włączonym ogranicznikiem.
    # Zgoda -> wyłączamy ogranicznik, ale nie ustawiamy limitu na maksimum,
    # żeby nie nadpisywać ustawienia użytkownika/OSD.
    if not slot.export_allowed:
        params["export_limit"] = 0.0
        params["export_limit_enabled"] = True
    else:
        params["export_limit_enabled"] = False

    return params


__all__ = ["DEFAULT_RATED_POWER_W", "GOODWE_MODE_MAP", "slot_to_params"]
