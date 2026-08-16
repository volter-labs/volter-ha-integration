"""Model harmonogramu EMS — warstwa czysta, bez zależności od Home Assistanta.

Naprawia lukę L-2 z audytu: dotąd sterowanie było czysto reaktywne (komenda przyszła →
wykonana → zapomniana), więc przy utracie łącza falownik zostawał na ostatnim setpoincie
bezterminowo. Harmonogram utrwalony lokalnie realizuje wymóg PME2 „działanie w oparciu
o profile czasowe" po stronie urządzenia, a nie tylko w chmurze.

Kontrakt JSON: `Volter-BOX/03-produkt/guardy-i-inwarianty.md` §5.
Ten sam model implementuje firmware (NVS zamiast helpers.storage).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .guards import Action

SCHEDULE_VERSION = 1


class InvalidSchedule(ValueError):
    """S-2: harmonogram odrzucony W CAŁOŚCI, bo parsowanie musi być fail-closed.

    Dokładny odpowiednik `guards.InvalidCommand` dla komend, tylko dla planu: plan
    z chmury to też niezaufane wejście, a częściowo przyjęty harmonogram jest w
    energetyce groźniejszy niż odrzucony. Dziedziczy po `ValueError`, żeby istniejąca
    bariera w `command_handler` (`except Exception`) raportowała go do chmury jako
    błąd komendy, a nie wywalała pętlę Realtime.
    """

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name
        self.message = message


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── S-2: walidacja typów pól planu (fail-closed) ─────────────────────────────
#
# S-2: `Slot.from_dict` przepisywało `power_w`/`soc_target`/`price_pln_kwh` wprost
# z JSON-a, bez żadnej kontroli typu — klasyczny błąd serializacji po stronie chmury
# (liczba jako string) przechodził przez parsowanie, był UTRWALANY w `Store`, a wybuchał
# dopiero w guardach. Efekt: po restarcie HA każdy tick rzucał wyjątkiem PRZED decyzją,
# więc urządzenie nie pisało nic i nie wchodziło nawet w fallback I-5 — awaria cicha,
# trwała i przeżywająca restart. Dlatego parsowanie jest teraz bramą, a nie przepisaniem.


def _wymagaj_dict(raw: Any, gdzie: str) -> dict[str, Any]:
    """S-2: dowolny JSON z chmury może nie być obiektem — sprawdzamy, zanim użyjemy `.get`."""
    if not isinstance(raw, dict):
        raise InvalidSchedule(gdzie, f"{gdzie} musi być obiektem, jest {type(raw).__name__}")
    return raw


def _liczba(raw: dict[str, Any], key: str) -> float | None:
    """S-2: pole liczbowe musi BYĆ liczbą — brak/`None` znaczy „nie podano".

    `bool` odrzucamy jawnie, bo w Pythonie jest podtypem `int` i `True` przeszłoby
    jako `1.0` — dokładnie ta sama klasa cichej podmiany intencji co `bool("false")`.
    """
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSchedule(key, f"{key} musi być liczbą, jest {value!r}")
    try:
        numeric = float(value)
    except OverflowError as err:
        # JSON nie ogranicza precyzji liczb całkowitych — `float(10**400)` rzuca
        # wyjątek spoza kontraktu parsera, a fail-closed ma tu ODRZUCIĆ plan.
        raise InvalidSchedule(key, f"{key}={value!r} nie mieści się w liczbie") from err
    if not math.isfinite(numeric):
        # `json.loads` przyjmuje literały NaN/Infinity, a NaN przechodzi każde
        # porównanie zakresu i szedłby prosto na encję falownika (ten sam wektor co RR-2).
        raise InvalidSchedule(key, f"{key}={value!r} nie jest liczbą skończoną (NaN/inf)")
    return numeric


def _flaga(raw: dict[str, Any], key: str, domyslna: bool) -> bool:
    """S-2: pole logiczne musi BYĆ bool — `bool("false")` to `True`.

    To groźniejszy wariant braku walidacji niż zły typ liczby: nie rzuca wyjątkiem,
    tylko po cichu ODWRACA intencję planu (zakaz eksportu staje się zgodą).
    """
    value = raw.get(key)
    if value is None:
        return domyslna
    if not isinstance(value, bool):
        raise InvalidSchedule(
            key, f"{key} musi być wartością logiczną (true/false), jest {value!r}"
        )
    return value


def _wybor(raw: dict[str, Any], key: str, dozwolone: tuple[str, ...]) -> str | None:
    """S-2 dla nowych pól opisowych U-1: wartość musi być ze znanego zbioru albo jej nie ma.

    DLACZEGO ta sama surowość co przy `mode`, mimo że pole jest „tylko opisowe":
    `charge_source` rozstrzyga, czy wolno pobierać energię Z SIECI do baterii, a
    `discharge_purpose` — czy slot w ogóle jest rozładowaniem. Literówka w chmurze
    („PV" zamiast „pv") przy cichej akceptacji zniknęłaby bez śladu i zabrała ze sobą
    kierunek, czyli dokładnie to, czego brak jest treścią U-1. `bool` odrzucamy razem
    z resztą nie-stringów (ta sama klasa cichej podmiany intencji co w `_liczba`).
    """
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value not in dozwolone:
        raise InvalidSchedule(
            key, f"{key}={value!r} nie jest jedną z wartości {list(dozwolone)}"
        )
    return value


def _moc_nieujemna(raw: dict[str, Any], key: str) -> float | None:
    """S-2: pułap w watach nie może być ujemny — i musi paść PRZY PARSOWANIU.

    Bez tego wartość ujemna przechodziłaby przez parser do `Store`, a odrzucał ją
    dopiero `sanitize_params` (dolna granica `PARAM_SPECS`) — czyli JUŻ PO utrwaleniu:
    każdy kolejny tick wywalałby całą komendę (I-10 jest fail-closed), bezterminowo
    i po restarcie HA. To jest dosłownie tryb awarii z S-2, tylko na nowym polu.
    """
    numeric = _liczba(raw, key)
    if numeric is not None and numeric < 0:
        raise InvalidSchedule(key, f"{key}={numeric} nie może być ujemne")
    return numeric


def _akcja(raw: dict[str, Any], key: str = "mode") -> Action:
    """S-2: nazwa akcji musi być znanym stringiem — nieznana unieważnia plan."""
    value = raw.get(key, Action.SELF_CONSUME.value)
    if not isinstance(value, str) or not value:
        raise InvalidSchedule(key, f"{key} musi być niepustym stringiem, jest {value!r}")
    try:
        return Action(value)
    except ValueError as err:
        znane = [a.value for a in Action]
        raise InvalidSchedule(key, f"{key}={value!r} nie jest znaną akcją: {znane}") from err


def _znacznik(raw: dict[str, Any], key: str) -> datetime:
    """S-2: granica slotu bez poprawnego znacznika czasu to plan bez sensu czasowego."""
    value = raw.get(key)
    if value is None:
        raise InvalidSchedule(key, f"slot bez pola {key!r}")
    try:
        return _parse_dt(value)
    except (TypeError, ValueError) as err:
        raise InvalidSchedule(
            key, f"{key}={value!r} nie jest poprawnym znacznikiem czasu ISO-8601"
        ) from err


#: U-1: dopuszczalne wartości pól opisowych kierunku. Kontrakt chmury:
#: `supabase/functions/energy-optimizer/device-schedule.ts` (`DeviceSlot`).
CHARGE_SOURCES: tuple[str, ...] = ("pv", "grid")
DISCHARGE_PURPOSES: tuple[str, ...] = ("self", "sell")


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
    #: U-1: skąd ładujemy — 'pv' | 'grid' | None. Kierunek NIEZBĘDNY, a nie ozdobny:
    #: chmura świadomie zostawia `mode='self_consume'` dla slotów, w których falownik
    #: fizycznie nigdzie się nie przełącza (żeby nie budzić I-8), więc dla 41% slotów
    #: z mocą to JEDYNE miejsce, gdzie stoi kierunek. Rozróżnienie pv/grid jest
    #: krytyczne: 'grid' to zgoda na zakup prądu do baterii, 'pv' — jej brak.
    charge_source: str | None = None
    #: U-1: po co rozładowujemy — 'self' | 'sell' | None. Druga połowa tego samego
    #: kierunku (patrz `charge_source`).
    discharge_purpose: str | None = None
    #: N-8/D-5: realny pułap eksportu w watach. Bez niego mapper znał tylko „wolno /
    #: nie wolno" i przy zgodzie ZDEJMOWAŁ ogranicznik, czyli kasował limit OSD.
    export_limit_w: float | None = None

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Slot":
        """S-2: parsowanie jest BRAMĄ, nie przepisaniem JSON-a do dataklasy.

        Każde pole przechodzi przez kontrolę typu, bo zły typ przepuszczony tutaj
        trafiał do `Store` i wybuchał dopiero w guardach — już po utrwaleniu, czyli
        w miejscu, z którego nie ma powrotu bez ingerencji w pliki HA.
        """
        _wymagaj_dict(raw, "slot")
        start = _znacznik(raw, "from")
        end = _znacznik(raw, "to")
        if end <= start:
            # Slot pusty lub odwrócony nigdy nie zostanie wybrany przez `covers()`,
            # więc bez tej kontroli plan wyglądałby na przyjęty, a w praktyce
            # oznaczał ciszę w tym oknie czasowym.
            raise InvalidSchedule(
                "to", f"slot kończy się ({end.isoformat()}) nie później niż zaczyna "
                f"({start.isoformat()})"
            )
        return cls(
            start=start,
            end=end,
            action=_akcja(raw),
            power_w=_liczba(raw, "power_w"),
            soc_target=_liczba(raw, "soc_target"),
            price_pln_kwh=_liczba(raw, "price_pln_kwh"),
            export_allowed=_flaga(raw, "export_allowed", True),
            # U-1: pola opcjonalne — plan sprzed rozszerzenia kontraktu (już utrwalony
            # w `Store`) musi się dalej wczytywać, inaczej aktualizacja integracji
            # skasowałaby aktywny harmonogram przy pierwszym starcie.
            charge_source=_wybor(raw, "charge_source", CHARGE_SOURCES),
            discharge_purpose=_wybor(raw, "discharge_purpose", DISCHARGE_PURPOSES),
            export_limit_w=_moc_nieujemna(raw, "export_limit_w"),
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
            # U-1: nowe pola MUSZĄ iść do `Store` — bez nich restart HA gubiłby kierunek
            # dokładnie tych slotów, które go potrzebują (`mode` mówi wtedy „self_consume").
            "charge_source": self.charge_source,
            "discharge_purpose": self.discharge_purpose,
            "export_limit_w": self.export_limit_w,
        }


def kierunek_slotu(slot: Slot) -> Action | None:
    """Jednoznaczny kierunek przepływu baterii dla slotu albo `None` (U-1/U-6).

    U-6: ta funkcja stoi TUTAJ, a nie w `mappers.py`, bo kierunek musi być znany
    PRZED guardami. Dopóki wyprowadzał go wyłącznie mapper (za guardami), executor
    podawał do `GuardContext` `action=slot.action` — czyli dla 41% slotów z mocą
    zawsze `SELF_CONSUME` — i I-1 (rezerwa backup) oraz I-8 (anty-oscylacja) były
    ślepe na kierunek opisowy. Mapper ma TŁUMACZYĆ intencję na nastawy, a nie ją
    wyprowadzać; warstwa nad nim rozstrzyga, co slot naprawdę znaczy.
    Moduł jest wolny od importów HA, więc funkcja przepisuje się na C razem
    z resztą `schedule.py`/`guards.py`.

    Chmura świadomie NIE podnosi do `mode='discharge'` slotów, w których falownik
    fizycznie nigdzie się nie przełącza (GoodWe drenuje baterię natywnie). Kierunek
    jedzie wtedy WYŁĄCZNIE w polach `discharge_purpose` / `charge_source` — zmierzone
    41% slotów z mocą.

    U-7: pierwotnym motywem chmury było też „nie budzić I-8". Od Tasku 13 (hipoteza
    H-2) mapper oddaje takie sloty falownikowi jako `eco_discharge`/`eco_charge`, więc
    przełączenie trybu JEST fizyczne — i musi zużywać budżet anty-oscylacyjny tak samo
    jak jawne `mode='discharge'`. Inaczej naprzemienne plany przerzucały falownik bez
    żadnego ograniczenia (zmierzone: 12 przełączeń w 24 minuty).

    Rozstrzygnięcie `charge_source='pv'` → BRAK kierunku (wymóg 4 zadania U-1):
    `eco_charge` realizuje zadaną moc ładowania i brakującą część dobiera Z SIECI
    (hipoteza H-1), a slot `CHARGE_FROM_PV` mówi wprost „ładuj NADWYŻKĄ" — planer ma
    osobny `CHARGE_FROM_GRID` na wypadek, gdy zakup jest zamierzony. Ładowanie
    nadwyżką PV falownik robi natywnie w trybie neutralnym; tracimy nastawę mocy, ale
    nie kupujemy prądu, o który nikt nie prosił. Ceną pomyłki w drugą stronę byłyby
    realne pieniądze.

    Brak `charge_source` przy `mode='charge'` zostawia stare zachowanie (ładowanie):
    plan sprzed rozszerzenia kontraktu nie może przez tę zmianę stracić ładowania.
    """
    if slot.action is Action.CHARGE:
        return None if slot.charge_source == "pv" else Action.CHARGE
    if slot.action is Action.DISCHARGE:
        return Action.DISCHARGE
    # SELF_CONSUME / IDLE — kierunek może siedzieć wyłącznie w polach opisowych.
    if slot.discharge_purpose is not None:
        return Action.DISCHARGE
    if slot.charge_source == "grid":
        # Dziś nieosiągalne z chmury (`chargeSourceForAction` daje przy self_consume
        # tylko 'pv'), ale gdy kontrakt to kiedyś dopuści, zgoda na pobór z sieci
        # musi znaczyć to samo w obu miejscach.
        return Action.CHARGE
    return None


def akcja_efektywna(slot: Slot) -> Action:
    """Intencja slotu dla guardów (U-6) — to, co FAKTYCZNIE pojedzie na falownik.

    `GuardContext.action` nie może być „może None": I-1 rozstrzyga po niej, czy
    zdusić rozładowanie, a I-8 liczy po niej budżet zmian kierunku. Gdy kierunku
    nie ma (`kierunek_slotu` → `None`), mapper zejdzie do trybu neutralnego i nie
    zapisze mocy — więc jedyną prawdziwą odpowiedzią jest `SELF_CONSUME`, a dla
    jawnego „stój" `IDLE`. Podanie tu `CHARGE` dla slotu, który kończy się trybem
    `general` (`charge_source='pv'`), zużywałoby budżet I-8 na przełączenie, którego
    falownik nigdy nie zobaczy — czyli dokładnie odwrotność wady U-7.
    """
    kierunek = kierunek_slotu(slot)
    if kierunek is not None:
        return kierunek
    return Action.IDLE if slot.action is Action.IDLE else Action.SELF_CONSUME


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
        if raw is None:
            return cls()
        # S-2: `float("abc")` rzucał ValueError, ale `float("30")` przechodził po
        # cichu — fallback jest ostatnią linią obrony (I-5), więc nie może zależeć
        # od tego, czy chmura akurat zserializowała liczbę jako string.
        _wymagaj_dict(raw, "fallback")
        if not raw:
            return cls()
        rezerwa = _liczba(raw, "soc_reserve")
        return cls(
            action=_akcja(raw),
            soc_reserve=20.0 if rezerwa is None else rezerwa,
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
        """S-3: brak `slots` to BŁĄD kształtu, a nie „plan bez slotów".

        `raw.get("slots", [])` sprawiało, że DOWOLNY dict był legalnym (pustym)
        harmonogramem — więc plan przysłany pod innym kluczem kasował aktywny plan
        i był raportowany do chmury jako sukces (sonda D).

        DECYZJA: pusta lista slotów JEST legalna — to sposób, w jaki chmura mówi
        „nie mam dla ciebie planu, wejdź w fallback". Odróżnienie od złego kształtu
        jest strukturalne, nie heurystyczne: liczy się OBECNOŚĆ klucza `slots`.
        Wycofanie planu trzeba powiedzieć wprost (`{"slots": []}`); przypadkowy śmieć
        nie ma jak tego udać, bo tego klucza po prostu nie ma.
        """
        _wymagaj_dict(raw, "harmonogram")
        if "slots" not in raw:
            raise InvalidSchedule(
                "slots",
                "harmonogram bez pola 'slots' — wycofanie planu musi być JAWNE "
                "(\"slots\": []), a dict bez tego pola nie jest harmonogramem",
            )
        surowe = raw["slots"]
        if not isinstance(surowe, list):
            raise InvalidSchedule(
                "slots", f"'slots' musi być listą, jest {type(surowe).__name__}"
            )

        slots: list[Slot] = []
        for i, s in enumerate(surowe):
            try:
                slots.append(Slot.from_dict(s))
            except InvalidSchedule as err:
                # S-2 fail-closed: JEDEN zły slot unieważnia CAŁY harmonogram —
                # nie ma tu "częściowego planu", tak samo jak nie ma częściowo
                # zastosowanej komendy w I-10. Numer slotu w komunikacie, bo bez
                # niego chmura nie ma jak znaleźć swojego błędu serializacji.
                raise InvalidSchedule(
                    f"slots[{i}].{err.field}", f"slot #{i}: {err.message}"
                ) from err
        slots.sort(key=lambda s: s.start)

        schedule_id = raw.get("schedule_id", "")
        if not isinstance(schedule_id, str):
            raise InvalidSchedule(
                "schedule_id", f"schedule_id musi być stringiem, jest {schedule_id!r}"
            )
        generated = raw.get("generated_at")
        if generated == "":
            # D-6 (Task 16, znalezione przy naprawie): `get-schedule` (Apka1) wysyła
            # `generated_at=""` dla LEGALNEGO pustego planu — `emptySchedule()` w
            # `schedule-assembly.ts` woła `toDeviceSchedule([], ...)`, a bez slotów
            # nie ma skąd wziąć znacznika czasu, więc kontrakt oddaje pusty string,
            # nie `null`. Bez tej gałęzi `_znacznik` traktowało pusty string jak
            # zepsuty znacznik ISO-8601 i S-2 (fail-closed) odrzucało CAŁY
            # harmonogram — czyli legalna odpowiedź „optymalizator wyłączony, brak
            # planu" nigdy nie docierała do executora, a urządzenie nie wchodziło
            # w fallback I-5, tylko zostawało na starym planie z `Store` bez końca.
            generated = None
        return cls(
            schedule_id=schedule_id,
            generated_at=_znacznik(raw, "generated_at") if generated is not None else None,
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


__all__ = [
    "CHARGE_SOURCES",
    "DISCHARGE_PURPOSES",
    "Fallback",
    "InvalidSchedule",
    "Schedule",
    "Slot",
    "SCHEDULE_VERSION",
    "akcja_efektywna",
    "kierunek_slotu",
]
