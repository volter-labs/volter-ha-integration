"""Guardy i inwarianty EMS — warstwa czysta, bez zależności od Home Assistanta.

Ten moduł jest świadomie wolny od importów HA, żeby:
  * dał się testować na hoście bez uruchamiania HA,
  * dał się przepisać 1:1 na C w firmware Volter BOX (`components/executor/`).

Specyfikacja: `Volter-BOX/03-produkt/guardy-i-inwarianty.md` (inwarianty I-1…I-10,
wektory testowe T-1…T-14, hierarchia priorytetów przy konflikcie).

Hierarchia priorytetów (wygrywa wyższy):
  1. bezpieczeństwo sprzętu (limity falownika/BMS)        — I-3
  2. praca wyspowa i rezerwa backup                        — I-7, I-1
  3. poprawność stanu (brak sprzeczności, świeżość danych)  — I-2, I-9
  4. ekonomia (nie eksportuj przy cenie <= 0)               — I-4
  5. optymalność planu                                      — ustępuje wszystkiemu wyżej
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable

# ── Parametry i ich kolejność stosowania ─────────────────────────────────────

#: Jawna kolejność zapisu parametrów (inwariant I-3 / luka L-6).
#: W części falowników zmiana trybu resetuje limity, więc limity muszą iść PO trybie.
PARAM_ORDER: tuple[str, ...] = (
    "mode",
    "eco_soc",
    "eco_power",
    "charge_limit",
    "discharge_limit",
    "export_limit",
    "export_limit_enabled",
)


class Action(str, Enum):
    """Intencja planu. Guardy pracują na intencji, nie na rejestrach."""

    CHARGE = "charge"
    DISCHARGE = "discharge"
    SELF_CONSUME = "self_consume"
    IDLE = "idle"


@dataclass(frozen=True)
class ParamSpec:
    """Dopuszczalny zakres parametru.

    UWAGA: wartości `hi` dla `charge_limit` i `export_limit` są wstępne i muszą być
    potwierdzone w Etapie 1 wiringu (`Volter-BOX/08-sciezka-a/mapa-nastaw-goodwe.md`).
    Do tego czasu działają jako sanity-check, nie jako prawdziwe limity sprzętowe.
    """

    lo: float
    hi: float
    unit: str
    to_confirm: bool = False


PARAM_SPECS: dict[str, ParamSpec] = {
    "eco_soc": ParamSpec(0, 100, "%"),
    "eco_power": ParamSpec(0, 100, "%"),
    "discharge_limit": ParamSpec(0, 100, "%"),  # GoodWe: DoD
    "charge_limit": ParamSpec(0, 200, "A", to_confirm=True),
    "export_limit": ParamSpec(0, 30000, "W", to_confirm=True),
}

#: Parametry, których obecność oznacza intencję ładowania / rozładowania.
#: Używane przez I-2 do wykrycia sprzeczności i przez I-1 do zerowania rozładowania.
#:
#: TODO(Etap-1): ta heurystyka jest słaba, bo w GoodWe `discharge_limit` to głębokość
#: rozładowania (DoD, %), czyli limit, a nie intencja. Docelowo intencja przychodzi
#: wprost w harmonogramie (`schedule.Slot.action`) i heurystyka jest potrzebna wyłącznie
#: dla starego kontraktu SET_WORK_MODE z surowymi parametrami. Potwierdzić semantykę
#: przy okazji mapy nastaw GoodWe.
_CHARGE_HINTS = ("charge_limit",)
_DISCHARGE_HINTS = ("discharge_limit",)


# ── Wejście guardów ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InverterLimits:
    """Twarde granice sprzętowe (I-3). Docelowo czytane z falownika."""

    max_charge_w: float | None = None
    max_discharge_w: float | None = None
    soc_min_hw: float = 0.0
    soc_max_hw: float = 100.0
    temperature_ok: bool = True
    #: Lista dopuszczalnych opcji encji select trybu pracy. Jeśli None — brak walidacji
    #: enuma (nie znamy jeszcze listy). Docelowo czytana z atrybutu `options` encji.
    allowed_modes: tuple[str, ...] | None = None


@dataclass(frozen=True)
class UserConfig:
    """Ustawienia użytkownika. `soc_reserve` to rezerwa backup w procentach."""

    soc_reserve: float = 20.0
    mode: str = "autarky"  # earn | autarky | backup

    @property
    def hard_reserve(self) -> float:
        """W trybie Backup rezerwa jest nienaruszalna (I-7)."""
        return self.soc_reserve


@dataclass(frozen=True)
class DeviceState:
    """Migawka stanu instalacji. `age_s` to wiek najstarszego istotnego odczytu."""

    soc: float | None = None
    battery_power_w: float | None = None
    pv_power_w: float | None = None
    grid_power_w: float | None = None
    age_s: float = 0.0
    previous_soc: float | None = None


@dataclass
class GuardContext:
    """Kontekst wykonania guardów."""

    state: DeviceState
    limits: InverterLimits = field(default_factory=InverterLimits)
    config: UserConfig = field(default_factory=UserConfig)
    price_pln_kwh: float | None = None
    #: Maksymalny dopuszczalny wiek odczytu (I-9).
    max_state_age_s: float = 300.0
    #: Maksymalny sensowny skok SoC między odczytami w punktach procentowych (I-9).
    max_soc_jump_pp: float = 20.0


class Status(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"
    THROTTLED = "throttled"
    DUPLICATE = "duplicate"
    DEGRADED = "degraded"


@dataclass
class Note:
    """Ślad decyzji guarda — trafia do logu i do raportu do chmury."""

    invariant: str
    message: str


@dataclass
class GuardResult:
    """Wynik przejścia komendy przez guardy."""

    params: dict[str, Any]
    status: Status
    notes: list[Note] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return self.status in (Status.ERROR, Status.DEGRADED, Status.DUPLICATE)

    def note(self, invariant: str, message: str) -> None:
        self.notes.append(Note(invariant, message))

    def as_report(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "params": self.params,
            "notes": [{"invariant": n.invariant, "message": n.message} for n in self.notes],
        }


# ── I-10: sanityzacja wejścia (fail-closed) ──────────────────────────────────


class InvalidCommand(ValueError):
    """Komenda odrzucona całościowo (I-10). Fail-closed: nie stosujemy części parametrów."""

    def __init__(self, invariant: str, message: str) -> None:
        super().__init__(message)
        self.invariant = invariant
        self.message = message


def sanitize_params(params: dict[str, Any], limits: InverterLimits) -> dict[str, Any]:
    """Sprawdź typy i zakresy. Przy jakimkolwiek naruszeniu odrzuć CAŁĄ komendę (I-10).

    Świadomie fail-closed: w energetyce częściowo zastosowana komenda jest groźniejsza
    niż komenda odrzucona. Nieznane parametry są ignorowane (forward compatibility),
    ale znane-a-błędne wywalają całość.
    """
    clean: dict[str, Any] = {}

    for key, value in params.items():
        if key == "mode":
            if not isinstance(value, str) or not value:
                raise InvalidCommand("I-10", f"mode musi być niepustym stringiem, jest {value!r}")
            if limits.allowed_modes is not None and value not in limits.allowed_modes:
                raise InvalidCommand(
                    "I-10", f"mode={value!r} nie jest w dozwolonych: {limits.allowed_modes}"
                )
            clean[key] = value
            continue

        if key == "export_limit_enabled":
            if not isinstance(value, bool):
                raise InvalidCommand("I-10", f"export_limit_enabled musi być bool, jest {value!r}")
            clean[key] = value
            continue

        spec = PARAM_SPECS.get(key)
        if spec is None:
            continue  # nieznany parametr — ignorujemy, nie wywalamy komendy

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidCommand("I-10", f"{key} musi być liczbą, jest {value!r}")
        if not (spec.lo <= float(value) <= spec.hi):
            raise InvalidCommand(
                "I-10", f"{key}={value} poza zakresem {spec.lo}..{spec.hi} {spec.unit}"
            )
        clean[key] = float(value)

    return clean


# ── I-1…I-9: guardy właściwe ─────────────────────────────────────────────────


def apply_guards(params: dict[str, Any], ctx: GuardContext) -> GuardResult:
    """Przepuść zsanityzowane parametry przez inwarianty I-1…I-9.

    Zwraca zmodyfikowany zestaw parametrów (możliwie przycięty) oraz status.
    Nie wykonuje żadnych zapisów — to robi warstwa `applier`.
    """
    out = dict(params)
    result = GuardResult(params=out, status=Status.SUCCESS)
    state, limits, cfg = ctx.state, ctx.limits, ctx.config

    # I-9: świeżość i wiarygodność telemetrii. Sprawdzane PIERWSZE — bez zaufanego
    # stanu nie wolno pisać nic, bo pozostałe guardy nie mają na czym pracować.
    if state.soc is None:
        result.status = Status.DEGRADED
        result.note("I-9", "brak odczytu SoC — wstrzymuję zapisy")
        result.params = {}
        return result
    if state.age_s > ctx.max_state_age_s:
        result.status = Status.DEGRADED
        result.note("I-9", f"odczyt starszy niż {ctx.max_state_age_s:.0f}s (wiek {state.age_s:.0f}s)")
        result.params = {}
        return result
    if not (0.0 <= state.soc <= 100.0):
        result.status = Status.DEGRADED
        result.note("I-9", f"SoC={state.soc} fizycznie niemożliwy")
        result.params = {}
        return result
    if (
        state.previous_soc is not None
        and abs(state.soc - state.previous_soc) > ctx.max_soc_jump_pp
    ):
        result.status = Status.DEGRADED
        result.note(
            "I-9",
            f"skok SoC {state.previous_soc}->{state.soc} przekracza {ctx.max_soc_jump_pp} pp",
        )
        result.params = {}
        return result

    # I-3: temperatura / okno pracy sprzętu — najwyższy priorytet.
    if not limits.temperature_ok:
        result.status = Status.DEGRADED
        result.note("I-3", "falownik/BMS poza oknem temperatur — wstrzymuję zapisy")
        result.params = {}
        return result

    # I-2: sprzeczna intencja (jednoczesne ładowanie i rozładowanie).
    wants_charge = any(out.get(k, 0) for k in _CHARGE_HINTS)
    wants_discharge = any(out.get(k, 0) for k in _DISCHARGE_HINTS)
    if wants_charge and wants_discharge:
        result.status = Status.ERROR
        result.note("I-2", "komenda żąda jednocześnie ładowania i rozładowania")
        result.params = {}
        return result

    # I-7: w trybie Backup rezerwa jest nienaruszalna.
    if cfg.mode == "backup" and "eco_soc" in out and out["eco_soc"] < cfg.hard_reserve:
        result.status = Status.ERROR
        result.note(
            "I-7",
            f"tryb backup: próba obniżenia rezerwy do {out['eco_soc']}% poniżej {cfg.hard_reserve}%",
        )
        result.params = {}
        return result

    # I-1: SoC >= rezerwa użytkownika. Zeruj rozładowanie i podnieś dolny próg.
    if state.soc <= cfg.soc_reserve:
        removed: list[str] = []
        for key in _DISCHARGE_HINTS:
            if out.pop(key, None) is not None:
                removed.append(key)
        if out.get("eco_soc", 0) < cfg.soc_reserve:
            out["eco_soc"] = cfg.soc_reserve
        if removed or out.get("eco_soc") == cfg.soc_reserve:
            result.status = Status.PARTIAL
            result.note(
                "I-1",
                f"SoC={state.soc}% <= rezerwa {cfg.soc_reserve}%: "
                f"usunięto {removed or 'brak'}, eco_soc podniesiony do {cfg.soc_reserve}%",
            )

    # I-3: przycięcie do granic sprzętowych.
    if limits.max_charge_w is not None and "charge_limit" in out:
        # charge_limit jest w A (do potwierdzenia w Etapie 1) — porównanie mocy wymaga
        # napięcia baterii, więc do czasu potwierdzenia stosujemy tylko sanity z PARAM_SPECS.
        pass
    if limits.soc_max_hw < out.get("eco_soc", 0):
        clipped = limits.soc_max_hw
        result.note("I-3", f"eco_soc przycięty {out['eco_soc']}->{clipped} (limit sprzętowy)")
        out["eco_soc"] = clipped
        result.status = Status.PARTIAL if result.status == Status.SUCCESS else result.status

    # I-4: nie eksportuj przy cenie <= 0.
    if ctx.price_pln_kwh is not None and ctx.price_pln_kwh <= 0:
        changed = False
        if out.get("export_limit", None) != 0:
            out["export_limit"] = 0.0
            changed = True
        if out.get("export_limit_enabled") is not True:
            out["export_limit_enabled"] = True
            changed = True
        if changed:
            result.note(
                "I-4",
                f"cena {ctx.price_pln_kwh} PLN/kWh <= 0: eksport zablokowany niezależnie od planu",
            )
            result.status = Status.PARTIAL if result.status == Status.SUCCESS else result.status

    result.params = out
    return result


def ordered(params: dict[str, Any]) -> list[tuple[str, Any]]:
    """Zwróć parametry w jawnej kolejności zapisu (I-3 / L-6).

    Parametry nieznane w `PARAM_ORDER` idą na koniec, w kolejności alfabetycznej,
    żeby zachowanie było deterministyczne.
    """
    known = [(k, params[k]) for k in PARAM_ORDER if k in params]
    rest = sorted((k, v) for k, v in params.items() if k not in PARAM_ORDER)
    return known + rest


# ── I-6: throttling zapisów ──────────────────────────────────────────────────


class WriteThrottle:
    """Chroni pamięć nieulotną falownika (I-6).

    Nastawy eco mode w GoodWe zapisują się do pamięci nieulotnej — częste zapisy
    zużywają sprzęt. Dwie reguły:
      * nie zapisuj wartości, która się nie zmieniła,
      * nie zapisuj tego samego parametru częściej niż co `min_interval_s`.
    """

    def __init__(self, min_interval_s: float = 60.0) -> None:
        self.min_interval_s = min_interval_s
        self._last_value: dict[str, Any] = {}
        self._last_write_ts: dict[str, float] = {}

    def filter(
        self, params: dict[str, Any], now_ts: float
    ) -> tuple[dict[str, Any], list[Note]]:
        """Zwróć parametry, które wolno zapisać, oraz noty o pominięciach."""
        allowed: dict[str, Any] = {}
        notes: list[Note] = []

        for key, value in params.items():
            if key in self._last_value and self._last_value[key] == value:
                notes.append(Note("I-6", f"{key}={value} bez zmiany — zapis pominięty"))
                continue
            last_ts = self._last_write_ts.get(key)
            if last_ts is not None and (now_ts - last_ts) < self.min_interval_s:
                notes.append(
                    Note(
                        "I-6",
                        f"{key} zapisany {now_ts - last_ts:.0f}s temu "
                        f"(< {self.min_interval_s:.0f}s) — zapis pominięty",
                    )
                )
                continue
            allowed[key] = value

        return allowed, notes

    def commit(self, params: dict[str, Any], now_ts: float) -> None:
        """Zarejestruj faktycznie wykonane zapisy."""
        for key, value in params.items():
            self._last_value[key] = value
            self._last_write_ts[key] = now_ts


# ── I-8: anty-oscylacja ──────────────────────────────────────────────────────


class DirectionLimiter:
    """Ogranicza liczbę zmian kierunku ładowanie<->rozładowanie (I-8).

    Chroni falownik i baterię przed oscylacją wywołaną szumem w planie
    albo w prognozie. Domyślnie 4 zmiany na godzinę.
    """

    def __init__(self, max_changes_per_hour: int = 4, window_s: float = 3600.0) -> None:
        self.max_changes = max_changes_per_hour
        self.window_s = window_s
        self._history: list[tuple[float, Action]] = []
        self._current: Action | None = None

    def allows(self, action: Action, now_ts: float) -> tuple[bool, Note | None]:
        directional = (Action.CHARGE, Action.DISCHARGE)
        if action not in directional:
            return True, None
        if self._current == action:
            return True, None

        self._prune(now_ts)
        changes = len(self._history)
        if changes >= self.max_changes:
            return False, Note(
                "I-8",
                f"{changes} zmian kierunku w ostatniej godzinie (limit {self.max_changes}) "
                f"— zmiana na {action.value} zignorowana",
            )
        return True, None

    def record(self, action: Action, now_ts: float) -> None:
        if action in (Action.CHARGE, Action.DISCHARGE) and self._current != action:
            self._history.append((now_ts, action))
        self._current = action

    def _prune(self, now_ts: float) -> None:
        cutoff = now_ts - self.window_s
        self._history = [(ts, a) for ts, a in self._history if ts >= cutoff]


# ── L-4: idempotencja ────────────────────────────────────────────────────────


class RequestDeduplicator:
    """Odrzuca powtórnie dostarczone komendy po `request_id` (luka L-4)."""

    def __init__(self, capacity: int = 256) -> None:
        self.capacity = capacity
        self._seen: list[str] = []
        self._set: set[str] = set()

    def is_duplicate(self, request_id: str | None) -> bool:
        if not request_id:
            return False
        return request_id in self._set

    def remember(self, request_id: str | None) -> None:
        if not request_id or request_id in self._set:
            return
        self._seen.append(request_id)
        self._set.add(request_id)
        while len(self._seen) > self.capacity:
            self._set.discard(self._seen.pop(0))


def infer_action(params: dict[str, Any]) -> Action:
    """Wywnioskuj kierunek z parametrów — potrzebne dla I-8 przy starym kontrakcie.

    Docelowo intencja przychodzi wprost w harmonogramie (`schedule.py`) i ta
    heurystyka jest już niepotrzebna.
    """
    if any(params.get(k, 0) for k in _CHARGE_HINTS):
        return Action.CHARGE
    if any(params.get(k, 0) for k in _DISCHARGE_HINTS):
        return Action.DISCHARGE
    mode = str(params.get("mode", "")).lower()
    # UWAGA na kolejność: "eco_discharge" zawiera podciąg "charge", więc rozładowanie
    # musi być sprawdzane PIERWSZE. Odwrotna kolejność dawała CHARGE dla eco_discharge
    # (bug wyłapany wektorem testowym).
    if "discharge" in mode:
        return Action.DISCHARGE
    if "charge" in mode:
        return Action.CHARGE
    return Action.SELF_CONSUME


__all__ = [
    "Action",
    "DeviceState",
    "DirectionLimiter",
    "GuardContext",
    "GuardResult",
    "InvalidCommand",
    "InverterLimits",
    "Note",
    "PARAM_ORDER",
    "PARAM_SPECS",
    "RequestDeduplicator",
    "Status",
    "UserConfig",
    "WriteThrottle",
    "apply_guards",
    "infer_action",
    "ordered",
    "sanitize_params",
]
