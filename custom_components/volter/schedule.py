"""Model harmonogramu EMS — warstwa czysta, bez zależności od Home Assistanta.

Naprawia lukę L-2 z audytu: dotąd sterowanie było czysto reaktywne (komenda przyszła →
wykonana → zapomniana), więc przy utracie łącza falownik zostawał na ostatnim setpoincie
bezterminowo. Harmonogram utrwalony lokalnie realizuje wymóg PME2 „działanie w oparciu
o profile czasowe" po stronie urządzenia, a nie tylko w chmurze.

Kontrakt JSON: `Volter-BOX/03-produkt/guardy-i-inwarianty.md` §5.
Ten sam model implementuje firmware (NVS zamiast helpers.storage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .guards import Action

SCHEDULE_VERSION = 1


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Slot:
    """Pojedynczy przedział harmonogramu."""

    start: datetime
    end: datetime
    action: Action
    power_w: float | None = None
    soc_target: float | None = None
    price_pln_kwh: float | None = None
    export_allowed: bool = True

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Slot":
        return cls(
            start=_parse_dt(raw["from"]),
            end=_parse_dt(raw["to"]),
            action=Action(str(raw.get("mode", "self_consume"))),
            power_w=raw.get("power_w"),
            soc_target=raw.get("soc_target"),
            price_pln_kwh=raw.get("price_pln_kwh"),
            export_allowed=bool(raw.get("export_allowed", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "mode": self.action.value,
            "power_w": self.power_w,
            "soc_target": self.soc_target,
            "price_pln_kwh": self.price_pln_kwh,
            "export_allowed": self.export_allowed,
        }


@dataclass(frozen=True)
class Fallback:
    """Zachowanie po wygaśnięciu planu (I-5).

    Świadomie jest częścią harmonogramu, a nie stałą w kodzie: chmura decyduje,
    co znaczy „bezpiecznie" dla danej instalacji, i nie duplikujemy tej wiedzy
    w dwóch implementacjach (HA i firmware).
    """

    action: Action = Action.SELF_CONSUME
    soc_reserve: float = 20.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Fallback":
        if not raw:
            return cls()
        return cls(
            action=Action(str(raw.get("mode", "self_consume"))),
            soc_reserve=float(raw.get("soc_reserve", 20.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.action.value, "soc_reserve": self.soc_reserve}

    def as_slot(self, moment: datetime, horizon_s: float = 3600.0) -> Slot:
        from datetime import timedelta

        return Slot(
            start=moment,
            end=moment + timedelta(seconds=horizon_s),
            action=self.action,
            soc_target=self.soc_reserve,
            # R-6 (reszta): fallback (brak harmonogramu w ogóle / plan wygasł) nie
            # niesie `price_pln_kwh` i nie ma skąd jej wziąć — integracja nie mapuje
            # żadnej encji cenowej, cena istnieje wyłącznie w slotach z chmury. I-4
            # ("nie eksportuj przy cenie <= 0") jest więc na tej ścieżce ślepe z
            # definicji, nie tylko dziś. Świadomy wybór: skoro nie da się
            # ZWERYFIKOWAĆ, że cena jest dodatnia, fallback musi być zachowawczy i
            # blokować eksport całkowicie, zamiast domyślnie na niego zezwalać.
            export_allowed=False,
        )


@dataclass
class Schedule:
    """Harmonogram 24–48 h. Slots muszą być posortowane rosnąco."""

    schedule_id: str = ""
    generated_at: datetime | None = None
    slots: list[Slot] = field(default_factory=list)
    fallback: Fallback = field(default_factory=Fallback)

    # ── odpytywanie ──────────────────────────────────────────────────────────

    def slot_for(self, moment: datetime) -> Slot | None:
        for slot in self.slots:
            if slot.covers(moment):
                return slot
        return None

    @property
    def valid_until(self) -> datetime | None:
        return max((s.end for s in self.slots), default=None)

    def is_expired(self, moment: datetime) -> bool:
        """True, gdy nie ma już żadnego slotu obejmującego `moment` ani później."""
        end = self.valid_until
        return end is None or moment >= end

    def effective_slot(self, moment: datetime) -> tuple[Slot, bool]:
        """Zwróć slot do wykonania oraz flagę „to jest fallback".

        Kluczowa różnica wobec poprzedniej implementacji: gdy plan się skończył,
        NIE zostawiamy ostatniego setpointu — wchodzimy w jawnie zdefiniowany fallback.
        """
        slot = self.slot_for(moment)
        if slot is not None:
            return slot, False
        return self.fallback.as_slot(moment), True

    # ── serializacja ─────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Schedule":
        slots = [Slot.from_dict(s) for s in raw.get("slots", [])]
        slots.sort(key=lambda s: s.start)
        generated = raw.get("generated_at")
        return cls(
            schedule_id=str(raw.get("schedule_id", "")),
            generated_at=_parse_dt(generated) if generated else None,
            slots=slots,
            fallback=Fallback.from_dict(raw.get("fallback")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SCHEDULE_VERSION,
            "schedule_id": self.schedule_id,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "slots": [s.to_dict() for s in self.slots],
            "fallback": self.fallback.to_dict(),
        }


__all__ = ["Fallback", "Schedule", "Slot", "SCHEDULE_VERSION"]
