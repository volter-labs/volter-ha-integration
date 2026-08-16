"""Wykonanie nastaw na encjach HA — jedyne miejsce, które faktycznie pisze do falownika.

Wydzielone z `command_handler.py`, żeby ścieżka komend z chmury i ścieżka harmonogramu
używały dokładnie tego samego kodu zapisu (i tych samych guardów przed nim).
W firmware odpowiednikiem tego pliku jest driver Modbus/UDP.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import COMMAND_ENTITY_MAP, OPT_ENTITY_EXPORT_LIMIT_SWITCH, WRITE_RETRIES
from .guards import ordered

_LOGGER = logging.getLogger(__name__)


def _is_forced(param_key: str, forced_params: set[str] | None) -> bool:
    """RR-3: `forced_params is None` znaczy "wołający nie ma o tym wiedzy" (stary
    kontrakt / wywołanie spoza executora) — wtedy zostajemy przy pierwotnej naprawie
    R-3 i traktujemy KAŻDY brak mapowania jako błąd. Executor zawsze przekazuje
    realny (możliwe że pusty) zestaw z `GuardResult.forced_params`, więc na jego
    ścieżce rozstrzyga wyłącznie przynależność do tego zestawu."""
    return forced_params is None or param_key in forced_params


async def apply_params(
    hass: HomeAssistant,
    options: dict,
    params: dict[str, Any],
    forced_params: set[str] | None = None,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
    """Zapisz parametry w jawnej kolejności (`guards.ordered`).

    Kolejność ma znaczenie: w części falowników zmiana trybu resetuje limity,
    więc `mode` musi iść przed limitami. Poprzednia implementacja iterowała po
    słowniku, czyli w kolejności przypadkowej (luka L-6).

    Przy błędzie zapisu ponawiamy `WRITE_RETRIES` razy i dopiero potem raportujemy
    błąd — bez pętli, żeby nie zużywać pamięci nieulotnej falownika (wektor T-14).

    RR-3: `forced_params` (patrz `guards.GuardResult.forced_params`) rozstrzyga, co
    się dzieje, gdy parametr NIE MA zmapowanej encji w opcjach integracji:
      * parametr w `forced_params` (guard bezpieczeństwa go wstawił/wymusił,
        np. I-4 przy cenie <= 0, I-1 podnoszące eco_soc) -> wpis w `errors`. Naprawa
        R-3 dotyczyła dokładnie tego przypadku: ochrona, która nie może się
        zastosować, nie może wyglądać jak sukces.
      * parametr POZA `forced_params` (normalne mapowanie planu — np.
        `export_limit_enabled`, które `mappers.slot_to_params` emituje ZAWSZE,
        niezależnie od tego, czy użytkownik w ogóle zmapował ten opcjonalny
        przełącznik) -> wpis w `notes`, status NIE zmienia się przez to na ERROR.
        Dosłowne traktowanie obu grup identycznie (naprawa R-3 w rundzie 1)
        zamieniało legalną konfigurację (świadomy brak mapowania encji
        opcjonalnej) w trwały ERROR co przebieg pętli (RR-3).
    """
    executed: list[str] = []
    errors: list[dict[str, str]] = []
    notes: list[dict[str, str]] = []

    for param_key, value in ordered(params):
        if param_key == "export_limit_enabled":
            entity_id = options.get(OPT_ENTITY_EXPORT_LIMIT_SWITCH, "")
            if not entity_id:
                if _is_forced(param_key, forced_params):
                    # R-3: cichy `continue` bez wpisu do errors sprawiał, że np. I-4
                    # (eksport przy cenie <= 0) mogło "zniknąć bez śladu" — executor
                    # ustawiał ERROR tylko gdy errors było niepuste, więc brak błędu
                    # + brak zapisu wyglądały jak SUCCESS/PARTIAL, czyli jak
                    # zadziałane zabezpieczenie, mimo że nic nie poszło do falownika.
                    errors.append({
                        "entity": param_key,
                        "error": (
                            "encja przełącznika limitu eksportu niezmapowana w opcjach "
                            "integracji — zabezpieczenie wymuszone przez guard nie mogło "
                            "się zastosować"
                        ),
                    })
                    _LOGGER.warning(
                        "Param %s: encja niezmapowana, zapis WYMUSZONY przez guard "
                        "pominięty [R-3]", param_key,
                    )
                else:
                    # RR-3: to samo pole, ale bez udziału guarda bezpieczeństwa —
                    # użytkownik po prostu nie zmapował opcjonalnego przełącznika.
                    # To jest legalna konfiguracja, więc widoczna nota, nie błąd.
                    notes.append({
                        "entity": param_key,
                        "note": (
                            "encja przełącznika limitu eksportu niezmapowana — parametr "
                            "z normalnego mapowania planu pominięty"
                        ),
                    })
                    _LOGGER.info(
                        "Param %s: encja niezmapowana (opcjonalna), zapis pominięty "
                        "[RR-3]", param_key,
                    )
                continue
            service = "turn_on" if value else "turn_off"
            ok, err = await _call(hass, "switch", service, {"entity_id": entity_id})
            if ok:
                executed.append(param_key)
            else:
                errors.append({"entity": param_key, "error": err})
            continue

        mapping = COMMAND_ENTITY_MAP.get(param_key)
        if not mapping:
            # R-3: parametr bez mapowania w ogóle (dziś nieosiągalne przez sanitize_params,
            # ale defensywnie) — traktuj identycznie jak niezmapowaną encję, nie jako
            # milczący brak zainteresowania. To NIE jest przypadek "opcjonalna encja
            # niezmapowana świadomie" — to nieznany parametr, więc zawsze błąd,
            # niezależnie od `forced_params`.
            errors.append({
                "entity": param_key,
                "error": f"parametr {param_key} nie ma zdefiniowanego mapowania na encję",
            })
            continue

        opt_key, ha_domain, ha_service, data_key = mapping
        entity_id = options.get(opt_key, "")
        if not entity_id:
            if _is_forced(param_key, forced_params):
                # R-3: patrz komentarz wyżej przy export_limit_enabled — ta sama
                # luka, druga gałąź kodu.
                errors.append({
                    "entity": param_key,
                    "error": (
                        f"encja dla parametru {param_key} niezmapowana w opcjach "
                        "integracji — zabezpieczenie wymuszone przez guard nie mogło "
                        "się zastosować"
                    ),
                })
                _LOGGER.warning(
                    "Param %s: encja niezmapowana, zapis WYMUSZONY przez guard "
                    "pominięty [R-3]", param_key,
                )
            else:
                # RR-3: parametr z normalnego mapowania planu (np. `mode`, `eco_soc`
                # z `soc_target` slotu) — użytkownik świadomie nie zmapował tej
                # opcjonalnej encji. Widoczna nota, status bez zmian.
                notes.append({
                    "entity": param_key,
                    "note": f"encja dla parametru {param_key} niezmapowana — zapis pominięty",
                })
                _LOGGER.info(
                    "Param %s: encja niezmapowana (opcjonalna), zapis pominięty "
                    "[RR-3]", param_key,
                )
            continue

        ok, err = await _call(
            hass, ha_domain, ha_service, {"entity_id": entity_id, data_key: value}
        )
        if ok:
            executed.append(param_key)
            _LOGGER.info("Zapisano %s: %s = %s", param_key, entity_id, value)
        else:
            errors.append({"entity": param_key, "error": err})
            _LOGGER.error("Błąd zapisu %s na %s: %s", param_key, entity_id, err)

    return executed, errors, notes


async def _call(
    hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]
) -> tuple[bool, str]:
    last_error = ""
    for attempt in range(1, WRITE_RETRIES + 1):
        try:
            await hass.services.async_call(domain, service, data, blocking=True)
            return True, ""
        except Exception as err:  # noqa: BLE001 — chcemy każdy błąd service call
            last_error = str(err)
            if attempt < WRITE_RETRIES:
                await asyncio.sleep(1.0 * attempt)
    return False, last_error


__all__ = ["apply_params"]
