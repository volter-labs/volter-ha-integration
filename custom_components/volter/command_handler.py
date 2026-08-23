"""Command Handler — subskrypcja Supabase Realtime i wykonywanie komend HA."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CANCEL_REPORT_TIMEOUT_S,
    COMMAND_ENTITY_MAP,
    DEVICE_TELEMETRY_PATH,
    OPT_ENTITY_EXPORT_LIMIT_SWITCH,
    REALTIME_HEARTBEAT_INTERVAL,
    REALTIME_RECONNECT_BASE,
    REALTIME_RECONNECT_MAX,
    STOP_LISTEN_TIMEOUT_S,
)
from .executor import VolterExecutor
from .device_token import DeviceTokenProvider
from .guards import GuardResult, RequestDeduplicator, Status, infer_action
from .schedule import InvalidSchedule

_LOGGER = logging.getLogger(__name__)


def _wybierz_harmonogram(payload: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """S-3: jawny, wąski kontrakt wejścia SET_SCHEDULE.

    Poprzednie `payload.get("schedule") or params.get("schedule") or params` miało dwie
    wady naraz. Po pierwsze `or` zsuwa się dalej przy KAŻDEJ wartości falsy — pusty dict
    pod właściwym kluczem cicho oddawał sterowanie kolejnemu źródłu. Po drugie ostatnie
    ogniwo przekazywało do `Schedule.from_dict` DOWOLNY `params`, więc plan przysłany
    pod innym kluczem (literówka, zmiana kontraktu, inna wersja chmury) kasował aktywny
    harmonogram i był raportowany jako sukces (sonda D).

    Rozpoznajemy dokładnie trzy kształty, w tej kolejności:
      1. `payload["schedule"]` — kontrakt bieżący,
      2. `params["schedule"]` — ten sam plan zagnieżdżony w parametrach,
      3. `params` niosące klucz `slots` — kontrakt zastany (`params` JEST planem).

    Klucz obecny, ale nie-obiekt, jest BŁĘDEM, a nie powodem do zsunięcia się niżej:
    inaczej zła serializacja jednego pola po cichu wracałaby na tę samą ścieżkę,
    którą zamyka to ustalenie. Brak rozpoznawalnego harmonogramu też jest błędem —
    nigdy pustym planem (o legalności `{"slots": []}` rozstrzyga `Schedule.from_dict`).
    """
    for zrodlo, kontener in (("payload['schedule']", payload), ("params['schedule']", params)):
        if "schedule" in kontener:
            kandydat = kontener["schedule"]
            if not isinstance(kandydat, dict):
                raise InvalidSchedule(
                    "schedule",
                    f"{zrodlo} musi być obiektem, jest {type(kandydat).__name__}",
                )
            return kandydat

    if "slots" in params:
        return params

    raise InvalidSchedule(
        "schedule",
        "brak rozpoznawalnego harmonogramu — oczekiwano payload['schedule'], "
        "params['schedule'] albo params z kluczem 'slots'",
    )


class VolterCommandHandler:
    """Subskrybuje kanał Realtime device:{device_id} i wykonuje komendy HA.

    Protokół Phoenix Channels (Supabase Realtime):
    1. Connect: wss://{project}.supabase.co/realtime/v1/websocket
    2. Join: topic=realtime:device:{device_id}
    3. Heartbeat: co 30s na topic=phoenix
    4. Odbieraj: event=broadcast, payload.event=command
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        supabase_url: str,
        anon_key: str,
        api_key: str,
        executor: VolterExecutor,
        token_provider: DeviceTokenProvider | None = None,
    ) -> None:
        """Inicjalizacja."""
        self.hass = hass
        self._executor = executor
        # N-6: tożsamość urządzenia dla kanału PRYWATNEGO. Brak providera (stare
        # testy) albo brak tokenu (chmura przed cutoverem) = join publiczny.
        self._token_provider = token_provider
        self._sent_token: str | None = None
        self._dedup = RequestDeduplicator()
        self._entry = entry
        self._device_id = device_id
        self._anon_key = anon_key
        self._api_key = api_key
        self._telemetry_url = f"{supabase_url}{DEVICE_TELEMETRY_PATH}"

        # Zamień https:// na wss:// dla WebSocket
        ws_base = supabase_url.replace("https://", "wss://").replace("http://", "ws://")
        self._ws_url = (
            f"{ws_base}/realtime/v1/websocket"
            f"?apikey={anon_key}&vsn=1.0.0"
        )
        self._channel_topic = f"realtime:device:{device_id}"

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._listen_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._ref_counter = 0
        self._reconnect_delay = REALTIME_RECONNECT_BASE

    async def async_start(self) -> None:
        """Uruchom nasłuchiwanie komend."""
        if not self._has_control_entities():
            _LOGGER.info("No control entities mapped — command handler disabled")
            return

        self._running = True
        # HA czeka w bootstrapie na zadania utworzone przez `hass.async_create_task`,
        # a te dwie pętle nie kończą się nigdy — start HA wisiał przez nie ~90 s
        # ("Setup timed out for bootstrap waiting on ..."). Zadania tła są
        # wyłączone z tego oczekiwania i nadal giną razem z config entry.
        self._listen_task = self._entry.async_create_background_task(
            self.hass, self._connection_loop(), name="volter_realtime_connection"
        )
        _LOGGER.debug("Command handler started")

    async def async_stop(self) -> None:
        """Zatrzymaj nasłuchiwanie."""
        self._running = False

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            # S-5: `cancel()` bez `await` tylko WYSYŁA anulowanie i natychmiast je
            # porzuca — obsługa anulowania w `_execute_command` (raport do chmury,
            # ślad w executorze) nigdy nie dostawała szansy się wykonać, bo HA szedł
            # dalej z wyładowaniem integracji. Czekamy z twardym limitem, żeby zawieszony
            # nasłuch nie blokował przeładowania.
            #
            # S-5d: `wait_for(task)` + `except CancelledError: pass` mieszało dwa
            # zupełnie różne zdarzenia w jednym `except`: „zadanie nasłuchu zakończyło
            # się anulowaniem" (oczekiwane — sami je anulowaliśmy linię wyżej) oraz
            # „ktoś anulował WOŁAJĄCEGO `async_stop`". To drugie było połykane, wbrew
            # zasadzie przyjętej w tym samym commicie w `executor._poczekaj_na_zapis`
            # i w `_execute_command`: anulowania nie wolno zamieniać na normalny powrót,
            # bo zatrzymywanie HA przestaje wtedy działać.
            #
            # `asyncio.wait` rozdziela te przypadki STRUKTURALNIE, a nie zgadywaniem po
            # stanie zadania: zakończenie zadania (w tym anulowaniem) wraca jako wynik
            # w zbiorze `done`, a `CancelledError` może z niego wylecieć wyłącznie
            # wtedy, gdy anulowano nas samych. Wariant `except CancelledError` +
            # sprawdzenie `task.cancelled()` odrzucony świadomie — przy jednoczesnym
            # anulowaniu obu stron nadal połykałby anulowanie wołającego.
            zakonczone, _ = await asyncio.wait(
                {self._listen_task}, timeout=STOP_LISTEN_TIMEOUT_S
            )
            if not zakonczone:
                _LOGGER.warning(
                    "Nasłuch nie zakończył się w %.0fs po anulowaniu — idę dalej "
                    "z wyładowaniem [S-5d]", STOP_LISTEN_TIMEOUT_S,
                )
            elif not self._listen_task.cancelled() and self._listen_task.exception():
                # Bez odebrania wyjątku asyncio zgłosiłoby „Task exception was never
                # retrieved" długo po wyładowaniu, bez związku z tym miejscem.
                _LOGGER.warning(
                    "Nasłuch zakończył się błędem: %s", self._listen_task.exception()
                )

        await self._close_ws()
        _LOGGER.debug("Command handler stopped")

    def _has_control_entities(self) -> bool:
        """Sprawdź czy jakiekolwiek encje sterujące są zmapowane."""
        options = self._entry.options
        for _param, (opt_key, *_rest) in COMMAND_ENTITY_MAP.items():
            if options.get(opt_key):
                return True
        if options.get(OPT_ENTITY_EXPORT_LIMIT_SWITCH):
            return True
        return False

    # ── WebSocket lifecycle ──────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        """Pętla reconnect z exponential backoff."""
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.warning("Realtime connection error: %s", err)

            if not self._running:
                break

            # Exponential backoff
            _LOGGER.info(
                "Reconnecting in %ds...", self._reconnect_delay
            )
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2, REALTIME_RECONNECT_MAX
            )

    async def _connect_and_listen(self) -> None:
        """Połącz się z Realtime WebSocket i nasłuchuj."""
        self._session = aiohttp.ClientSession()

        try:
            self._ws = await self._session.ws_connect(
                self._ws_url,
                heartbeat=REALTIME_HEARTBEAT_INTERVAL,
                timeout=15,
            )

            _LOGGER.info("Connected to Supabase Realtime")
            self._reconnect_delay = REALTIME_RECONNECT_BASE

            # Join channel — prywatny z tokenem urządzenia (N-6), publiczny bez.
            await self._send_json(await self._join_message())

            # Heartbeat task
            # jak wyżej — heartbeat też blokował bootstrap HA
            self._heartbeat_task = self._entry.async_create_background_task(
                self.hass, self._heartbeat_loop(), name="volter_realtime_heartbeat"
            )

            # Nasłuchuj wiadomości
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(json.loads(msg.data))
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    _LOGGER.warning("WebSocket closed/error: %s", msg.type)
                    break

        finally:
            await self._close_ws()

    async def _close_ws(self) -> None:
        """Zamknij WebSocket i sesję."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _join_message(self) -> dict:
        """Ramka `phx_join`.

        N-6: z tokenem urządzenia kanał jest PRYWATNY — Realtime sprawdza RLS na
        `realtime.messages` (migracja 060), więc na `device:{uid}` wchodzi tylko
        właściciel. Bez tokenu (chmura przed cutoverem, brak łącza do device-token)
        dołączamy publicznie jak dotąd; po migracji taki join się nie uda i pętla
        reconnect spróbuje ponownie — już z tokenem, gdy chmura wróci.
        """
        token = await self._token_provider.async_get() if self._token_provider else None
        payload: dict = {"config": {"broadcast": {"self": False}}}
        if token:
            payload["config"]["private"] = True
            payload["access_token"] = token
        else:
            _LOGGER.warning(
                "Brak tokenu urządzenia — dołączam do kanału PUBLICZNEGO [N-6]"
            )
        self._sent_token = token
        return {
            "topic": self._channel_topic,
            "event": "phx_join",
            "payload": payload,
            "ref": self._next_ref(),
        }

    async def _refresh_channel_token(self) -> None:
        """Phoenix: nowy JWT na żywym kanale idzie zdarzeniem `access_token`."""
        if self._token_provider is None:
            return
        token = await self._token_provider.async_get()
        if token and token != self._sent_token:
            await self._send_json({
                "topic": self._channel_topic,
                "event": "access_token",
                "payload": {"access_token": token},
                "ref": self._next_ref(),
            })
            self._sent_token = token

    async def _heartbeat_loop(self) -> None:
        """Wysyłaj heartbeat co REALTIME_HEARTBEAT_INTERVAL sekund."""
        try:
            while self._running and self._ws and not self._ws.closed:
                await asyncio.sleep(REALTIME_HEARTBEAT_INTERVAL)
                await self._send_json({
                    "topic": "phoenix",
                    "event": "heartbeat",
                    "payload": {},
                    "ref": self._next_ref(),
                })
                # N-6: token żyje krócej niż połączenie — odświeżaj bez rozłączania.
                await self._refresh_channel_token()
        except (asyncio.CancelledError, ConnectionError):
            pass

    async def _send_json(self, data: dict) -> None:
        """Wyślij JSON przez WebSocket."""
        if self._ws and not self._ws.closed:
            await self._ws.send_json(data)

    def _next_ref(self) -> str:
        """Generuj kolejny ref dla Phoenix protocol."""
        self._ref_counter += 1
        return str(self._ref_counter)

    # ── Message handling ─────────────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        """Obsłuż wiadomość z Realtime."""
        topic = msg.get("topic", "")
        event = msg.get("event", "")

        if event == "phx_reply":
            status = msg.get("payload", {}).get("status")
            if topic == self._channel_topic:
                _LOGGER.info("Channel join status: %s", status)
            return

        if event == "phx_error":
            _LOGGER.error("Channel error: %s", msg.get("payload"))
            return

        # Broadcast event — komendy z chmury
        if event == "broadcast" and topic == self._channel_topic:
            payload = msg.get("payload", {})
            broadcast_event = payload.get("event", "")
            broadcast_payload = payload.get("payload", {})

            if broadcast_event == "command":
                await self._execute_command(broadcast_payload)

    async def _execute_command(self, payload: dict) -> None:
        """Zrouj komendę z chmury przez executor (guardy + throttle + zapis).

        Zmiana wobec pierwotnej implementacji: handler NIE pisze już bezpośrednio
        do encji. Wszystko idzie przez `VolterExecutor.async_apply`, czyli przez
        sanityzację (I-10), inwarianty I-1…I-9, anty-oscylację (I-8) i throttling (I-6).
        Handler odpowiada tylko za transport i raportowanie.
        """
        # R-10 (reszta): `command`/`request_id` startują jako `None` PRZED barierą,
        # bo `payload` sam może mieć zły kształt — jeśli wybuchnie zanim je wydobędziemy,
        # blok `except` niżej musi mieć czym zalogować/zaraportować (request_id=None
        # jest dla RequestDeduplicator/`_report_result` bezpieczne: `None` nigdy nie
        # dedukuje, patrz R-4).
        command: str | None = None
        request_id: str | None = None

        # R-10 (reszta): bariera na wyjątki musi objąć CAŁĄ obsługę wiadomości,
        # ŁĄCZNIE z wydobyciem pól z payloadu — nie tylko treść komendy. Wcześniej
        # `command = payload.get(...)` / `params = payload.get(...)` / `request_id =
        # payload.get(...)` stały PRZED tym try/except: payload z chmury w kształcie
        # innym niż dict (lista/string/liczba/None) wywalał `AttributeError` już na
        # pierwszym z tych wywołań, więc bariera zaczynająca się dopiero PO nich nic
        # nie łapała — wyjątek propagował przez `_handle_message` do
        # `_connect_and_listen`, którego `finally` zamyka WebSocket (backoff 2->120s).
        try:
            if not isinstance(payload, dict):
                _LOGGER.warning(
                    "Komenda odrzucona: payload nie jest obiektem (jest %s)",
                    type(payload).__name__,
                )
                await self._report_result(
                    request_id, "error",
                    errors=[{
                        "entity": "payload",
                        "error": f"payload musi być obiektem, jest {type(payload).__name__}",
                    }],
                )
                return

            command = payload.get("command", "")
            params = payload.get("params", {}) or {}
            # R-4: `payload.get('request_id', 'unknown')` sprawiało, że WSZYSTKIE komendy
            # bez request_id (ręczny broadcast — smoke test Fazy C) dzieliły jeden wspólny,
            # truthy klucz dedup. Po pierwszej udanej każda następna była cicho odrzucana
            # jako duplikat aż do restartu HA. `None` + `RequestDeduplicator` (guards.py)
            # nigdy nie dedukuje `None` — więc brak request_id oznacza "nigdy nie deduplikuj".
            request_id = payload.get("request_id")

            # L-4: idempotencja. Zapamiętujemy DOPIERO po wykonaniu i tylko gdy komenda
            # nie skończyła się błędem — inaczej nieudana próba blokowałaby retry z chmury
            # na zawsze (N-5).
            if self._dedup.is_duplicate(request_id):
                _LOGGER.info("Komenda %s już wykonana — pomijam [L-4]", request_id)
                await self._report_result(request_id, "duplicate")
                return

            # R-10: `params` z chmury to dowolny JSON. `infer_action`/`sanitize_params`
            # zakładają dict (`.get`, `.items`) — cokolwiek innego (np. lista) wywala
            # AttributeError. Walidujemy jawnie zamiast czekać na crash w głębi guardów,
            # żeby powód błędu w raporcie do chmury był czytelny.
            if not isinstance(params, dict):
                _LOGGER.warning(
                    "Komenda %s odrzucona: params nie jest obiektem (jest %s)",
                    request_id, type(params).__name__,
                )
                await self._report_result(
                    request_id, "error",
                    errors=[{
                        "entity": "params",
                        "error": f"params musi być obiektem, jest {type(params).__name__}",
                    }],
                )
                return

            _LOGGER.info(
                "Komenda: %s (request_id=%s, params=%s)", command, request_id, params
            )

            if command == "SET_SCHEDULE":
                try:
                    # S-3: wybór źródła planu jest WEWNĄTRZ tej bariery, bo od teraz
                    # sam w sobie może odrzucić wiadomość (zły kształt = błąd komendy,
                    # nie pusty plan) — i musi zostać zaraportowany do chmury tak samo
                    # jak błąd parsowania, czyli bez zapamiętania `request_id`.
                    raw = _wybierz_harmonogram(payload, params)
                    result = await self._executor.async_set_schedule(raw)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Nie udało się przyjąć harmonogramu: %s", err)
                    await self._report_result(
                        request_id, "error", errors=[{"entity": "schedule", "error": str(err)}]
                    )
                    return
                # RR-4: `async_set_schedule` UTRWALA harmonogram w `Store` i podmienia
                # aktywny plan executora ZANIM cokolwiek zostanie wykonane — to jest trwały
                # skutek uboczny, niezależny od tego, czy `result.executed` jest puste (I-6
                # mógł odfiltrować wszystkie zapisy do falownika). `_remember_if_effective`
                # (R-2) patrzy tylko na `executed`, więc bez tego retransmisja tej samej
                # komendy po reconnect Realtime nie trafiała w dedup i potrafiła cofnąć
                # aktywny (nowszy) plan do wersji sprzed niej (sonda P12). Zapamiętujemy
                # więc od razu, gdy plan naprawdę trafił do Store — niezależnie od executed.
                self._dedup.remember(request_id)
                await self._report_guard_result(request_id, result, remember=False)
                return

            if command == "SET_WORK_MODE":
                result = await self._executor.async_apply(
                    params, action=infer_action(params), source="cloud"
                )
                await self._report_guard_result(request_id, result)
                return

            _LOGGER.warning("Nieznana komenda: %s", command)
            await self._report_result(
                request_id, "error",
                errors=[{"entity": "command", "error": f"Unknown command: {command}"}],
            )
        except asyncio.CancelledError:
            # S-5: `CancelledError` dziedziczy po `BaseException`, więc `except Exception`
            # niżej go NIE łapie — do chmury nie szło NIC, a chmura rozlicza i planuje
            # kolejną dobę zakładając, że komenda się wykonała. Ścieżka jest realna przy
            # KAŻDEJ zmianie opcji integracji (update listener -> reload -> unload ->
            # `async_stop`), więc nie jest to przypadek teoretyczny.
            _LOGGER.warning(
                "Komenda %s (request_id=%s) anulowana — zatrzymanie/przeładowanie "
                "integracji [S-5]", command, request_id,
            )
            await self._raportuj_anulowanie(request_id)
            # Anulowania NIE WOLNO połknąć — inaczej wyładowanie integracji się zawiesza.
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Nieoczekiwany błąd wykonania komendy %s: %s", command, err)
            await self._report_result(
                request_id, "error", errors=[{"entity": "command", "error": str(err)}]
            )

    async def _raportuj_anulowanie(self, request_id: str | None) -> None:
        """S-5: best-effort raport do chmury o anulowanym przebiegu.

        Status `partial`, a nie `error` ani `success`: zapis do falownika mógł zostać
        dokończony pod ochroną (`executor._async_zapisz_sekwencje`), ale przebieg
        komendy się nie domknął — chmura musi dostać obie te informacje naraz.
        `request_id` świadomie NIE trafia do dedupu (ta sama zasada co R-2): anulowanie
        jest przejściowe, więc retransmisja po przeładowaniu musi działać.

        Cały raport ma twardy budżet czasu, bo to jest ścieżka WYŁĄCZANIA integracji —
        nie może jej opóźniać bardziej niż zwykły POST.
        """
        try:
            await asyncio.wait_for(
                asyncio.shield(
                    asyncio.ensure_future(
                        self._report_result(
                            request_id,
                            Status.PARTIAL.value,
                            errors=[{
                                "entity": "command",
                                "error": (
                                    "przebieg anulowany (zatrzymanie lub przeładowanie "
                                    "integracji) — wynik zapisu nieznany"
                                ),
                            }],
                            notes=[{
                                "invariant": "S-5",
                                "message": (
                                    "komenda przerwana anulowaniem; sekwencja nastaw "
                                    "dokańczana pod ochroną, request_id pozostaje "
                                    "retryowalny"
                                ),
                            }],
                        )
                    )
                ),
                timeout=CANCEL_REPORT_TIMEOUT_S,
            )
        except (TimeoutError, asyncio.CancelledError):
            # Powtórne anulowanie/timeout: raport przepada, ale anulowanie i tak leci
            # dalej z wołającego — nie zamieniamy tego na cichy sukces.
            _LOGGER.warning("Nie udało się zaraportować anulowania komendy [S-5]")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Błąd raportu o anulowaniu komendy: %s [S-5]", err)

    def _remember_if_effective(self, request_id: str, result: GuardResult) -> None:
        """Zapamiętaj request_id TYLKO gdy komenda faktycznie coś zapisała (R-2).

        `Status.ERROR` (odrzucone) dotąd był jedynym wykluczeniem, ale `THROTTLED`
        (I-8) i `DEGRADED` (I-9) są z definicji równie przejściowe — w obu do falownika
        nie poszło nic, a mimo to były zapamiętywane, więc retry z chmury po ustaniu
        przyczyny trafiał w "duplicate" i komenda przepadała na stałe.
        `SUCCESS`/`PARTIAL` z pustym `executed` (np. wszystko odfiltrował throttle I-6,
        bo wartość się nie zmieniła albo minął zbyt krótki czas od ostatniego zapisu)
        to ten sam przypadek "nic nie poszło do falownika" — świadomie traktujemy go
        tak samo: brak zapisu nie blokuje retry, niezależnie od statusu guardów.
        """
        if result.status in (Status.SUCCESS, Status.PARTIAL) and result.executed:
            self._dedup.remember(request_id)

    async def _report_guard_result(
        self, request_id: str, result: GuardResult, *, remember: bool = True
    ) -> None:
        """Zaraportuj wynik razem ze śladem decyzji guardów.

        `remember=False` (RR-4): wołający już zdecydował o zapamiętaniu `request_id`
        na podstawie trwałego skutku ubocznego komendy (SET_SCHEDULE — plan w Store),
        a nie na podstawie `result.executed` jak robi to `_remember_if_effective` (R-2).
        """
        if remember:
            self._remember_if_effective(request_id, result)
        # R-9: `errors=[]` było zahardkodowane — realne błędy per-encja z `executor`
        # (m.in. niezmapowana encja, R-3) nigdy nie opuszczały HA. To była regresja
        # wobec implementacji sprzed Fazy A, która przekazywała prawdziwą listę.
        await self._report_result(
            request_id,
            result.status.value,
            executed=list(result.executed),
            errors=list(result.errors),
            notes=[{"invariant": n.invariant, "message": n.message} for n in result.notes],
        )

    async def _report_result(
        self,
        request_id: str,
        status: str,
        executed: list[str] | None = None,
        errors: list[dict[str, str]] | None = None,
        notes: list[dict[str, str]] | None = None,
    ) -> None:
        """Wyślij wynik komendy z powrotem do chmury jako telemetria z extra.

        `notes` to ślad decyzji guardów — dzięki temu w chmurze widać nie tylko
        "co się nie udało", ale "który inwariant to zablokował i dlaczego".
        """
        result = {
            "request_id": request_id,
            "status": status,
            "executed": executed or [],
            "errors": errors or [],
            "notes": notes or [],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._telemetry_url,
                    json={
                        "readings": [{
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "extra": {"command_result": result},
                        }],
                    },
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": self._api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("Failed to report command result: %s", resp.status)
        except Exception as err:
            _LOGGER.warning("Error reporting command result: %s", err)
