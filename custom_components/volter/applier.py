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


async def apply_params(
    hass: HomeAssistant,
    options: dict,
    params: dict[str, Any],
) -> tuple[list[str], list[dict[str, str]]]:
    """Zapisz parametry w jawnej kolejności (`guards.ordered`).

    Kolejność ma znaczenie: w części falowników zmiana trybu resetuje limity,
    więc `mode` musi iść przed limitami. Poprzednia implementacja iterowała po
    słowniku, czyli w kolejności przypadkowej (luka L-6).

    Przy błędzie zapisu ponawiamy `WRITE_RETRIES` razy i dopiero potem raportujemy
    błąd — bez pętli, żeby nie zużywać pamięci nieulotnej falownika (wektor T-14).
    """
    executed: list[str] = []
    errors: list[dict[str, str]] = []

    for param_key, value in ordered(params):
        if param_key == "export_limit_enabled":
            entity_id = options.get(OPT_ENTITY_EXPORT_LIMIT_SWITCH, "")
            if not entity_id:
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
            continue

        opt_key, ha_domain, ha_service, data_key = mapping
        entity_id = options.get(opt_key, "")
        if not entity_id:
            _LOGGER.debug("Param %s: encja niezmapowana, pomijam", param_key)
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

    return executed, errors


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
