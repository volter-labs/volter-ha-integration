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
    MAX_STATE_AGE_S,
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
from .ha_state import read_device_state, read_inverter_limits
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
        self._direction = DirectionLimiter(max_changes_per_hour=MAX_DIRECTION_CHANGES_PER_HOUR)
        self._prev_soc: float | None = None
        self._last: dict[str, Any] = {}
        self._last_decision: str | None = None

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
        slot: Slot | None = None,
        _remapped: bool = False,
    ) -> GuardResult:
        """Jedyna droga do falownika: sanityzacja → guardy → throttle → zapis.

        `slot` podaje ścieżka harmonogramu. Jest potrzebny wyłącznie po to, żeby przy
        `GuardResult.forced_action` dało się przemapować slot na tryb bezpieczny (R-1);
        `_remapped` gwarantuje, że dzieje się to najwyżej raz.
        """
        options = dict(self._entry.options)
        limits = read_inverter_limits(self.hass, options)
        state = read_device_state(self.hass, options, self._prev_soc)
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
        )
        result = apply_guards(clean, ctx)
        # R-5: baseline SoC dla NASTĘPNEGO przebiegu aktualizujemy WYŁĄCZNIE z odczytów,
        # które same przeszły I-9. Dawniej ta linia biegła bezwarunkowo — odczyt odrzucony
        # jako fizycznie niemożliwy skok stawał się zaufanym punktem odniesienia już w
        # kolejnym ticku, więc I-9 chroniło przez dokładnie jeden przebieg.
        if not any(note.invariant == "I-9" for note in result.notes):
            self._prev_soc = state.soc if state.soc is not None else self._prev_soc

        for note in result.notes:
            _LOGGER.info("[%s] guard %s: %s", source, note.invariant, note.message)

        if result.rejected:
            _LOGGER.warning("[%s] Zapis wstrzymany, status=%s", source, result.status.value)
            # R-12: DEGRADED/ERROR/DUPLICATE to wczesny return — bez aktualizacji
            # `_last_decision` tutaj ścieżka DEGRADED nie ma żadnej ochrony przed
            # anty-spamem (loguje WARNING+INFO co przebieg pętli), a powrót do sukcesu
            # po awarii z identyczną decyzją co przed nią wygląda jak "bez zmian".
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
                forced = await self.async_apply(
                    slot_to_params(forced_slot, rated_power_w=rated),
                    price_pln_kwh=price_pln_kwh,
                    action=result.forced_action,
                    source=source,
                    slot=forced_slot,
                    _remapped=True,
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
        act = action or infer_action(result.params)
        allowed, note = self._direction.allows(act, now_ts)
        if not allowed and note is not None:
            result.status = Status.THROTTLED
            result.note(note.invariant, note.message)
            _LOGGER.info("[%s] guard %s: %s", source, note.invariant, note.message)
            # R-12: THROTTLED to też wczesny return — musi zaktualizować `_last_decision`
            # tak samo jak ścieżka normalna.
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
            executed, errors = await apply_params(self.hass, options, writable)
            # N-4: raport do chmury musi mówić prawdę — `executed` to to, co naprawdę
            # poszło do falownika, nie to, co przeszło guardy (`result.params`).
            result.executed = executed
            # R-9: bez tego przypisania realne błędy per-encja (m.in. niezmapowana
            # encja z R-3) ginęły — command_handler raportował do chmury errors=[]
            # na sztywno, niezależnie od tego, co faktycznie zawiodło.
            result.errors = errors

            self._throttle.commit({k: writable[k] for k in executed if k in writable}, now_ts)
            self._direction.record(act, now_ts)

            if errors and not executed:
                result.status = Status.ERROR
            elif errors:
                result.status = Status.PARTIAL

        # R-12: log decyzji (anty-spam + `_last_decision`) przeniesiony do `_remember`,
        # żeby DZIAŁAŁ TAKŻE na wczesnych returnach (I-10, I-1..I-9, I-8) — patrz
        # komentarz przy `_remember`.
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
          * `self._prev_soc` i `self._last` pozostają bez zmian (to suchy przebieg,
            nie przebieg — nie wolno mu zafałszować kolejnego realnego wykonania).
        """
        options = dict(self._entry.options)
        limits = read_inverter_limits(self.hass, options)
        state = read_device_state(self.hass, options, self._prev_soc)
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
            "last": self._last,
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
            # `async_apply` zwróciłby THROTTLED. `allows`, NIGDY `record`: sam odczyt
            # nie może zmienić stanu `DirectionLimiter` — to suchy przebieg.
            act = slot_action or infer_action(guarded.params)
            allowed, direction_note = self._direction.allows(act, time.monotonic())
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
            "last": self._last,
        }


__all__ = ["VolterExecutor"]
