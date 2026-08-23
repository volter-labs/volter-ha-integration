"""Ten sam `schedule_id` z INNĄ treścią musi dotrzeć do urządzenia.

Znalezione na żywej instalacji. Po naprawie po stronie chmury (moc slotu
odtwarzana z progów SoC) `get-schedule` zaczął zwracać plan z mocą — ale HA go
odrzucał, bo `schedule_id` się nie zmienił: planer nie przeliczał planu, więc
identyfikator wskazywał te same wiersze co przed naprawą.

Objaw był mylący do granic: chmura zwracała 200 z poprawną treścią, po stronie
serwera wszystko się zgadzało, a falownik dalej dostawał tryb neutralny. Dopiero
atrybuty sensora w recorderze pokazały, że HA trzyma stary plan.

Dedup po samym identyfikatorze zakłada, że id jest funkcją treści. Dla
`schedule_id` sklejanego z identyfikatorów WIERSZY to nieprawda: treść zmienia
też wersja kodu tłumaczącego plan na kontrakt urządzenia.
"""

from __future__ import annotations

import pytest

from custom_components.volter.fetcher import ScheduleFetcher
from tests.conftest import FakeHass


class _ExecutorStub:
    def __init__(self) -> None:
        self.przyjete: list[dict] = []

    async def async_set_schedule(self, plan: dict) -> None:
        self.przyjete.append(plan)


def _plan(moc: float | None) -> dict:
    return {
        "schedule_id": "abc",
        "generated_at": "2026-08-23T14:00:00+00:00",
        "slots": [{
            "from": "2026-08-23T14:00:00+00:00", "to": "2026-08-23T15:00:00+00:00",
            "mode": "charge", "charge_source": "grid", "discharge_purpose": None,
            "power_w": moc, "soc_target": 80, "price_pln_kwh": 0.4,
            "export_allowed": False, "export_limit_w": None,
        }],
        "fallback": {"mode": "self_consume", "soc_reserve": 10},
    }


def _fetcher(ex: _ExecutorStub, odpowiedzi: list[dict]) -> ScheduleFetcher:
    f = ScheduleFetcher.__new__(ScheduleFetcher)
    f._executor = ex
    f._last_schedule_id = None
    f._last_empty_plan_signature = None
    f._last_plan_signature = None
    kolejka = list(odpowiedzi)

    async def _fetch():
        return kolejka.pop(0) if kolejka else None

    f._fetch = _fetch
    return f


@pytest.mark.asyncio
async def test_ta_sama_tresc_pod_tym_samym_id_jest_pomijana():
    """To jest właściwość, dla której dedup w ogóle istnieje: niezmieniony plan
    nie może ruszać `helpers.storage` co pięć minut."""
    ex = _ExecutorStub()
    f = _fetcher(ex, [_plan(3000.0), _plan(3000.0)])

    await f.async_refresh()
    await f.async_refresh()

    assert len(ex.przyjete) == 1


@pytest.mark.asyncio
async def test_inna_tresc_pod_tym_samym_id_dociera_do_executora():
    ex = _ExecutorStub()
    f = _fetcher(ex, [_plan(None), _plan(3000.0)])

    await f.async_refresh()
    await f.async_refresh()

    assert len(ex.przyjete) == 2, "poprawiona treść musi dojechać mimo tego samego id"
    assert ex.przyjete[1]["slots"][0]["power_w"] == 3000.0
