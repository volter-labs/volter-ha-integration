"""Executor — jedyny pisarz do falownika. Wykonuje harmonogram i komendy z chmury.

Naprawia luki L-1, L-2, L-3, L-5, L-6 z audytu:
  * każdy zapis przechodzi przez guardy (`guards.apply_guards`) — nic z chmury nie leci
    wprost do falownika,
  * harmonogram jest utrwalony lokalnie (`helpers.storage`) i wykonywany cyklicznie,
    więc utrata łącza nie zostawia falownika na wiecznym setpoincie,
  * po wygaśnięciu planu wchodzimy w jawny fallback, nie w „ostatnią znaną wartość",
  * zapisy są throttlowane (pamięć nieulotna falownika) i mają jawną kolejność.

Odpowiednik w firmware: `components/executor/`. Ta sama maszyna, inna warstwa wykonawcza.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .applier import apply_params
from .const import (
    DEFAULT_RATED_POWER_W,
    DEFAULT_SOC_RESERVE,
    EXECUTOR_INTERVAL,
    MAX_DIRECTION_CHANGES_PER_HOUR,
    MAX_SOC_JUMP_PP,
    MAX_STATE_AGE_S,
    OPT_RATED_POWER_W,
    OPT_SOC_RESERVE,
    OPT_USER_MODE,
    STORAGE_KEY,
    STORAGE_VERSION,
    WRITE_MIN_INTERVAL_S,
)
from .guards import (
    Action,
    DirectionLimiter,
    GuardContext,
    GuardResult,
    InvalidCommand,
    Status,
    UserConfig,
    WriteThrottle,
    apply_guards,
    infer_action,
    sanitize_params,
)
from .ha_state import read_device_state, read_inverter_limits
from .mappers import slot_to_params
from .schedule import Schedule

_LOGGER = logging.getLogger(__name__)


class VolterExecutor:
    """Pętla wykonawcza + wspólna brama zapisu dla harmonogramu i komend."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self._schedule: Schedule | None = None
        self._unsub: CALLBACK_TYPE | None = None
        self._throttle = WriteThrottle(min_interval_s=WRITE_MIN_INTERVAL_S)
        self._direction = DirectionLimiter(max_changes_per_hour=MAX_DIRECTION_CHANGES_PER_HOUR)
        self._prev_soc: float | None = None
        self._last: dict[str, Any] = {}

    # ── cykl życia ───────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        raw = await self._store.async_load()
        if raw:
            try:
                self._schedule = Schedule.from_dict(raw)
                _LOGGER.info(
                    "Wczytano harmonogram z pamięci lokalnej: %s slotów, ważny do %s",
                    len(self._schedule.slots),
                    self._schedule.valid_until,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Nie udało się odczytać zapisanego harmonogramu: %s", err)

        self._unsub = async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=EXECUTOR_INTERVAL)
        )
        _LOGGER.info("Executor uruchomiony (interwał %ss)", EXECUTOR_INTERVAL)

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        _LOGGER.info("Executor zatrzymany")

    # ── harmonogram ──────────────────────────────────────────────────────────

    async def async_set_schedule(self, raw: dict[str, Any]) -> GuardResult:
        """Przyjmij i utrwal harmonogram z chmury, potem wykonaj natychmiast."""
        schedule = Schedule.from_dict(raw)
        self._schedule = schedule
        await self._store.async_save(schedule.to_dict())
        _LOGGER.info(
            "Zapisano harmonogram %s: %s slotów, ważny do %s",
            schedule.schedule_id or "(bez id)",
            len(schedule.slots),
            schedule.valid_until,
        )
        return await self._async_execute_now()

    # ── wspólna brama zapisu ─────────────────────────────────────────────────

    async def async_apply(
        self,
        raw_params: dict[str, Any],
        *,
        price_pln_kwh: float | None = None,
        action: Action | None = None,
        source: str = "cloud",
    ) -> GuardResult:
        """Jedyna droga do falownika: sanityzacja → guardy → throttle → zapis."""
        options = dict(self._entry.options)
        limits = read_inverter_limits(self.hass, options)
        state = read_device_state(self.hass, options, self._prev_soc)
        cfg = UserConfig(
            soc_reserve=float(options.get(OPT_SOC_RESERVE, DEFAULT_SOC_RESERVE)),
            mode=str(options.get(OPT_USER_MODE, "autarky")),
        )

        # I-10 — fail-closed
        try:
            clean = sanitize_params(raw_params, limits)
        except InvalidCommand as err:
            _LOGGER.error("[%s] Komenda odrzucona (%s): %s", source, err.invariant, err.message)
            result = GuardResult(params={}, status=Status.ERROR)
            result.note(err.invariant, err.message)
            self._remember(source, result, [], [])
            return result

        # I-1…I-9
        ctx = GuardContext(
            state=state,
            limits=limits,
            config=cfg,
            price_pln_kwh=price_pln_kwh,
            max_state_age_s=MAX_STATE_AGE_S,
            max_soc_jump_pp=MAX_SOC_JUMP_PP,
        )
        result = apply_guards(clean, ctx)
        self._prev_soc = state.soc if state.soc is not None else self._prev_soc

        for note in result.notes:
            _LOGGER.info("[%s] guard %s: %s", source, note.invariant, note.message)

        if result.rejected:
            _LOGGER.warning("[%s] Zapis wstrzymany, status=%s", source, result.status.value)
            self._remember(source, result, [], [])
            return result

        now_ts = time.monotonic()

        # I-8 — anty-oscylacja
        act = action or infer_action(result.params)
        allowed, note = self._direction.allows(act, now_ts)
        if not allowed and note is not None:
            result.status = Status.THROTTLED
            result.note(note.invariant, note.message)
            _LOGGER.info("[%s] guard %s: %s", source, note.invariant, note.message)
            self._remember(source, result, [], [])
            return result

        # I-6 — throttling zapisów (ochrona pamięci nieulotnej falownika)
        writable, throttle_notes = self._throttle.filter(result.params, now_ts)
        for tn in throttle_notes:
            result.note(tn.invariant, tn.message)
            _LOGGER.debug("[%s] guard %s: %s", source, tn.invariant, tn.message)

        if not writable:
            _LOGGER.debug("[%s] Brak zmian do zapisania", source)
            self._remember(source, result, [], [])
            return result

        executed, errors = await apply_params(self.hass, options, writable)
        # N-4: raport do chmury musi mówić prawdę — `executed` to to, co naprawdę
        # poszło do falownika, nie to, co przeszło guardy (`result.params`).
        result.executed = executed

        self._throttle.commit({k: writable[k] for k in executed if k in writable}, now_ts)
        self._direction.record(act, now_ts)

        if errors and not executed:
            result.status = Status.ERROR
        elif errors:
            result.status = Status.PARTIAL

        self._remember(source, result, executed, errors)
        return result

    # ── pętla ────────────────────────────────────────────────────────────────

    async def _async_tick(self, _now: datetime | None = None) -> None:
        await self._async_execute_now()

    async def _async_execute_now(self) -> GuardResult:
        if self._schedule is None:
            _LOGGER.debug("Brak harmonogramu — nic do wykonania")
            return GuardResult(params={}, status=Status.SUCCESS)

        now = datetime.now(timezone.utc)
        slot, is_fallback = self._schedule.effective_slot(now)
        if is_fallback:
            _LOGGER.info(
                "Plan wygasł (ważny do %s) — wchodzę w fallback: %s, rezerwa %s%% [I-5]",
                self._schedule.valid_until,
                self._schedule.fallback.action.value,
                self._schedule.fallback.soc_reserve,
            )

        options = dict(self._entry.options)
        rated = float(options.get(OPT_RATED_POWER_W, DEFAULT_RATED_POWER_W))
        params = slot_to_params(slot, rated_power_w=rated)

        return await self.async_apply(
            params,
            price_pln_kwh=slot.price_pln_kwh,
            action=slot.action,
            source="fallback" if is_fallback else "schedule",
        )

    # ── diagnostyka ──────────────────────────────────────────────────────────

    def _remember(
        self,
        source: str,
        result: GuardResult,
        executed: list[str],
        errors: list[dict[str, str]],
    ) -> None:
        self._last = {
            "source": source,
            "at": datetime.now(timezone.utc).isoformat(),
            "executed": executed,
            "errors": errors,
            **result.as_report(),
        }

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "schedule_id": self._schedule.schedule_id if self._schedule else None,
            "schedule_valid_until": (
                self._schedule.valid_until.isoformat()
                if self._schedule and self._schedule.valid_until
                else None
            ),
            "slots": len(self._schedule.slots) if self._schedule else 0,
            "last": self._last,
        }


__all__ = ["VolterExecutor"]
