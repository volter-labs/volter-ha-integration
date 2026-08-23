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
import math
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
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_RATED_POWER_W,
    DEFAULT_SOC_RESERVE,
    EXECUTOR_INTERVAL,
    MAX_DIRECTION_CHANGES_PER_HOUR,
    MAX_SOC_JUMP_PP,
    MAX_SOC_RATE_PP_PER_MIN,
    MAX_STATE_AGE_S,
    MIN_SOC_JUMP_TOLERANCE_PP,
    OPT_ENTITY_EXPORT_LIMIT_SWITCH,
    OPT_CONTROL_ENABLED,
    OPT_RATED_POWER_W,
    OPT_SOC_RESERVE,
    OPT_USER_MODE,
    RESERVE_HYSTERESIS_PP,
    RESERVE_LATCH_ENGAGED_MIN_S,
    RESERVE_LATCH_RELEASED_MIN_S,
    STOP_WRITE_TIMEOUT_S,
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
    ReserveHysteresis,
    Status,
    UserConfig,
    WritePermit,
    WriteThrottle,
    apply_guards,
    infer_action,
    sanitize_params,
)
from .ha_state import ParamBoundsCache, read_device_state, read_inverter_limits
from .mappers import slot_to_params
from .schedule import Fallback, Schedule, Slot, akcja_efektywna

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


def _cena_zaufana(value: Any) -> float | None:
    """S-2c: cena wyciągnięta z aktywnego planu nie może wywalić toru zapisu.

    Ta wartość NIE pochodzi od wołającego — bierze ją sam executor z `self._schedule`
    (gałąź R-6, żeby I-4 broniło też komendy operatora). Gdy plan jest zatruty,
    porównanie `cena <= 0` w guardach rzuca `TypeError` na ścieżce, którą awaryjny
    fallback musiał właśnie ratować — czyli ratunek wybuchał z tego samego powodu co
    awaria. `None` znaczy „ceny nie znam": I-4 jest wtedy ślepe, ale slot fallbacku
    i tak ma `export_allowed=False`, więc brak wiedzy o cenie nie otwiera eksportu.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _LOGGER.warning(
            "Cena z aktywnego planu ma zły typ (%r) — I-4 pracuje bez ceny [S-2]", value
        )
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        _LOGGER.warning("Cena z aktywnego planu nie jest skończona (%r) — I-4 bez ceny", value)
        return None
    return numeric


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
        # S-5: uchwyt do TRWAJĄCEJ sekwencji zapisu (chronionej `asyncio.shield`).
        # Bez niego `async_stop` nie miał czego pilnować i wyładowywał integrację
        # w środku PARAM_ORDER — falownik zostawał w nowym trybie ze starymi limitami.
        self._inflight: asyncio.Future[GuardResult] | None = None
        # S-5b: przepustka TRWAJĄCEJ sekwencji — jedyny sposób, żeby ją zatrzymać bez
        # anulowania jej w środku service calla. `asyncio.shield` chronił sekwencję tak
        # skutecznie, że przeżywała `async_stop` i pisała do falownika po wyładowaniu
        # integracji, ścigając się z nowym executorem powołanym przez reload.
        self._przepustka: WritePermit | None = None
        # S-5b: po rozpoczęciu `async_stop` executor nie jest już właścicielem falownika.
        # Przebieg, który czekał na `_write_lock`, nie może po wyładowaniu zacząć NOWEJ
        # sekwencji — powołałby świeżą przepustkę, której nikt już nie odbierze, czyli
        # obszedłby tę naprawę bokiem.
        self._zatrzymany = False
        self._direction = DirectionLimiter(max_changes_per_hour=MAX_DIRECTION_CHANGES_PER_HOUR)
        # S-4: zatrzask progu rezerwy (I-1). Stan MIĘDZY przebiegami, dokładnie jak
        # `_direction` i `_throttle` — bez pamięci histereza jest tylko przesuniętym progiem.
        # S-4b: pasmo to za mało — zatrzask ma też minimalny CZAS TRWANIA stanu, bo samo
        # pasmo przepuszczało w całości każdą oscylację od niego szerszą.
        self._reserve = ReserveHysteresis(
            band_pp=RESERVE_HYSTERESIS_PP,
            engaged_min_s=RESERVE_LATCH_ENGAGED_MIN_S,
            released_min_s=RESERVE_LATCH_RELEASED_MIN_S,
        )
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
        # S-5b: flaga PRZED czekaniem, nie po nim. `async_stop` nie trzyma `_write_lock`,
        # więc przebieg stojący w kolejce po zamek (tick złapany w locie albo komenda
        # z chmury) wystartowałby w środku wyładowania i powołał świeżą sekwencję
        # z przepustką, której nikt by już nie odebrał — dokładnie obejście tej naprawy.
        self._zatrzymany = True
        # S-5: zatrzymanie executora MUSI poczekać na trwający zapis — inaczej HA
        # wyładowuje integrację w środku sekwencji PARAM_ORDER (ścieżka realna przy
        # KAŻDEJ zmianie opcji: update listener -> async_reload -> async_unload_entry).
        await self._poczekaj_na_zapis("executor zatrzymany — wyładowanie integracji")
        # S-5b: bezwarunkowo, na wyjściu — po `async_stop` żadna sekwencja nie ma prawa
        # dotknąć falownika, niezależnie od tego, czy czekanie skończyło się w limicie.
        # To jest granica własności: od tej chwili falownikiem rządzi executor powołany
        # przez reload, a stara sekwencja pisałaby mu pod ręką (S-1 o piętro wyżej).
        if self._przepustka is not None:
            self._przepustka.revoke("executor zatrzymany")
        _LOGGER.info("Executor zatrzymany")

    async def _poczekaj_na_zapis(self, powod: str) -> None:
        """S-5/S-5b: poczekaj na sekwencję zapisu, a gdy nie zdąży — ODBIERZ JEJ PRAWO.

        Koszt `asyncio.shield` jest realny: opóźnia wyładowanie integracji o czas
        jednej sekwencji nastaw. Jest to jednak koszt OGRANICZONY
        (`STOP_WRITE_TIMEOUT_S`), podczas gdy alternatywa — przerwanie zapisu w połowie
        — zostawia falownik np. w `eco_charge` z limitami z poprzedniego slotu, czyli
        w stanie, któremu ma zapobiegać PARAM_ORDER.

        S-5b/S-5c — DLACZEGO dwa etapy: samo odpuszczenie czekania (pierwotne S-5)
        zostawiało sekwencję i przy życiu, i przy prawie do zapisu. Skutek zależał od
        wołającego: w `async_stop` sekwencja pisała do falownika PO wyładowaniu
        integracji (S-5b), a w `async_apply` przeplatała się z nowym przebiegiem, czyli
        znosiła serializację S-1 wewnątrz jednego executora (S-5c). Limit czasu jest
        więc potrzebny, ale sam w sobie nie może być furtką: po jego upływie ODBIERAMY
        przepustkę i czekamy drugi raz. Sekwencja przerywa się na najbliższej granicy
        między nastawami, więc drugie czekanie jest z reguły natychmiastowe.

        Gdy i drugie czekanie minie, sekwencja wisi WEWNĄTRZ jednego service calla,
        którego i tak nie da się cofnąć — ale bez przepustki nie wykona już żadnej
        następnej nastawy. Tyle da się obiecać uczciwie i dokładnie tyle obiecujemy.
        """
        zadanie = self._inflight
        przepustka = self._przepustka
        if zadanie is None or zadanie.done():
            return
        if await self._czekaj_na_zadanie(zadanie):
            return

        _LOGGER.error(
            "Zapis do falownika nie zakończył się w %.0fs (%s) — odbieram sekwencji "
            "prawo zapisu [S-5b]", STOP_WRITE_TIMEOUT_S, powod,
        )
        if przepustka is not None:
            przepustka.revoke(powod)
        if not await self._czekaj_na_zadanie(zadanie):
            _LOGGER.error(
                "Sekwencja nadal nie oddała sterowania — wisi wewnątrz jednego service "
                "calla. Przepustka odebrana, więc kolejnych nastaw nie wykona [S-5b]"
            )

    async def _czekaj_na_zadanie(self, zadanie: asyncio.Future[GuardResult]) -> bool:
        """Czekaj z limitem. `True` = zadanie się zakończyło, `False` = limit minął."""
        try:
            # `shield` w środku, bo `wait_for` po timeoucie anuluje to, na co czeka —
            # a my chcemy odpuścić CZEKANIE, nie przerwać zapis w połowie.
            await asyncio.wait_for(asyncio.shield(zadanie), timeout=STOP_WRITE_TIMEOUT_S)
        except TimeoutError:
            return False
        # `asyncio.CancelledError` świadomie NIE jest tu łapane: to `BaseException`,
        # więc `except Exception` niżej go nie tknie i poleci dalej. Anulowanie samego
        # zatrzymywania musi dojść do wołającego.
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Trwający zapis zakończył się błędem: %s [S-5]", err)
        return True

    # ── harmonogram ──────────────────────────────────────────────────────────

    async def async_set_schedule(self, raw: dict[str, Any]) -> GuardResult:
        """Przyjmij i utrwal harmonogram z chmury, potem wykonaj natychmiast.

        S-2: kolejność w tej metodzie jest ochroną, nie stylem. Najpierw PEŁNA
        walidacja typów (`Schedule.from_dict` jest fail-closed i rzuca
        `InvalidSchedule`), potem utrwalenie, i dopiero na końcu podmiana aktywnego
        planu w pamięci procesu. Wcześniej plan lądował w `Store` bez żadnej kontroli
        typów pól slotu, więc pojedynczy błąd serializacji po stronie chmury (liczba
        jako string) zatruwał pamięć trwałą: po restarcie HA każdy tick rzucał
        wyjątkiem PRZED decyzją, urządzenie nie pisało nic i nie wchodziło nawet
        w fallback I-5.

        S-2: podmiana `self._schedule` PO udanym `async_save`, a nie przed — gdyby
        zapis do `Store` zawiódł, aktywny plan i pamięć trwała muszą zostać zgodne
        (stary plan w obu miejscach), zamiast rozjechać się na czas do restartu.
        """
        schedule = Schedule.from_dict(raw)
        await self._store.async_save(schedule.to_dict())
        self._schedule = schedule
        if not schedule.slots:
            # S-3: pusty plan JEST legalny (chmura mówi „nie mam planu"), ale kasuje
            # aktywny harmonogram — to musi być widoczne w logu HA, bo z zewnątrz
            # wygląda identycznie jak awaria łącza z chmurą.
            _LOGGER.warning(
                "Harmonogram %s nie ma ani jednego slotu — aktywny plan wycofany, "
                "urządzenie wchodzi w fallback [I-5]",
                schedule.schedule_id or "(bez id)",
            )
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
            # S-5b: przebieg, który czekał na zamek przez całe wyładowanie integracji,
            # nie zaczyna nowej sekwencji — falownik ma już innego właściciela.
            # Sprawdzenie jest ZA zamkiem, bo dopiero tu wiadomo, że nikt inny nie pisze.
            # Świadomie BEZ `_remember`, mimo reguły R-12 („każde wyjście przez log
            # decyzji"): `_last` i anty-spam to stan wyładowanego już executora, którego
            # nikt nie odczyta, a nadpisanie go zatarłoby ślad po tym, co naprawdę
            # poszło do falownika przed zatrzymaniem. Widoczność daje WARNING niżej.
            if self._zatrzymany:
                _LOGGER.warning(
                    "[%s] Zapis pominięty — executor zatrzymany, falownikiem rządzi "
                    "już inny właściciel [S-5b]", source,
                )
                wynik = GuardResult(params={}, status=Status.ERROR)
                wynik.note("S-5b", "executor zatrzymany — zapis nie należy już do niego")
                return wynik
            # S-5: sekwencja zapisu porzucona przez ANULOWANEGO wołającego leci dalej
            # pod `shield` i już nie trzyma zamka — nowy przebieg musi poczekać na jej
            # koniec, inaczej wracamy do przeplotu z S-1 (tyle że tylko po anulowaniu).
            # S-5c: to czekanie ma limit czasu, więc SAMO w sobie nie wystarcza —
            # `_poczekaj_na_zapis` odbiera po nim przepustkę, żeby furtka czasowa nie
            # przepuszczała nowego przebiegu obok wciąż piszącej sekwencji.
            await self._poczekaj_na_zapis(f"nowy przebieg ({source}) przejmuje tor zapisu")
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
            # S-2c: sięgnięcie po cenę do aktywnego planu jest tu plumbingiem, a nie
            # decyzją — nie wolno mu przewrócić całego toru zapisu, bo to ta sama
            # ścieżka, którą ratuje awaryjny fallback po zatrutym planie.
            try:
                current_slot, _ = self._schedule.effective_slot(datetime.now(timezone.utc))
                effective_price = _cena_zaufana(current_slot.price_pln_kwh)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Nie udało się odczytać ceny z aktywnego planu (%s) — I-4 bez ceny", err
                )
                effective_price = None

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
            # S-4: REALNY przebieg dostaje żywy zatrzask — tylko on ma prawo go
            # zakładać i zwalniać (suchy przebieg dostaje kopię, patrz `async_diagnose`).
            reserve_hysteresis=self._reserve,
            # S-4b: zatrzask mierzy czas trwania własnego stanu, więc musi dostać
            # DOKŁADNIE ten sam zegar co I-6 i I-8 — jedno źródło czasu na przebieg.
            now_s=time.monotonic(),
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
                # U-1: przemapowanie musi zdjąć TAKŻE kierunek opisowy i moc, nie tylko
                # `action`. Od rozszerzenia kontraktu (Task 13) mapper czyta kierunek
                # również z `charge_source`/`discharge_purpose`, więc samo podmienienie
                # trybu zostawiałoby slot, który wraca do falownika jako
                # `eco_discharge` tylnymi drzwiami — czyli I-1 zduszone przez guard
                # wskrzeszone przez pole opisowe. `power_w` znika razem z kierunkiem,
                # bo wymuszona autokonsumpcja to jawne „bateria decyduje sama":
                # nastawa mocy bez komendy kierunku jest dokładnie wadą U-1.
                forced_slot = replace(
                    slot,
                    action=result.forced_action,
                    charge_source=None,
                    discharge_purpose=None,
                    power_w=None,
                )
                rated = float(options.get(OPT_RATED_POWER_W, DEFAULT_RATED_POWER_W))
                # RR-10 (ustalenie 2. kontrolera z rundy 2): to był `_LOGGER.warning`
                # BEZWARUNKOWY, poza anty-spamem `_remember` — 60 WARNING/h w stanie
                # ustabilizowanym (cel RR-5 osiągnięty tylko dla linii "Decyzja").
                # Jako `Note` trafia do `result.notes`, a te zaraz niżej scalają się
                # w `forced.notes` i przechodzą przez `_remember` na końcu tej metody
                # — czyli TĘ SAMĄ regułę anty-spamu (INFO/DEBUG) co pozostałe notatki
                # guardów. `key` stały (treść i tak jest stabilna dopóki `forced_action`
                # się nie zmienia — same wartości enuma), ale jawny dla spójności
                # z resztą mechanizmu identity-key (RR-10).
                result.note(
                    "I-1",
                    f"Guardy wymusiły akcję {result.forced_action.value} zamiast "
                    f"{(action.value if action else '?')} — przemapowuję slot",
                    key="forced_remap",
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

        if not writable:
            result.executed = []
            # R-12: log decyzji (anty-spam + `_last_decision`) siedzi w `_remember`,
            # żeby DZIAŁAŁ TAKŻE na wczesnych returnach (I-10, I-1..I-9, I-8).
            # RR-5: gałąź WEWNĘTRZNA (przemapowanie po forced_action) nie loguje sama.
            if _report:
                self._remember(source, result, [], [], act)
            return result

        # WYŁĄCZNIK STEROWANIA — ostatnia brama przed dotknięciem falownika.
        # Stoi TU, a nie na wejściu `async_apply`, celowo: guardy, throttle i wybór
        # slotu policzyły się w całości, więc log decyzji i `volter.diagnose` pokazują
        # dokładnie to, co POSZŁOBY do falownika. Wyłącznik odbiera prawo zapisu,
        # nie widoczność — inaczej nie dałoby się przygotować instalacji na sucho.
        # Domyślnie WYŁĄCZONY, bo do czasu potwierdzenia mapowania trybów na żywym
        # falowniku (Etap 3) jedyną barierą przed pierwszym zapisem był przypadek:
        # niezgodność nazw trybów łapana przez I-10.
        if not bool(options.get(OPT_CONTROL_ENABLED, DEFAULT_CONTROL_ENABLED)):
            result.executed = []
            # THROTTLED, nie ERROR: nic nie poszło, ale nic też nie zawiodło —
            # a dedup (R-2) nie zapamiętuje tego statusu, więc komenda z chmury
            # pozostaje retryowalna po włączeniu sterowania.
            result.status = Status.THROTTLED
            result.note(
                "STEROWANIE",
                "sterowanie wyłączone w opcjach — pominięto zapis: "
                + ", ".join(f"{k}={v}" for k, v in sorted(writable.items())),
            )
            if _report:
                self._remember(source, result, [], [], act)
            return result

        # S-5: właściwa sekwencja nastaw idzie w OSOBNYM zadaniu i jest awaitowana
        # przez `asyncio.shield`. Anulowanie tego przebiegu (przeładowanie integracji
        # po zmianie opcji) przerywa CZEKANIE, ale nie sam zapis — inaczej falownik
        # zostawał w nowym trybie ze starymi limitami, czyli w stanie, któremu ma
        # zapobiegać PARAM_ORDER, i nie było tego widać ani w HA, ani w chmurze.
        # Świadomie `asyncio.ensure_future`, a nie `hass.async_create_task`: HA anuluje
        # swoje śledzone zadania przy wyładowaniu, czyli robiłoby dokładnie to, przed
        # czym broni to ustalenie. Zadanie jest za to pilnowane przez `_inflight`
        # i awaitowane w `async_stop`, więc nie jest porzucone.
        # S-5b: przepustka powstaje razem z sekwencją i jest jej jedyną smyczą.
        # Sekwencja trzyma ją sama (przeżywa wyzerowanie `self._przepustka`), więc
        # odebranie prawa działa także wtedy, gdy uchwyt w executorze już nie istnieje.
        przepustka = WritePermit()
        self._przepustka = przepustka
        zadanie = asyncio.ensure_future(
            self._async_zapisz_sekwencje(
                options, writable, result, act, now_ts, source, _report, przepustka
            )
        )
        # S-5: zadanie dziedziczy nazwę wołającego, bo sekwencja zapisu JEST jego
        # kontynuacją — log HA, traceback i „kto pisał do falownika" mają dalej
        # wskazywać na pętlę, która zapis zleciła, a nie na anonimowy `Task-17`.
        biezace = asyncio.current_task()
        if biezace is not None:
            zadanie.set_name(biezace.get_name())
        self._inflight = zadanie
        zadanie.add_done_callback(self._zwolnij_zapis)
        try:
            return await asyncio.shield(zadanie)
        except asyncio.CancelledError:
            _LOGGER.warning(
                "[%s] Przebieg anulowany w trakcie zapisu — sekwencja nastaw dokańcza "
                "się pod ochroną [S-5]", source,
            )
            self._stempel_anulowania(source, act, result, zadanie)
            # Anulowania NIE WOLNO połknąć: zamiana go na zwykły wynik zablokowałaby
            # zatrzymywanie Home Assistanta.
            raise

    def _zwolnij_zapis(self, zadanie: asyncio.Future) -> None:
        """S-5: uchwyt do trwającego zapisu żyje dokładnie tyle, ile sam zapis."""
        if self._inflight is zadanie:
            self._inflight = None
            # S-5b: przepustka idzie w parze z uchwytem — odbieranie prawa zapisu
            # sekwencji, która już się zakończyła, tylko myliłoby diagnozę.
            self._przepustka = None

    def _stempel_anulowania(
        self,
        source: str,
        act: Action,
        result: GuardResult,
        zadanie: asyncio.Future,
    ) -> None:
        """S-5: anulowanie MUSI zostawić ślad w `_last` — i to ślad PRAWDZIWY.

        Sonda E pokazała `diagnostics['last'] == {}`: stan połowiczny falownika nie
        był widoczny nigdzie, więc pytanie „co poszło do falownika" nie miało
        odpowiedzi dokładnie wtedy, gdy poszło coś nietypowego. Stempel jest odroczony
        do zakończenia chronionego zadania, bo dopiero wtedy `result.executed` mówi,
        co naprawdę zostało zapisane; `result` to ten sam obiekt, który zadanie
        uzupełnia, więc nie trzeba niczego przepisywać.
        """

        def _stempel(_zadanie: asyncio.Future | None = None) -> None:
            try:
                result.note(
                    "S-5",
                    "przebieg anulowany w trakcie zapisu — sekwencja nastaw dokończona "
                    "pod ochroną (asyncio.shield)",
                )
                self._remember(source, result, list(result.executed), list(result.errors), act)
                self._last["cancelled"] = True
            except Exception as err:  # noqa: BLE001
                # Ślad po anulowaniu nie może sam wywalić callbacku pętli zdarzeń.
                _LOGGER.error("Nie udało się zapisać śladu po anulowaniu: %s [S-5]", err)

        if zadanie.done():
            _stempel()
        else:
            zadanie.add_done_callback(_stempel)

    async def _async_zapisz_sekwencje(
        self,
        options: dict[str, Any],
        writable: dict[str, Any],
        result: GuardResult,
        act: Action,
        now_ts: float,
        source: str,
        _report: bool,
        przepustka: WritePermit,
    ) -> GuardResult:
        """S-5: NIEROZERWALNA sekwencja zapisu + jej księgowanie.

        Wszystko, co musi zajść razem, siedzi w jednym zadaniu: zapis w PARAM_ORDER,
        commit throttle'a I-6 i zapis kierunku I-8. Gdyby księgowanie zostało po
        stronie anulowanego wołającego, throttle nie zapamiętałby wartości, które
        FIZYCZNIE poszły do falownika — i kolejny przebieg zapisałby je ponownie,
        czyli anulowanie kosztowałoby dodatkowy zapis do pamięci nieulotnej (S-4).

        S-5b: „nierozerwalna" znaczy „nie do przerwania W ŚRODKU service calla", a NIE
        „nie do zatrzymania". Sekwencja pyta o przepustkę przed każdą nastawą, więc
        właściciel może ją zatrzymać na granicy między nastawami — w jedynym miejscu,
        w którym przerwanie da się uczciwie rozliczyć.
        """
        # RR-3: `forced_params` rozstrzyga w `apply_params`, czy brak zmapowanej
        # encji dla danego parametru jest błędem (zabezpieczenie guarda) czy tylko
        # widoczną notą (normalny parametr planu, opcjonalna encja świadomie
        # niezmapowana) — patrz `GuardResult.forced_params` i docstring `apply_params`.
        executed, errors, skip_notes = await apply_params(
            self.hass, options, writable, forced_params=result.forced_params,
            permit=przepustka,
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
        # więc chmura i log HA nadal je widzą.
        for skip in skip_notes:
            result.note("RR-3", f"{skip['entity']}: {skip['note']}")

        if przepustka.aborted:
            # S-5b: przerwanie w połowie łamie PARAM_ORDER (falownik w nowym trybie ze
            # starymi limitami) i musi być WIDOCZNE — pierwotne S-5 istniało właśnie
            # dlatego, że ten stan nie był widoczny nigdzie. Nastawy pominięte siedzą
            # już w `errors` (`applier`), tu dokładamy jedno zdanie o powodzie.
            result.note(
                "S-5b",
                f"sekwencja nastaw przerwana ({przepustka.reason}) — PARAM_ORDER "
                f"złamany, do falownika doszło tylko {executed or 'nic'}",
            )

        # S-5b: commitujemy WYŁĄCZNIE `executed`, więc nastawy przerwane przez
        # odebranie przepustki nie stają się dla I-6 „wartością bez zmiany". Bez tego
        # kolejny przebieg nigdy by ich nie dopisał — to ten sam trwały rozjazd
        # plan/rzeczywistość, który zamyka S-1.
        self._throttle.commit({k: writable[k] for k in executed if k in writable}, now_ts)
        self._direction.record(act, now_ts)

        if errors and not executed:
            result.status = Status.ERROR
        elif errors:
            result.status = Status.PARTIAL
        elif writable and not executed:
            # RR-9 (ustalenie 2. kontrolera z rundy 2): żądaliśmy zapisu
            # (`writable` niepuste — inaczej ta sekwencja w ogóle by się nie
            # uruchomiła, patrz `if not writable: return` wyżej) i NIC nie poszło
            # do falownika, a mimo to `errors` jest puste — bo WSZYSTKIE parametry
            # trafiły w gałąź "encja niezmapowana (opcjonalna)" (RR-3: nota, nie
            # błąd, bo żaden z nich nie był w `forced_params`). Gdyby choć jeden
            # był wymuszony przez guard bezpieczeństwa, `apply_params` dałby dla
            # niego błąd i trafilibyśmy w gałąź wyżej — ta gałąź jest więc
            # wyłącznie dla "cały zestaw to zwykłe, świadomie niezmapowane
            # parametry planu". `SUCCESS` tu kłamie: plan zażądał zapisu i ZERO
            # dotarło do falownika. Nie wracamy do trwałego `ERROR` z pierwotnego
            # R-3 (to była regresja RR-3 miała naprawić) — `PARTIAL` mówi dosłowną
            # prawdę "plan nie został w pełni wykonany" i jest spójny z
            # `command_handler._remember_if_effective` (R-2), które już traktuje
            # `PARTIAL` z pustym `executed` jako "nic nie poszło, retry legalny".
            result.status = Status.PARTIAL

        # RR-5: gałąź WEWNĘTRZNA (przemapowanie po forced_action) nie loguje sama —
        # bez tej bramki przebieg wewnętrzny (status np. `success`) i przebieg
        # zewnętrzny scalony (status np. `partial`) wołały `_remember` osobno z
        # DWOMA różnymi kluczami decyzji, które nadpisywały się nawzajem (sonda P2).
        if _report:
            self._remember(source, result, executed, errors, act)
        return result

    # ── pętla ────────────────────────────────────────────────────────────────

    async def _async_tick(self, _now: datetime | None = None) -> None:
        await self._async_execute_now()

    async def _async_execute_now(self) -> GuardResult:
        """S-2c: pętla wykonawcza nie może zostawić urządzenia BEZ DECYZJI.

        Niezależna warstwa obrony, celowo dublująca walidację z `Schedule.from_dict`:
        nawet gdyby cokolwiek przeciekło przez parser (albo wybuchło w mapperze,
        odczycie encji czy guardach), wyjątek leciał dotąd PRZED wyborem akcji, więc
        falownik zostawał na ostatniej nastawie bez żadnego sygnału i BEZ fallbacku
        I-5 — dokładnie ten tryb awarii opisuje S-2. Wyjątek ma więc prowadzić do
        bezpiecznej decyzji, a nie do ciszy.
        """
        try:
            return await self._async_execute_planned()
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception(
                "Wyjątek w torze wykonania harmonogramu (%s) — wchodzę w awaryjny "
                "fallback [I-5]", err,
            )
            return await self._async_awaryjny_fallback(err)

    async def _async_awaryjny_fallback(self, powod: BaseException) -> GuardResult:
        """S-2c: ostatnia deska ratunku — self_consume + rezerwa użytkownika (I-5).

        Świadomie NIE korzysta z `self._schedule` ani z `schedule.fallback`: skoro
        coś w torze planu właśnie wybuchło, plan jest podejrzany i jedyną zaufaną
        wiedzą jest konfiguracja użytkownika. Cała metoda jest opakowana w drugi
        `try`, bo nawet odczyt opcji może być źródłem wyjątku — a metoda, która
        istnieje po to, żeby wyjątek nie uciszył urządzenia, sama nie może rzucać.
        """
        try:
            options = dict(self._entry.options)
            reserve = float(options.get(OPT_SOC_RESERVE, DEFAULT_SOC_RESERVE))
            fallback = Fallback(action=Action.SELF_CONSUME, soc_reserve=reserve)
            slot = fallback.as_slot(datetime.now(timezone.utc))
            rated = float(options.get(OPT_RATED_POWER_W, DEFAULT_RATED_POWER_W))
            result = await self.async_apply(
                slot_to_params(slot, rated_power_w=rated),
                price_pln_kwh=slot.price_pln_kwh,
                # U-6: ten sam sposób wyprowadzenia intencji co na ścieżce planu —
                # slot fallbacku dziś kierunku nie niesie, ale gałąź ratunkowa nie może
                # być jedynym miejscem, które liczy akcję po staremu.
                action=akcja_efektywna(slot),
                source="fallback-awaryjny",
                slot=slot,
            )
            # Raport do chmury musi mówić, że plan NIE został wykonany — sam zapis
            # fallbacku mógł się udać, ale wykonanie harmonogramu się nie udało.
            # `self._last` zostaje przy prawdzie o samym zapisie (SUCCESS), bo to
            # dwie różne informacje: co poszło do falownika vs czy plan się wykonał.
            result.note("I-5", f"awaryjny fallback po wyjątku w torze wykonania: {powod}")
            if result.status is Status.SUCCESS:
                result.status = Status.PARTIAL
            return result
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Awaryjny fallback [I-5] też się nie powiódł: %s", err)
            wynik = GuardResult(params={}, status=Status.ERROR)
            wynik.note("I-5", f"awaryjny fallback nie powiódł się: {err}")
            return wynik

    async def _async_execute_planned(self) -> GuardResult:
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
                # U-6: jak wyżej — jedna reguła wyprowadzania intencji na wszystkich
                # ścieżkach wykonania, żeby żadna nie została z własną wersją prawdy.
                action=akcja_efektywna(slot),
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
            # U-6: do guardów idzie kierunek EFEKTYWNY, nie surowe `slot.action`.
            # Chmura zostawia `mode='self_consume'` dla 41% slotów z mocą i wiezie
            # kierunek w `discharge_purpose`/`charge_source`, więc `slot.action` mówił
            # guardom „nic się nie dzieje" dokładnie tam, gdzie bateria się rozładowuje:
            # I-1 nie dusiło rozładowania poniżej rezerwy (R-1 wskrzeszone nową ścieżką),
            # a I-8 nie liczyło zmian kierunku (U-7). Rozstrzyga o tym funkcja czysta,
            # z której korzysta też mapper — jedno źródło prawdy, bo rozjazd tych dwóch
            # miejsc to natychmiastowy powrót U-6.
            action=akcja_efektywna(slot),
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
            # U-6/R-7: suchy przebieg musi liczyć intencję DOKŁADNIE tak samo jak realny
            # tor zapisu — inaczej diagnoza pokazuje `forced_action=None` i pełne
            # `would_write` tam, gdzie realny przebieg dusi rozładowanie przez I-1.
            slot_action = akcja_efektywna(slot)
            rated = float(options.get(OPT_RATED_POWER_W, DEFAULT_RATED_POWER_W))
            raw_params = slot_to_params(slot, rated_power_w=rated)
            slot_info = {
                "source": "fallback" if is_fallback else "schedule",
                "from": slot.start.isoformat(),
                "to": slot.end.isoformat(),
                "action": slot.action.value,
                # U-6: `action` to DOSŁOWNE pole planu, a `action_efektywna` to intencja,
                # na której naprawdę pracują guardy. Dla 41% slotów z mocą te dwie
                # wartości się różnią (`self_consume` vs `discharge`) i bez pokazania
                # obok siebie nie da się na żywym sprzęcie odróżnić „plan tak mówił"
                # od „urządzenie tak zrozumiało" — a po to ten raport istnieje.
                "action_efektywna": slot_action.value,
                "soc_target": slot.soc_target,
                "price_pln_kwh": slot.price_pln_kwh,
                # U-1: bez tych trzech pól raport nie odróżnia „slot bez kierunku" od
                # „kierunek przyszedł, ale mapper go zgubił" — a `diagnose` jest jedynym
                # oknem, którym potwierdzimy hipotezy mappera na żywym falowniku (Etap 3).
                "power_w": slot.power_w,
                "charge_source": slot.charge_source,
                "discharge_purpose": slot.discharge_purpose,
                "export_limit_w": slot.export_limit_w,
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
                # S-2c: diagnoza jest narzędziem do odpowiadania na pytanie „dlaczego
                # nic się nie dzieje" — nie może się wywalić dokładnie wtedy, gdy plan
                # jest zatruty, czyli w jedynej sytuacji, w której jest naprawdę potrzebna.
                price_pln_kwh=_cena_zaufana(slot_info.get("price_pln_kwh")),
                # R-1: suchy przebieg musi widzieć dokładnie tę samą intencję co realny,
                # inaczej diagnostyka pokazywałaby zapis, którego I-1 by nie przepuściło.
                action=slot_action,
                # S-4: diagnoza widzi TEN SAM zatrzask co realny tor zapisu, ale pracuje
                # na KOPII — inaczej suche pytanie „co byś teraz zrobił" zakładałoby
                # ochronę I-1 i zmieniało zachowanie kolejnego realnego przebiegu
                # (ten sam kontrakt „zero mutacji" co RR-2 dla cache granic i RR-7
                # dla DirectionLimiter).
                reserve_hysteresis=self._reserve.snapshot(),
                # S-4b: ten sam zegar co realny przebieg — diagnoza ma odpowiadać na
                # pytanie „co byś zrobił TERAZ", a stan zatrzasku zależy od czasu.
                now_s=time.monotonic(),
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
        # RR-10: klucz per-nota to `key` (tożsamość zdarzenia), a NIE dosłowna
        # treść `message` — dwie noty tej samej PRZYCZYNY (np. I-9 "wiek rośnie co
        # tick") różnią się tekstem w każdym przebiegu i klucz oparty na treści
        # nigdy się nie powtarzał, więc anty-spam nigdy się nie stabilizował.
        # `n.key or n.message`: notatki bez jawnego `key` (domyślne `""`) wracają
        # do starego zachowania — dosłowna treść NADAL niesie tożsamość tam, gdzie
        # faktycznie niesie inny fakt (np. I-6 "eco_soc=X bez zmiany").
        notes_key = tuple((n.invariant, n.key or n.message) for n in result.notes)
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
