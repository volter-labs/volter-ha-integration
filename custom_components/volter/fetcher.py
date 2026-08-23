"""ScheduleFetcher — pobieranie harmonogramu z chmury, transport pull po HTTPS.

Task 16 (Faza D): brakujące ogniwo między Edge Function `get-schedule` (Apka1)
a executorem HA. Świadomie NIE używa Supabase Realtime: broadcast jest
fire-and-forget, więc utrata łącza w momencie replanu oznaczałaby jazdę na
starym planie do jego wygaśnięcia, bez szansy na dosynchronizowanie się po
powrocie łącza. Pull dosynchronizowuje się sam na kolejnym tiku i jest
uwierzytelniony tym samym kluczem co telemetria (`X-API-Key`). To także
transport, który trafi do firmware Volter BOX (Q-002).

Cała walidacja kształtu planu (S-2/S-3, fail-closed) siedzi w `schedule.py` —
ten moduł jest wyłącznie transportem i dwoma strażnikami:
  * błąd sieci / HTTP inny niż 200 NIE może skasować planu lokalnego,
  * niezmieniony plan NIE może ruszać `helpers.storage` co 5 min — po `schedule_id`,
    a gdy ten jest pusty (legalny pusty plan, U-8/D-6), po treści planu.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import GET_SCHEDULE_PATH, SCHEDULE_FETCH_INTERVAL
from .executor import VolterExecutor

_LOGGER = logging.getLogger(__name__)


class ScheduleFetcher:
    """Pobiera plan z `get-schedule` i przekazuje go executorowi."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        supabase_url: str,
        api_key: str,
        executor: VolterExecutor,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._url = f"{supabase_url}{GET_SCHEDULE_PATH}"
        self._api_key = api_key
        self._executor = executor
        self._unsub: CALLBACK_TYPE | None = None
        #: Strażnik zapisu do `helpers.storage`. `executor.async_set_schedule`
        #: zapisuje BEZWARUNKOWO (RR-4 tego wymaga po stronie command_handlera —
        #: SET_SCHEDULE musi utrwalać się za każdym razem, żeby reconnect Realtime
        #: nie potrafił cofnąć aktywnego planu retransmisją), więc to jest JEDYNA
        #: warstwa chroniąca Store przed zapisem co 5 min bez powodu. Świadomie NIE
        #: dublujemy tego wewnątrz executora — zderzyłoby się to z RR-4, który
        #: wymaga, żeby KAŻDE wywołanie `async_set_schedule` naprawdę zapisało.
        self._last_schedule_id: str | None = None
        #: Odcisk treści OSTATNIO PRZYJĘTEGO planu. Dedup po samym id zakłada, że id
        #: jest funkcją treści — dla `schedule_id` sklejanego z identyfikatorów wierszy
        #: to nieprawda (patrz `async_refresh`).
        self._last_plan_signature: str | None = None
        #: U-8: `schedule_id` pusty (legalny pusty plan, patrz `PUSTY_PLAN_Z_CHMURY`
        #: w `test_fetcher.py` / D-6) jest ZAWSZE falsy, więc porównanie po samym id
        #: nigdy nie wykryje "bez zmian" — zmierzone 288 zapisów do Store na dobę,
        #: bezterminowo, dopóki optymalizator jest wyłączony. Gdy id jest puste,
        #: dedup schodzi na porównanie TREŚCI planu; ten sygnaturowy odcisk to jej
        #: nośnik. `None` znaczy "ostatni przyjęty plan miał NIEPUSTY id" — dzięki
        #: temu przejście niepusty→pusty zawsze przechodzi (nie ma z czym porównać).
        self._last_empty_plan_signature: str | None = None

    # ── cykl życia ───────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Pierwszy fetch od razu (urządzenie nie czeka 5 min na pierwszy plan),
        potem cyklicznie co `SCHEDULE_FETCH_INTERVAL`."""
        await self.async_refresh()
        self._unsub = async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=SCHEDULE_FETCH_INTERVAL)
        )
        _LOGGER.info("Pobieranie planu z chmury uruchomione (co %ss)", SCHEDULE_FETCH_INTERVAL)

    async def async_stop(self) -> None:
        """S-5: fetcher nie trzyma żadnego zadania w locie poza subskrypcją
        timera — sam `_fetch` idzie w krótkiej, samodzielnej sesji zamykanej po
        każdym wywołaniu (wzorzec `command_handler._report_result`), więc nie ma
        tu odpowiednika `executor._inflight`, na który trzeba by czekać."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_tick(self, _now: Any = None) -> None:
        await self.async_refresh()

    # ── odświeżanie planu ────────────────────────────────────────────────────

    async def async_refresh(self) -> None:
        """Pobierz plan i przekaż go executorowi.

        Błąd sieci NIE kasuje planu lokalnego — urządzenie jedzie dalej na tym,
        co ma, a I-5 zadziała dopiero po wygaśnięciu slotów (nie po pierwszym
        timeoucie). To samo dotyczy zepsutej odpowiedzi (D-6, patrz niżej):
        malformowany plan nie ma prawa wywrócić pętli pobierania.
        """
        try:
            plan = await self._fetch()
        except Exception as err:  # noqa: BLE001 — każdy błąd sieci jest niekrytyczny
            _LOGGER.warning("Nie udało się pobrać planu (%s) — jadę na lokalnym [I-5]", err)
            return

        if plan is None:
            return

        schedule_id = str(plan.get("schedule_id", ""))
        sygnatura_planu = json.dumps(plan, sort_keys=True)
        if schedule_id:
            # Dedup po id ORAZ po treści. Samo id nie wystarcza, bo `schedule_id`
            # jest sklejony z identyfikatorów WIERSZY `optimizer_schedules` — a treść
            # zmienia też wersja kodu tłumaczącego plan na kontrakt urządzenia.
            #
            # Znalezione na żywej instalacji: po naprawie po stronie chmury (moc slotu
            # odtwarzana z progów SoC) `get-schedule` zwracał już plan z mocą, ale HA
            # go odrzucał — planer nie przeliczał planu, więc id wskazywało te same
            # wiersze co przed naprawą. Objaw był mylący: chmura zwracała 200 z dobrą
            # treścią, po stronie serwera wszystko się zgadzało, a falownik dalej
            # dostawał tryb neutralny.
            if (
                schedule_id == self._last_schedule_id
                and sygnatura_planu == self._last_plan_signature
            ):
                _LOGGER.debug("Plan %s bez zmian — pomijam zapis do Store", schedule_id)
                return
            if schedule_id == self._last_schedule_id:
                _LOGGER.info(
                    "Plan %s ma to samo id, ale INNĄ treść — przyjmuję", schedule_id
                )
        else:
            # U-8: id pusty (legalny pusty plan, D-6) — "bez zmian" rozstrzyga TREŚĆ,
            # nie id. Warunek `self._last_schedule_id == ""` celowo wymaga, żeby
            # OSTATNI PRZYJĘTY plan też miał pusty id: gdy poprzedni plan był
            # niepusty (albo to pierwszy fetch), sygnatury nie ma z czym porównać
            # i zapis MUSI przejść — inaczej przejście "plan realny → optymalizator
            # wyłączony" nigdy by nie dotarło do executora (I-5 zostałoby ślepe).
            if (
                self._last_schedule_id == ""
                and sygnatura_planu == self._last_empty_plan_signature
            ):
                _LOGGER.debug("Pusty plan bez zmian — pomijam zapis do Store")
                return

        try:
            await self._executor.async_set_schedule(plan)
        except Exception as err:  # noqa: BLE001
            # D-6: `Schedule.from_dict` jest fail-closed (S-2/S-3) i słusznie odrzuca
            # PRAWDZIWIE zepsuty kształt — ale odrzucenie nie może propagować się
            # poza fetcher. Bez tej bariery jedna zła odpowiedź (albo regresja po
            # stronie chmury) wyłączałaby pobieranie planu na resztę sesji HA,
            # zamiast po prostu zostać zignorowana do następnego, dobrego tiku.
            # Legalny pusty plan (generated_at="") tu NIE trafia — schedule.py
            # traktuje go jako poprawny wejściowy kształt (patrz D-6 w schedule.py).
            _LOGGER.warning("Plan z chmury odrzucony (%s) — jadę na lokalnym [I-5/S-2]", err)
            return

        self._last_schedule_id = schedule_id
        # U-8: sygnatura ma sens WYŁĄCZNIE, gdy ten plan miał pusty id — dla
        # niepustego id zeruj ją, żeby stary odcisk z poprzedniego pustego planu
        # nie przeciekł w drugą stronę (pusty → niepusty → znowu ten sam pusty).
        self._last_plan_signature = sygnatura_planu
        self._last_empty_plan_signature = sygnatura_planu if not schedule_id else None
        _LOGGER.info(
            "Przyjęto plan %s (%s slotów)", schedule_id or "(bez id)",
            len(plan.get("slots", [])) if isinstance(plan.get("slots"), list) else "?",
        )

    async def _fetch(self) -> dict[str, Any] | None:
        """GET `get-schedule`. `None` = nic do zastosowania na tym tiku.

        S-2/S-3: 500 (błąd bazy po stronie chmury — patrz `get-schedule/index.ts`)
        traktujemy identycznie jak błąd sieci, nie jak zepsutą odpowiedź do
        parsowania — to świadome rozróżnienie chmury „nie wiem" (500) od
        „brak planu" (200 z pustymi `slots`, patrz D-6).
        """
        async with aiohttp.ClientSession() as session:
            async with session.get(
                self._url,
                headers={"X-API-Key": self._api_key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("get-schedule zwrócił %s", resp.status)
                    return None
                return await resp.json()


__all__ = ["ScheduleFetcher"]
