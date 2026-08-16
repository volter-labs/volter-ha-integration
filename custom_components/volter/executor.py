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

import asyncio
import copy
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .applier import apply_params
from .const import (
    COMMAND_ENTITY_MAP,
    DEFAULT_RATED_POWER_W,
    DEFAULT_SOC_RESERVE,
    EXECUTOR_INTERVAL,
    MAX_DIRECTION_CHANGES_PER_HOUR,
    MAX_SOC_JUMP_PP,
    MAX_SOC_RATE_PP_PER_MIN,
    MAX_STATE_AGE_S,
    MIN_SOC_JUMP_TOLERANCE_PP,
    OPT_ENTITY_EXPORT_LIMIT_SWITCH,
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
    Note,
    Status,
    UserConfig,
    WriteThrottle,
    apply_guards,
    infer_action,
    sanitize_params,
)
from .ha_state import ParamBoundsCache, read_device_state, read_inverter_limits
from .mappers import slot_to_params
from .schedule import Fallback, Schedule, Slot

_LOGGER = logging.getLogger(__name__)


def _mapped_entity(param_key: str, options: dict[str, Any]) -> bool:
    """Czy `param_key` ma zmapowaną encję w opcjach integracji.

    R-7: `async_diagnose` musi wiedzieć dokładnie to samo, co `applier.apply_params`
    naprawdę zrobi — inaczej `would_write` pokazuje zapis, który w realnym
    `async_apply` skończy się cichym błędem R-3 (encja niezmapowana), nie zapisem.
    """
    if param_key == "export_limit_enabled":
        return bool(options.get(OPT_ENTITY_EXPORT_LIMIT_SWITCH))
    mapping = COMMAND_ENTITY_MAP.get(param_key)
    if not mapping:
        return False
    opt_key = mapping[0]
    return bool(options.get(opt_key))


class VolterExecutor:
    """Pętla wykonawcza + wspólna brama zapisu dla harmonogramu i komend."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self._schedule: Schedule | None = None
        self._unsub: CALLBACK_TYPE | None = None
        self._throttle = WriteThrottle(min_interval_s=WRITE_MIN_INTERVAL_S)
        # S-1: jeden zamek na CAŁY tor zapisu, bo `async_apply` woła tick co 60 s
        # ORAZ `command_handler` z chmury — bez niego przeplot na `await` w
        # `apply_params` zostawiał na falowniku wartość jednego przebiegu, a w
        # `_throttle` wartość drugiego (rozjazd TRWAŁY: I-6 uznaje potem wartość
        # planu za „bez zmiany" i nigdy jej nie dopisuje).
        self._write_lock = asyncio.Lock()
        self._direction = DirectionLimiter(max_changes_per_hour=MAX_DIRECTION_CHANGES_PER_HOUR)
        self._prev_soc: float | None = None
        # RR-1: znacznik czasu przyjęcia baseline'u SoC. I-9 testuje TEMPO zmiany, więc
        # sama wartość poprzedniej próbki nie wystarcza — bez czasu 35 pp po 40 minutach
        # przerwy wygląda tak samo jak 35 pp w 10 sekund.
        self._prev_soc_ts: float | None = None
        self._last: dict[str, Any] = {}
        self._last_decision: str | None = None
        # R-12 (reszta): klucz osobny od `_last_decision` — treść notatek guardów
        # (np. komunikat I-9) może się zmienić niezależnie od tego, czy `decision`
        # (akcja/params/status/executed) się zmieniła. `None` na starcie gwarantuje,
        # że pierwszy przebieg zawsze loguje na INFO/WARNING (nie ma z czym porównać).
        self._last_notes_key: tuple[tuple[str, str], ...] | None = None
        # RR-2: pamięć ostatnich znanych granic nastaw. Bez niej werdykt I-10 zależał od
        # tego, czy encja `number` akurat odpowiada — ta sama komenda dostawała raz
        # `success`, raz `error`. Cache jest per-executor i ginie z restartem HA.
        self._bounds_cache = ParamBoundsCache()

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

    def _soc_sample_gap_s(self) -> float | None:
        """RR-1: ile czasu upłynęło od przyjęcia baseline'u SoC (albo `None`).

        `None` znaczy „nie mamy jeszcze żadnej zaufanej próbki" — wtedy I-9 nie ma
        czego porównywać albo wraca do konserwatywnego progu bezwzględnego.
        """
        if self._prev_soc_ts is None:
            return None
        return max(0.0, time.monotonic() - self._prev_soc_ts)

    async def async_apply(
        self,
        raw_params: dict[str, Any],
        *,
        price_pln_kwh: float | None = None,
        action: Action | None = None,
        source: str = "cloud",
        slot: Slot | None = None,
        _remapped: bool = False,
        _extra_forced_params: frozenset[str] = frozenset(),
        _report: bool = True,
    ) -> GuardResult:
        """Publiczna brama zapisu — serializuje przebiegi obu pętli (S-1).

        S-1: `async_apply` wołają DWIE niezależne pętle: tick co 60 s i
        `command_handler` obsługujący komendy z chmury. `apply_params` ma w środku
        `await` na każdym service callu, więc bez zamka przebiegi przeplatały się
        parametr po parametrze: na falowniku zostawała wartość tego, kto pisał
        fizycznie jako ostatni, a w `_throttle` wartość tego, kto commitował jako
        ostatni. Rozjazd jest TRWAŁY — od tej chwili I-6 widzi wartość planu jako
        „bez zmiany" i nigdy jej nie dopisze, bez żadnego sygnału. Przy okazji
        znikał drugi skutek: podwójny zapis tej samej encji w oknie I-6 (zużycie
        NVM) i złamanie PARAM_ORDER MIĘDZY przebiegami (`mode` jednego lądował po
        limitach drugiego).

        S-1 — DLACZEGO zamek jest tutaj, a nie wokół samego `apply_params`:
        odczyt stanu (`read_inverter_limits`, `read_device_state`, cena z aktualnego
        slotu) siedzi w `_async_apply_locked`, czyli już ZA zamkiem. Przebieg, który
        czekał w kolejce, przelicza więc guardy na rzeczywistości PO cudzym zapisie,
        a nie na migawce sprzed czekania — w międzyczasie SoC mógł spaść poniżej
        rezerwy i I-1 musi to zobaczyć. Odświeżamy STAN ŚWIATA, ale NIE intencję:
        `raw_params` zostają takie, jakie podał wołający (komendy operatora nie
        wolno po cichu przepisać, a slot z ticku i tak zostanie poprawiony w ≤60 s
        przez kolejny przebieg pętli).

        S-1 — DLACZEGO rozdzielenie na dwie metody, a nie `asyncio.Lock` wprost na
        tej: ścieżka `forced_action` (naprawa R-1) woła tor zapisu REKURENCYJNIE ze
        swojego wnętrza. `asyncio.Lock` nie jest reentrantny, więc zamek założony na
        tej metodzie zakleszczyłby przemapowanie na tryb bezpieczny na zawsze —
        czyli dokładnie ścieżkę ochronną. Licznik reentrancji odrzucony świadomie:
        wymaga `contextvars` albo trzymania „właściciela" i cicho przepuszcza także
        przypadkowe zagnieżdżenie z innego miejsca. Wydzielenie
        `_async_apply_locked` sprawia, że reentrancja jest MOŻLIWA WYŁĄCZNIE tam,
        gdzie jest wprost zapisana w kodzie, i widać ją w jednym miejscu.
        """
        async with self._write_lock:
            return await self._async_apply_locked(
                raw_params,
                price_pln_kwh=price_pln_kwh,
                action=action,
                source=source,
                slot=slot,
                _remapped=_remapped,
                _extra_forced_params=_extra_forced_params,
                _report=_report,
            )

    async def _async_apply_locked(
        self,
        raw_params: dict[str, Any],
        *,
        price_pln_kwh: float | None = None,
        action: Action | None = None,
        source: str = "cloud",
        slot: Slot | None = None,
        _remapped: bool = False,
        _extra_forced_params: frozenset[str] = frozenset(),
        _report: bool = True,
    ) -> GuardResult:
        """Właściwy tor zapisu: sanityzacja → guardy → throttle → zapis.

        S-1: WOŁAĆ WYŁĄCZNIE spod `self._write_lock` — publicznym wejściem jest
        `async_apply`. Jedyne dozwolone wywołanie bezpośrednie to rekurencja po
        `forced_action` niżej, która z definicji już trzyma zamek.

        `slot` podaje ścieżka harmonogramu. Jest potrzebny wyłącznie po to, żeby przy
        `GuardResult.forced_action` dało się przemapować slot na tryb bezpieczny (R-1);
        `_remapped` gwarantuje, że dzieje się to najwyżej raz.

        `_extra_forced_params` (RR-3): parametry, które NIE pochodzą z `apply_guards`
        (bo guardy pracują na wartościach, nie na nazwach encji falownika), ale mimo
        to są substytutem bezpieczeństwa wymuszonym przez guard — dziś wyłącznie
        `mode` w przebiegu przemapowanym po `forced_action`. Musi trafić do
        `result.forced_params` PRZED wywołaniem `apply_params`, inaczej brak
        zmapowanej encji trybu wyglądałby jak zwykła nota, a nie jak zniknięcie
        ochrony I-1 (dokładnie ten wzorzec luki, który zamyka RR-3).

        `_report` (RR-5): `False` wyłącznie dla WEWNĘTRZNEGO rekurencyjnego wywołania
        po `forced_action` (patrz niżej) — ten przebieg nie loguje ani nie aktualizuje
        `_last_decision` sam, bo przebieg ZEWNĘTRZNY i tak zrobi to za niego z pełnym,
        scalonym raportem. Bez tego dwa wywołania `_remember` (wewnętrzne i zewnętrzne)
        nadpisywały sobie nawzajem `_last_decision` w KAŻDYM ticku, więc anty-spam z
        R-12 nigdy się nie stabilizował (RR-5).
        """
        options = dict(self._entry.options)
        # RR-2: realny tor zapisu UCZY cache granic (i z niego korzysta, gdy encja
        # chwilowo nie odpowiada). Znacznik bierzemy z tego samego zegara co I-6/I-8,
        # żeby cały przebieg był czasowo spójny.
        limits = read_inverter_limits(
            self.hass, options, self._bounds_cache, now=time.monotonic()
        )
        state = read_device_state(
            self.hass,
            options,
            self._prev_soc,
            # RR-1: odstęp od poprzedniej ZAUFANEJ próbki — jedyna informacja, dzięki
            # której I-9 odróżni awarię czujnika od zmiany po przerwie w telemetrii.
            previous_soc_age_s=self._soc_sample_gap_s(),
        )
        cfg = UserConfig(
            soc_reserve=float(options.get(OPT_SOC_RESERVE, DEFAULT_SOC_RESERVE)),
            mode=str(options.get(OPT_USER_MODE, "autarky")),
        )

        # R-6: I-4 jest inwariantem (poziom 4 hierarchii specyfikacji), nie regułą
        # planu — ma bronić także przed komendą operatora. Ścieżka SET_WORK_MODE
        # z chmury nie niesie ceny wcale (domyślne `None`), więc bez tego I-4 było
        # martwe dokładnie tam, gdzie miało chronić przed poleceniem z zewnątrz.
        effective_price = price_pln_kwh
        if effective_price is None and self._schedule is not None:
            current_slot, _ = self._schedule.effective_slot(datetime.now(timezone.utc))
            effective_price = current_slot.price_pln_kwh

        # I-10 — fail-closed
        try:
            clean = sanitize_params(raw_params, limits)
        except InvalidCommand as err:
            _LOGGER.error("[%s] Komenda odrzucona (%s): %s", source, err.invariant, err.message)
            result = GuardResult(params={}, status=Status.ERROR)
            result.note(err.invariant, err.message)
            # R-12: wczesny return (I-10) musi przejść przez ten sam log decyzji co
            # ścieżka normalna — inaczej seria odrzuceń nigdy nie zmienia
            # `_last_decision` i powrót do sukcesu po niej nie generuje INFO.
            # RR-5: gałąź WEWNĘTRZNA (przemapowanie po forced_action) nie loguje sama.
            if _report:
                self._remember(source, result, [], [], action or infer_action(raw_params))
            return result

        # I-1…I-9
        ctx = GuardContext(
            state=state,
            limits=limits,
            config=cfg,
            price_pln_kwh=effective_price,
            # R-1: bez jawnej intencji guardy musiałyby zgadywać kierunek z nazw trybów
            # falownika, których świadomie nie znają — I-1 i I-2 były przez to martwe
            # na całej ścieżce harmonogramu.
            action=action,
            max_state_age_s=MAX_STATE_AGE_S,
            max_soc_jump_pp=MAX_SOC_JUMP_PP,
            max_soc_rate_pp_per_min=MAX_SOC_RATE_PP_PER_MIN,
            min_soc_jump_tolerance_pp=MIN_SOC_JUMP_TOLERANCE_PP,
        )
        result = apply_guards(clean, ctx)
        if _extra_forced_params:
            # RR-3: dołożone PRZED zapisem — patrz docstring `_extra_forced_params`.
            result.forced_params |= _extra_forced_params
        # R-5: baseline SoC dla NASTĘPNEGO przebiegu aktualizujemy WYŁĄCZNIE z odczytów,
        # których samo I-9 nie zakwestionowało — inaczej odczyt odrzucony jako fizycznie
        # niemożliwy stawałby się zaufanym punktem odniesienia już w kolejnym ticku
        # i guard chroniłby przez dokładnie jeden przebieg.
        #
        # RR-1: warunkiem jest `soc_baseline_ok`, a NIE brak noty I-9. Notę I-9 zostawia
        # też przyjęcie nowego baseline'u po długiej przerwie — mylenie tych dwóch
        # przypadków zamrażało punkt odniesienia na stałe i blokowało tor zapisu aż do
        # restartu HA. Znacznik czasu idzie w parze z wartością, bo tempo liczy się od
        # momentu przyjęcia baseline'u.
        if result.soc_baseline_ok and state.soc is not None:
            self._prev_soc = state.soc
            self._prev_soc_ts = time.monotonic()

        # R-12 (reszta): notatki guardów i WARNING „Zapis wstrzymany" NIE są już
        # logowane tutaj bezwarunkowo — obie linie przeniesione do `_remember`,
        # gdzie podlegają TEJ SAMEJ regule anty-spamu co linia „Decyzja" (INFO/WARNING
        # przy zmianie, DEBUG przy powtórce). Wcześniej ta pętla stała PRZED
        # sprawdzeniem `result.rejected`, więc ścieżka DEGRADED logowała INFO+WARNING
        # bezwarunkowo na KAŻDYM ticku, mimo że „Decyzja" już była wyciszona.
        if result.rejected:
            # R-12: DEGRADED/ERROR/DUPLICATE to wczesny return — bez aktualizacji
            # `_last_decision` tutaj ścieżka DEGRADED nie ma żadnej ochrony przed
            # anty-spamem, a powrót do sukcesu po awarii z identyczną decyzją co
            # przed nią wygląda jak "bez zmian".
            # RR-5: gałąź WEWNĘTRZNA (przemapowanie po forced_action) nie loguje sama.
            if _report:
                self._remember(source, result, [], [], action or infer_action(clean))
            return result

        # R-1: guard podmienił intencję planu (np. I-1 zdusiło rozładowanie). Zapis
        # ORYGINALNYCH parametrów byłby wtedy szkodliwy, bo `mode` nadal niesie tryb
        # rozładowania — wracamy więc do mappera po komplet nastaw dla trybu bezpiecznego.
        # Przemapowanie robimy najwyżej raz (`_remapped`), żeby mapper i guardy nie
        # mogły wpaść w pętlę, gdyby nowa akcja też została zakwestionowana.
        if result.forced_action is not None and not _remapped:
            if slot is not None:
                forced_slot = replace(slot, action=result.forced_action)
                rated = float(options.get(OPT_RATED_POWER_W, DEFAULT_RATED_POWER_W))
                _LOGGER.warning(
                    "[%s] Guardy wymusiły akcję %s zamiast %s — przemapowuję slot",
                    source,
                    result.forced_action.value,
                    (action.value if action else "?"),
                )
                # S-1: rekurencja idzie do części BEZ zamka — ten przebieg już go
                # trzyma (`asyncio.Lock` nie jest reentrantny, więc `async_apply`
                # zakleszczyłoby tu ścieżkę ochronną R-1 na zawsze).
                forced = await self._async_apply_locked(
                    slot_to_params(forced_slot, rated_power_w=rated),
                    price_pln_kwh=price_pln_kwh,
                    action=result.forced_action,
                    source=source,
                    slot=forced_slot,
                    _remapped=True,
                    # RR-3: "mode" w TYM przebiegu to substytut bezpieczny
                    # wymuszony przez I-1, nie zwykła treść planu — brak
                    # zmapowanej encji trybu musi być głośnym błędem (tak jak
                    # I-4 w R-3), nie cichą notą o opcjonalnej encji.
                    _extra_forced_params=frozenset({"mode"}),
                    # RR-5: przebieg WEWNĘTRZNY nie loguje ani nie aktualizuje
                    # `_last_decision`/`_last_notes_key` sam — przebieg ZEWNĘTRZNY
                    # (kilka linii niżej) i tak woła `_remember` z pełnym, scalonym
                    # raportem `forced`. Bez tego dwa wywołania `_remember` (z różnymi
                    # kluczami decyzji: `success` z wewnętrznego, `partial` ze
                    # scalonego) nadpisywały sobie `_last_decision` na KAŻDYM ticku —
                    # anty-spam R-12 nigdy się nie stabilizował (sonda P2, RR-5).
                    _report=False,
                )
                # Powód wymuszenia (nota I-1) powstał w PIERWSZYM przebiegu — bez przeniesienia
                # go dalej raport do chmury nie tłumaczyłby, czemu plan nie został wykonany.
                forced.notes = list(result.notes) + list(forced.notes)
                forced.forced_action = result.forced_action
                if forced.status is Status.SUCCESS:
                    forced.status = Status.PARTIAL
                # R-12: nadpisujemy `_last` (i `_last_decision`) pełnym raportem
                # wymuszonej akcji, nie oryginalnie zamierzonej — to ONA faktycznie
                # zaszła.
                self._remember(
                    source, forced, list(forced.executed), self._last.get("errors", []),
                    result.forced_action,
                )
                return forced

            if "mode" in result.params:
                # Stary kontrakt bez slotu: nie ma z czego zbudować kompletu nastaw dla
                # trybu bezpiecznego, ale wepchnięcie falownika w rozładowanie poniżej
                # rezerwy byłoby wprost naruszeniem I-1. Zostawiamy resztę parametrów
                # (m.in. podniesiony eco_soc) — pętla harmonogramu poprawi tryb w ≤60 s.
                pominiety = result.params.pop("mode")
                result.note(
                    "I-1",
                    f"tryb {pominiety!r} pominięty — komenda bez slotu nie może wymusić "
                    f"{result.forced_action.value}",
                )
                if result.status is Status.SUCCESS:
                    result.status = Status.PARTIAL

        now_ts = time.monotonic()

        # I-8 — anty-oscylacja
        # RR-6: gdy guard wymusił inną akcję niż żądana (I-1, gałąź bez slotu —
        # ta ZE slotem wraca wcześniej przez rekurencję z jawnym `action=forced_action`),
        # DirectionLimiter i throttling I-8 muszą widzieć akcję EFEKTYWNĄ
        # (`result.forced_action`), a NIE żądaną przez wołającego. Inaczej
        # `_direction.record()` niżej zapisywałby kierunek, który guard I-1 właśnie
        # zablokował — nic rozładowującego nie idzie do falownika, a mimo to
        # `_direction._current` pokazywałby DISCHARGE (sonda P6, RR-6).
        act = result.forced_action or action or infer_action(result.params)
        allowed, note = self._direction.allows(act, now_ts)
        if not allowed and note is not None:
            result.status = Status.THROTTLED
            result.note(note.invariant, note.message)
            # RR-5: manualny log usunięty — notatka I-8 dołożona linię wyżej trafia
            # do `result.notes`, więc `_remember` (wołane niżej) ją zaloguje sama,
            # z anty-spamem R-12 (reszta). Podwójne logowanie (raz tu, raz w
            # `_remember`) dawałoby dwa wpisy na tę samą notatkę.
            # RR-5: gałąź WEWNĘTRZNA (przemapowanie po forced_action) nie loguje sama
            # — `_report=False` w rekurencyjnym wywołaniu, patrz docstring `_report`.
            if _report:
                self._remember(source, result, [], [], act)
            return result

        # I-6 — throttling zapisów (ochrona pamięci nieulotnej falownika)
        writable, throttle_notes = self._throttle.filter(result.params, now_ts)
        for tn in throttle_notes:
            result.note(tn.invariant, tn.message)
            _LOGGER.debug("[%s] guard %s: %s", source, tn.invariant, tn.message)

        executed: list[str] = []
        errors: list[dict[str, str]] = []

        if not writable:
            result.executed = []
        else:
            # RR-3: `forced_params` rozstrzyga w `apply_params`, czy brak zmapowanej
            # encji dla danego parametru jest błędem (zabezpieczenie guarda) czy tylko
            # widoczną notą (normalny parametr planu, opcjonalna encja świadomie
            # niezmapowana) — patrz `GuardResult.forced_params` i docstring
            # `apply_params`.
            executed, errors, skip_notes = await apply_params(
                self.hass, options, writable, forced_params=result.forced_params
            )
            # N-4: raport do chmury musi mówić prawdę — `executed` to to, co naprawdę
            # poszło do falownika, nie to, co przeszło guardy (`result.params`).
            result.executed = executed
            # R-9: bez tego przypisania realne błędy per-encja (m.in. niezmapowana
            # encja z R-3) ginęły — command_handler raportował do chmury errors=[]
            # na sztywno, niezależnie od tego, co faktycznie zawiodło.
            result.errors = errors
            # RR-3: parametry z normalnego mapowania planu bez zmapowanej encji nie
            # są błędem, ale nie mogą też zniknąć bez śladu — trafiają do `notes`,
            # więc chmura i log HA nadal je widzą (zgodnie z zasadą "nota widoczna,
            # nie błąd" dla tej kategorii).
            for skip in skip_notes:
                result.note("RR-3", f"{skip['entity']}: {skip['note']}")

            self._throttle.commit({k: writable[k] for k in executed if k in writable}, now_ts)
            self._direction.record(act, now_ts)

            if errors and not executed:
                result.status = Status.ERROR
            elif errors:
                result.status = Status.PARTIAL

        # R-12: log decyzji (anty-spam + `_last_decision`) przeniesiony do `_remember`,
        # żeby DZIAŁAŁ TAKŻE na wczesnych returnach (I-10, I-1..I-9, I-8) — patrz
        # komentarz przy `_remember`.
        # RR-5: gałąź WEWNĘTRZNA (przemapowanie po forced_action) nie loguje sama —
        # bez tej bramki przebieg wewnętrzny (status np. `success`) i przebieg
        # zewnętrzny scalony (status np. `partial`) wołały `_remember` osobno z
        # DWOMA różnymi kluczami decyzji, które nadpisywały się nawzajem —
        # `decision != self._last_decision` było prawdziwe na KAŻDYM ticku, mimo
        # ustabilizowanego stanu (sonda P2, RR-5).
        if _report:
            self._remember(source, result, executed, errors, act)
        return result

    # ── pętla ────────────────────────────────────────────────────────────────

    async def _async_tick(self, _now: datetime | None = None) -> None:
        await self._async_execute_now()

    async def _async_execute_now(self) -> GuardResult:
        options = dict(self._entry.options)

        if self._schedule is None:
            # R-11: brak harmonogramu to stan DOMYŚLNY po instalacji i po restarcie
            # z pustym storage — nie wolno go traktować inaczej niż wygaśnięcie planu.
            # Cichy SUCCESS bez zapisu zostawiał falownik na dowolnej wcześniejszej
            # nastawie; I-5 wymaga jawnego fallbacku (self_consume + rezerwa
            # użytkownika), nie „nic nie rób".
            reserve = float(options.get(OPT_SOC_RESERVE, DEFAULT_SOC_RESERVE))
            fallback = Fallback(action=Action.SELF_CONSUME, soc_reserve=reserve)
            slot = fallback.as_slot(datetime.now(timezone.utc))
            _LOGGER.info(
                "Brak harmonogramu — wchodzę w domyślny fallback: self_consume, "
                "rezerwa %s%% [I-5]",
                reserve,
            )
            rated = float(options.get(OPT_RATED_POWER_W, DEFAULT_RATED_POWER_W))
            params = slot_to_params(slot, rated_power_w=rated)
            return await self.async_apply(
                params,
                price_pln_kwh=slot.price_pln_kwh,
                action=slot.action,
                source="fallback",
                slot=slot,
            )

        now = datetime.now(timezone.utc)
        slot, is_fallback = self._schedule.effective_slot(now)
        if is_fallback:
            _LOGGER.info(
                "Plan wygasł (ważny do %s) — wchodzę w fallback: %s, rezerwa %s%% [I-5]",
                self._schedule.valid_until,
                self._schedule.fallback.action.value,
                self._schedule.fallback.soc_reserve,
            )

        rated = float(options.get(OPT_RATED_POWER_W, DEFAULT_RATED_POWER_W))
        params = slot_to_params(slot, rated_power_w=rated)

        return await self.async_apply(
            params,
            price_pln_kwh=slot.price_pln_kwh,
            action=slot.action,
            source="fallback" if is_fallback else "schedule",
            # R-1: slot jedzie dalej, żeby guardy mogły kazać przemapować go na tryb
            # bezpieczny bez wiedzy o nazwach trybów falownika.
            slot=slot,
        )

    # ── diagnostyka ──────────────────────────────────────────────────────────

    async def async_diagnose(self) -> dict[str, Any]:
        """Suchy przebieg: policz, co executor zrobiłby teraz — i nic nie zapisz.

        To jest odpowiedź na pytanie „dlaczego nic się nie dzieje". Zwraca stan
        zmapowanych encji, granice odczytane z falownika (w tym realne `allowed_modes`,
        które weryfikują hipotezę z `mappers.py`), wybrany slot, wynik sanityzacji
        i guardów oraz listę zapisów, które faktycznie by poszły po throttlingu.

        Twarde reguły tej metody:
          * ZERO zapisów — żadnego `hass.services.async_call`,
          * podgląd throttlingu przez `filter`, nigdy `commit` — stan I-6 zostaje nietknięty,
          * cache granic z encji widziany przez KOPIĘ (`snapshot`) — suchy przebieg
            niczego nie zapamiętuje (RR-2),
          * `self._prev_soc`, `self._prev_soc_ts` i `self._last` pozostają bez zmian
            (to suchy przebieg,
            nie przebieg — nie wolno mu zafałszować kolejnego realnego wykonania).
        """
        options = dict(self._entry.options)
        # RR-2: suchy przebieg widzi te same zapamiętane granice co realny tor zapisu,
        # ale pisze na KOPII cache — inaczej diagnoza uczyłaby executor granic, czyli
        # łamała własny kontrakt „zero mutacji stanu" (R-7/R-14).
        limits = read_inverter_limits(
            self.hass, options, self._bounds_cache.snapshot(), now=time.monotonic()
        )
        # RR-1: suchy przebieg musi widzieć ten sam odstęp próbek co realny — inaczej
        # diagnoza pokazywałaby I-9 tam, gdzie `async_apply` przyjmuje nowy baseline.
        # Sam ODCZYT `_prev_soc_ts` niczego nie mutuje, więc kontrakt „zero mutacji" stoi.
        state = read_device_state(
            self.hass, options, self._prev_soc, previous_soc_age_s=self._soc_sample_gap_s()
        )
        cfg = UserConfig(
            soc_reserve=float(options.get(OPT_SOC_RESERVE, DEFAULT_SOC_RESERVE)),
            mode=str(options.get(OPT_USER_MODE, "autarky")),
        )

        # Mapowanie encji: `found: false` odpowiada wprost na „zmapowałem, ale nie istnieje".
        entities: dict[str, Any] = {}
        for opt_key, entity_id in options.items():
            if not isinstance(entity_id, str) or "." not in entity_id:
                continue
            st = self.hass.states.get(entity_id)
            entities[opt_key] = {
                "entity_id": entity_id,
                "state": None if st is None else st.state,
                "found": st is not None,
            }

        now = datetime.now(timezone.utc)
        slot_info: dict[str, Any] = {"source": "brak harmonogramu"}
        raw_params: dict[str, Any] = {}
        slot_action: Action | None = None
        if self._schedule is not None:
            slot, is_fallback = self._schedule.effective_slot(now)
            slot_action = slot.action
            rated = float(options.get(OPT_RATED_POWER_W, DEFAULT_RATED_POWER_W))
            raw_params = slot_to_params(slot, rated_power_w=rated)
            slot_info = {
                "source": "fallback" if is_fallback else "schedule",
                "from": slot.start.isoformat(),
                "to": slot.end.isoformat(),
                "action": slot.action.value,
                "soc_target": slot.soc_target,
                "price_pln_kwh": slot.price_pln_kwh,
            }

        report: dict[str, Any] = {
            "at": now.isoformat(),
            "entities": entities,
            "state": {
                "soc": state.soc,
                # Brak zmapowanych encji monitoringu daje age_s = inf, a to nie jest
                # poprawny JSON w odpowiedzi serwisu HA — raportujemy wtedy None.
                "age_s": state.age_s if state.age_s < float("inf") else None,
                "pv_power_w": state.pv_power_w,
                "grid_power_w": state.grid_power_w,
            },
            "limits": {
                "allowed_modes": list(limits.allowed_modes) if limits.allowed_modes else None,
                "soc_max_hw": limits.soc_max_hw,
                # R-8: bez tego nie da się odróżnić „nastawa przycięta do granicy sprzętu"
                # od „encja nie podała granicy i zadziałał tylko sanity-check PARAM_SPECS" —
                # a to pierwsze pytanie przy smoke teście, gdy zapis idzie inny niż plan.
                "param_bounds": {
                    key: {"min": b.lo, "max": b.hi} for key, b in limits.param_bounds.items()
                },
            },
            "config": {"soc_reserve": cfg.soc_reserve, "mode": cfg.mode},
            "slot": slot_info,
            "raw_params": raw_params,
            "would_write": {},
            # R-14: kopia obronna — `async_diagnose` obiecuje ZERO mutacji stanu
            # wewnętrznego ("suchy przebieg"). Żywa referencja do `self._last`
            # przekazywana dalej do konsumenta (serwis HA -> chmura) łamałaby tę
            # obietnicę w chwili, gdy ktoś zmodyfikowałby odpowiedź w miejscu.
            # Głęboka kopia, bo `self._last` zawiera zagnieżdżone listy (`executed`,
            # `errors`, `notes`) — płytki `dict(...)` dzieliłby te listy z oryginałem.
            "last": copy.deepcopy(self._last),
        }

        # Guardy liczymy ZAWSZE, także bez harmonogramu — bo najczęstsze pytanie brzmi
        # „czy stan instalacji w ogóle pozwoliłby cokolwiek zapisać" (I-9), a na to
        # odpowiedź nie zależy od tego, czy plan już dotarł z chmury.
        try:
            clean = sanitize_params(raw_params, limits)
        except InvalidCommand as err:
            report["guards"] = {
                "status": "error",
                "notes": [{"invariant": err.invariant, "message": err.message}],
                "params": {},
            }
            return report

        guarded = apply_guards(
            clean,
            GuardContext(
                state=state,
                limits=limits,
                config=cfg,
                price_pln_kwh=slot_info.get("price_pln_kwh"),
                # R-1: suchy przebieg musi widzieć dokładnie tę samą intencję co realny,
                # inaczej diagnostyka pokazywałaby zapis, którego I-1 by nie przepuściło.
                action=slot_action,
                max_state_age_s=MAX_STATE_AGE_S,
                max_soc_jump_pp=MAX_SOC_JUMP_PP,
                max_soc_rate_pp_per_min=MAX_SOC_RATE_PP_PER_MIN,
                min_soc_jump_tolerance_pp=MIN_SOC_JUMP_TOLERANCE_PP,
            ),
        )
        report["guards"] = guarded.as_report()

        # `would_write` liczymy tylko wtedy, gdy naprawdę jest co zapisywać. Bez
        # harmonogramu guardy pracują na pustym zestawie i I-1 potrafi dorzucić
        # eco_soc = rezerwa — raportowanie tego jako „poszłoby do falownika"
        # byłoby kłamstwem.
        if raw_params and not guarded.rejected:
            # R-7: `async_apply` sprawdza I-8 (anty-oscylacja) PO guardach, a PRZED
            # throttlingiem — diagnose musi odwzorować dokładnie tę kolejność, inaczej
            # raportuje `status='success'` i pełne `would_write` tam, gdzie realny
            # `async_apply` zwróciłby THROTTLED. `would_allow`, NIGDY `allows`/`record`:
            # sam odczyt nie może zmienić stanu `DirectionLimiter` — to suchy przebieg
            # (RR-7: `allows` mutuje `self._history` przez `_prune`, mimo że jest
            # tylko sprawdzeniem — `would_allow` jest jej nie-mutującym odpowiednikiem).
            act = slot_action or infer_action(guarded.params)
            # RR-7: `allows()` woła `_prune`, które PRZYPISUJE `self._history` —
            # mutacja stanu, nawet gdy wpis jest i tak poza oknem. Suchy przebieg
            # musi użyć `would_allow`, jego nie-mutującego odpowiednika.
            allowed, direction_note = self._direction.would_allow(act, time.monotonic())
            if not allowed and direction_note is not None:
                report["guards"]["status"] = Status.THROTTLED.value
                report["guards"]["notes"].append(
                    {"invariant": direction_note.invariant, "message": direction_note.message}
                )
            else:
                # Podgląd throttlingu bez commitu — stan throttle'a zostaje nietknięty.
                writable, notes = self._throttle.filter(guarded.params, time.monotonic())
                # R-7: parametr bez zmapowanej encji w opcjach integracji nigdy nie
                # zostanie zapisany — `applier.apply_params` zgłosi błąd R-3 i pominie
                # go. `would_write` musi pokazywać tylko to, co naprawdę by poszło,
                # inaczej diagnoza kłamie dokładnie w sytuacji, którą ma tłumaczyć.
                unmapped = [key for key in writable if not _mapped_entity(key, options)]
                for key in unmapped:
                    writable.pop(key)
                    notes.append(
                        Note(
                            "R-3",
                            f"{key}: encja niezmapowana w opcjach integracji — zapis nie poszedłby",
                        )
                    )
                report["would_write"] = writable
                report["guards"]["notes"].extend(
                    {"invariant": n.invariant, "message": n.message} for n in notes
                )

        return report

    def _remember(
        self,
        source: str,
        result: GuardResult,
        executed: list[str],
        errors: list[dict[str, str]],
        act: Action,
    ) -> None:
        self._last = {
            "source": source,
            "at": datetime.now(timezone.utc).isoformat(),
            "executed": executed,
            "errors": errors,
            **result.as_report(),
        }

        # R-12 (reszta): notatki guardów (np. „guard I-9: …") i WARNING „Zapis
        # wstrzymany" podlegają TEJ SAMEJ regule anty-spamu co linia „Decyzja" niżej —
        # INFO/WARNING przy zmianie treści, DEBUG przy dokładnym powtórzeniu. Klucz
        # jest OSOBNY od `_last_decision`: treść notatek (np. inny komunikat I-9) może
        # się zmienić, mimo że `decision` (akcja/params/status/executed) zostaje ta
        # sama — i odwrotnie. Wcześniej ta pętla stała PRZED sprawdzeniem
        # `result.rejected` w `async_apply` i logowała bezwarunkowo na KAŻDYM ticku
        # ścieżki DEGRADED (reszta R-12 z rundy 2, RR-7-doc pkt „R-12 (reszta)").
        notes_key = tuple((n.invariant, n.message) for n in result.notes)
        notes_changed = notes_key != self._last_notes_key
        note_level = logging.INFO if notes_changed else logging.DEBUG
        for note in result.notes:
            _LOGGER.log(note_level, "[%s] guard %s: %s", source, note.invariant, note.message)
        if result.rejected:
            warn_level = logging.WARNING if notes_changed else logging.DEBUG
            _LOGGER.log(
                warn_level, "[%s] Zapis wstrzymany, status=%s", source, result.status.value
            )
        self._last_notes_key = notes_key

        # R-12: `_remember` jest jedynym miejscem wołanym z KAŻDEJ ścieżki wyjścia
        # `async_apply` — łącznie z wczesnymi returnami (błąd sanityzacji I-10,
        # odrzucenie I-1..I-9, throttled I-8) — dlatego log decyzji (i anty-spam
        # `_last_decision`) siedzi tutaj, a nie na końcu `async_apply`. Wcześniej ten
        # blok stał wyłącznie na "szczęśliwej ścieżce": powrót do normalnej pracy po
        # DEGRADED nie generował żadnego INFO (bo `_last_decision` zostawało
        # zamrożone na wartości sprzed awarii — identyczna decyzja po awarii
        # wyglądała jak "bez zmian"), a sama ścieżka DEGRADED nie miała żadnej
        # ochrony przed spamem (WARNING + INFO co przebieg pętli, czyli co 60 s).
        #
        # Klucz decyzji uwzględnia `executed`, nie tylko `params`/`status` —
        # przebieg, który policzył te same parametry, ale NIC nie zapisał (np. I-6
        # odfiltrował wszystko po stronie niezmienionych wartości), musi być
        # odróżnialny na poziomie INFO od przebiegu, który faktycznie zapisał.
        decision = (
            f"{act.value}|{sorted(result.params.items())}|{result.status.value}|"
            f"executed={sorted(executed)}"
        )
        if decision != self._last_decision:
            _LOGGER.info(
                "[%s] Decyzja: %s, status=%s, zapisano=%s",
                source, act.value, result.status.value, executed or "nic",
            )
            self._last_decision = decision
        else:
            _LOGGER.debug("[%s] Decyzja bez zmian: %s", source, act.value)

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
            # R-14 (reszta): kopia obronna — ten sam błąd co pierwotne R-14
            # w `async_diagnose` (już naprawione), tylko w tej property nikt go
            # dotąd nie domknął. Żywa referencja do `self._last` pozwoliłaby
            # konsumentowi modyfikującemu odpowiedź w miejscu podmienić stan
            # wewnętrzny executora. Głęboka kopia, bo `self._last` zawiera
            # zagnieżdżone listy (`executed`, `errors`, `notes`).
            "last": copy.deepcopy(self._last),
        }


__all__ = ["VolterExecutor"]
