"""S-4b — zatrzask rezerwy chroni tylko oscylacje węższe niż pasmo.

Aneks rundy 3 (`2026-08-16-faza-a-audyt-swiezym-okiem.md`), zmierzone na 1440 tikach:

    amplituda 3 pp (SoC 19/22) -> 2 zapisy eco_soc na dobę   [S-4 OK]
    amplituda 5 pp (SoC 19/24) -> 1440 zapisów na dobę        [NADAL ŹLE]

Samo poszerzanie pasma niczego nie rozwiązuje — dla KAŻDEGO pasma istnieje większa
amplituda. Potrzebny jest drugi wymiar: MINIMALNY CZAS TRWANIA STANU zatrzasku.
Dopiero pasmo + czas dają ograniczenie NIEZALEŻNE od amplitudy.

Cel liczbowy pilnowany w tym pliku: dla dowolnego przebiegu SoC wokół progu rezerwy
liczba zapisów `eco_soc` do falownika w ciągu doby (1440 tików po 60 s) <= 24.

Czego pilnujemy w drugą stronę (I-1 to OCHRONA, nie wygładzanie): SoC WYRAŹNIE
i TRWALE poniżej rezerwy musi blokować rozładowanie w PIERWSZYM tiku, niezależnie
od tego, jak długo trwa bieżący stan zatrzasku.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volter import executor as executor_module
from custom_components.volter.executor import VolterExecutor
from custom_components.volter.guards import (
    Action,
    DeviceState,
    GuardContext,
    ReserveHysteresis,
    UserConfig,
    apply_guards,
)
from tests.conftest import FakeHass, ZegarSterowany

#: Doba w tikach pętli wykonawczej (EXECUTOR_INTERVAL = 60 s).
TIKI_DOBY = 1440

#: Cel liczbowy z ustalenia S-4b — zapisy `eco_soc` do pamięci nieulotnej na dobę.
BUDZET_ZAPISOW_NA_DOBE = 24

OPTIONS = {
    "entity_soc": "sensor.soc",
    "entity_pv_power": "sensor.pv",
    "entity_grid_power": "sensor.grid",
    "entity_ems_mode": "select.tryb",
    "entity_eco_mode_soc": "number.eco_soc",
    "entity_eco_mode_power": "number.eco_power",
    "entity_charge_limit": "number.charge_limit",
    "entity_soc_upper": "number.soc_upper",
    "entity_export_limit_switch": "switch.export_limit",
    "soc_reserve": 20.0,
    "user_mode": "autarky",
    "rated_power_w": 10000.0,
}

REZERWA = 20.0
#: SoC tuż pod rezerwą — dolny punkt oscylacji, dokładnie jak w sondach z dokumentu.
DOL = 19.0


def _hass(soc: float) -> FakeHass:
    hass = FakeHass()
    hass.states.set("sensor.soc", str(soc))
    hass.states.set("sensor.pv", "1200")
    hass.states.set("sensor.grid", "-300")
    hass.states.set(
        "select.tryb",
        "auto",
        {"options": ["auto", "charge_battery", "discharge_battery", "backup"]},
    )
    hass.states.set("number.eco_soc", "20")
    hass.states.set("number.eco_power", "0")
    return hass


def _plan(mode: str, *, soc_target: float) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schedule_id": "s4b",
        "slots": [
            {
                "from": (now - timedelta(minutes=10)).isoformat(),
                "to": (now + timedelta(hours=48)).isoformat(),
                "mode": mode,
                "power_w": 5000.0,
                "soc_target": soc_target,
                "export_allowed": True,
            }
        ],
        "fallback": {"mode": "self_consume", "soc_reserve": 20.0},
    }


def _zapisy_eco_soc(hass: FakeHass) -> list[float]:
    """Progi SoC FAKTYCZNIE zapisane do NVM, wyrażone jako PRÓG — nie jako głębokość.

    Etap 3: na encję idzie głębokość rozładowania (DoD), bo `eco_mode_soc` w GoodWe
    jest tylko do odczytu. Odwracamy z powrotem na próg, żeby asercje mówiły o tym,
    o czym są — o rezerwie — a LICZBA zapisów (sedno tego pliku) jest bez zmian.
    """
    return [
        round(100.0 - data["value"], 1)
        for domain, _service, data in hass.services.calls
        if domain == "number" and data.get("entity_id") == "number.eco_soc"
    ]


async def _doba(fake_entry, monkeypatch, przebieg_soc: list[float]) -> list[float]:
    """Przepuść zadany przebieg SoC przez PEŁNY tor zapisu i zwróć zapisy `eco_soc`.

    Mierzymy to samo, co dokument: realne wywołania serwisu `number.set_value` na
    encji `eco_soc`, a nie statusy guardów. Throttle I-6 zostaje NIETKNIĘTY (60 s),
    bo tiki są co 60 s — czyli dokładnie na jego granicy, i to jest właśnie powód,
    dla którego I-6 nie chroni przed tą klasą oscylacji.
    """
    zegar = ZegarSterowany()
    monkeypatch.setattr(executor_module, "time", zegar)
    hass = _hass(przebieg_soc[0])
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    # Pierwszy przebieg = przyjęcie planu (`async_set_schedule` wykonuje natychmiast).
    await executor.async_set_schedule(_plan("self_consume", soc_target=10.0))

    for soc in przebieg_soc[1:]:
        zegar.przesun(60.0)
        hass.states.set("sensor.soc", str(soc))
        await executor._async_tick()

    return _zapisy_eco_soc(hass)


def _kwadrat(amplituda: float, ile: int = TIKI_DOBY) -> list[float]:
    """Fala prostokątna DOL / DOL+amplituda — dosłownie sonda z dokumentu."""
    return [DOL if i % 2 == 0 else DOL + amplituda for i in range(ile)]


def _trojkat(amplituda: float, ile: int = TIKI_DOBY, krok: float = 4.0) -> list[float]:
    """Najszybsza oscylacja o danej amplitudzie, jaką I-9 w ogóle przepuszcza.

    Przy dużych amplitudach fala prostokątna jest odrzucana przez I-9 jako skok
    fizycznie niemożliwy (4 pp/min), więc test na niej nie mierzyłby zatrzasku,
    tylko I-9. Trójkąt o zboczu równym limitowi tempa przechodzi przez I-9 w całości
    i jest najgorszym przypadkiem, jaki realnie dociera do I-1.
    """
    gora = DOL + amplituda
    krok = min(krok, amplituda)
    seq: list[float] = []
    soc, kierunek = DOL, 1.0
    while len(seq) < ile:
        seq.append(soc)
        soc += kierunek * krok
        if soc >= gora:
            soc, kierunek = gora, -1.0
        elif soc <= DOL:
            soc, kierunek = DOL, 1.0
    return seq


# ── Sonda S-4b: amplituda szersza niż pasmo ──────────────────────────────────


@pytest.mark.asyncio
async def test_s4b_sonda_amplituda_5pp_miesci_sie_w_budzecie(fake_entry, monkeypatch):
    """Sonda wprost z aneksu: SoC 19/24, 1440 tików, zmierzone 1440 zapisów.

    Pasmo 3 pp przepuszcza tę oscylację w całości, bo 24 >= 20 + 3 — zatrzask
    zwalnia się w każdym górnym tiku i zakłada w każdym dolnym. To ta sama klasa
    problemu co pierwotne S-4 i tak samo zużywa pamięć nieulotną falownika.
    """
    zapisy = await _doba(fake_entry, monkeypatch, _kwadrat(5.0))
    assert len(zapisy) <= BUDZET_ZAPISOW_NA_DOBE, (
        f"S-4b: {len(zapisy)} zapisów eco_soc na dobę przy oscylacji 19/24 — "
        f"pasmo bez czasu trwania stanu nie ogranicza niczego"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("amplituda", [1.0, 3.0, 5.0])
async def test_s4b_kwadrat_dowolna_amplituda_w_budzecie(fake_entry, monkeypatch, amplituda):
    """Fala prostokątna o amplitudzie mieszczącej się w limicie tempa I-9 (<= 5 pp)."""
    zapisy = await _doba(fake_entry, monkeypatch, _kwadrat(amplituda))
    assert len(zapisy) <= BUDZET_ZAPISOW_NA_DOBE, (
        f"amplituda {amplituda} pp: {len(zapisy)} zapisów eco_soc na dobę"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("amplituda", [15.0, 40.0])
async def test_s4b_trojkat_duza_amplituda_w_budzecie(fake_entry, monkeypatch, amplituda):
    """Duże amplitudy podane ze zboczem 4 pp/tik, żeby I-9 ich nie odrzuciło."""
    zapisy = await _doba(fake_entry, monkeypatch, _trojkat(amplituda))
    assert len(zapisy) <= BUDZET_ZAPISOW_NA_DOBE, (
        f"amplituda {amplituda} pp: {len(zapisy)} zapisów eco_soc na dobę"
    )


@pytest.mark.asyncio
async def test_s4b_losowy_soc_wokol_progu_w_budzecie(fake_entry, monkeypatch):
    """SoC skaczący losowo wokół progu — bez żadnej regularności do wykorzystania.

    Błądzenie przypadkowe ze zboczem <= 4 pp/tik (limit I-9), w przedziale
    obejmującym zarówno płytkie zejścia pod rezerwę, jak i wyraźne zejścia poniżej
    pasma — czyli także ścieżkę natychmiastowej ochrony.
    """
    los = random.Random(20260816)
    soc = REZERWA
    przebieg: list[float] = []
    for _ in range(TIKI_DOBY):
        przebieg.append(soc)
        soc = max(12.0, min(32.0, soc + los.choice([-4.0, -2.0, 0.0, 2.0, 4.0])))

    zapisy = await _doba(fake_entry, monkeypatch, przebieg)
    assert len(zapisy) <= BUDZET_ZAPISOW_NA_DOBE, (
        f"losowy SoC wokół progu: {len(zapisy)} zapisów eco_soc na dobę"
    )


# ── Ochrona nie może być wygładzana ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_s4b_wyrazne_zejscie_blokuje_rozladowanie_w_pierwszym_tiku(
    fake_entry, monkeypatch
):
    """SoC spada z 60 na 10 i zostaje — I-1 ma zadziałać NATYCHMIAST.

    Czas trwania stanu zatrzasku ma tłumić DRGANIA, a nie opóźniać reakcję na realną
    zmianę. Zejście o 3 pp poniżej rezerwy to już nie szum czujnika, tylko bateria,
    która naprawdę zjada rezerwę backup — ochrona nie ma prawa na nic czekać.
    """
    zegar = ZegarSterowany()
    monkeypatch.setattr(executor_module, "time", zegar)
    hass = _hass(60.0)
    fake_entry.options = dict(OPTIONS)
    executor = VolterExecutor(hass, fake_entry)

    # Plan chce rozładowywać do 10% — powyżej rezerwy guard nie ma nic do roboty.
    await executor.async_set_schedule(_plan("discharge", soc_target=10.0))
    assert _zapisy_eco_soc(hass) == [10.0], "powyżej rezerwy plan idzie bez zmian"
    przed = len(hass.services.calls)

    # Kwadrans później bateria jest na 10% i tam zostaje. Odstęp taki, żeby I-9
    # uznało spadek za możliwy — test ma rozstrzygać o I-1, nie o odrzuceniu odczytu.
    zegar.przesun(900.0)
    hass.states.set("sensor.soc", "10")
    await executor._async_tick()

    assert _zapisy_eco_soc(hass) == [10.0, 20.0], (
        "I-1 nie zadziałało w pierwszym tiku po realnym zejściu pod rezerwę"
    )
    tryby = [
        data["option"]
        for domain, _s, data in hass.services.calls[przed:]
        if domain == "select"
    ]
    assert tryby and "discharge_battery" not in tryby, (
        "rozładowanie musi zostać zdjęte z falownika, a nie tylko odnotowane"
    )

    # I zostaje zablokowane w kolejnych tikach — ochrona nie jest jednorazowa.
    for _ in range(5):
        zegar.przesun(60.0)
        await executor._async_tick()
    assert _zapisy_eco_soc(hass) == [10.0, 20.0], (
        "trwałe zejście pod rezerwę nie może generować kolejnych zapisów"
    )


def test_s4b_czas_trwania_stanu_nie_blokuje_ochrony():
    """Jednostkowo: świeżo zwolniony zatrzask MUSI dać się załączyć wyraźnym spadkiem.

    Stan „zwolniony" ma swój minimalny czas trwania (to on tłumi drgania), ale
    wyraźne zejście poniżej rezerwy jest wyjściem awaryjnym z tego czasu — inaczej
    czas trwania stanu zamieniłby ochronę w wygładzanie.
    """
    h = ReserveHysteresis()
    # Zatrzask zakłada się i zwalnia (pełny cykl), żeby stan „zwolniony" był ŚWIEŻY.
    assert h.engaged(19.0, REZERWA, now=0.0) is True
    assert h.engaged(40.0, REZERWA, now=100_000.0) is False

    # Sekundę później SoC jest wyraźnie pod rezerwą — ochrona bez czekania.
    assert h.engaged(10.0, REZERWA, now=100_001.0) is True


def test_s4b_stale_w_const_i_guards_nie_rozjezdzaja_sie():
    """`guards.py` jest świadomie wolne od HA, więc te same stałe żyją w dwóch plikach.

    Executor buduje zatrzask z wartości z `const.py`, a cała argumentacja doboru
    (i testy jednostkowe) siedzi przy `guards.py` — rozjazd byłby niewidoczny w kodzie
    i widoczny dopiero w zapisach do NVM falownika.
    """
    from custom_components.volter import const, guards

    assert const.RESERVE_HYSTERESIS_PP == guards.RESERVE_HYSTERESIS_PP
    assert const.RESERVE_LATCH_ENGAGED_MIN_S == guards.RESERVE_LATCH_ENGAGED_MIN_S
    assert const.RESERVE_LATCH_RELEASED_MIN_S == guards.RESERVE_LATCH_RELEASED_MIN_S

    # Budżet zapisów jest FUNKCJĄ tych stałych, a nie niezależną obietnicą: jeden pełny
    # cykl zatrzasku to najwyżej dwa zapisy `eco_soc`, więc doba musi się w nim zmieścić.
    h = ReserveHysteresis()
    assert 2 * (86400.0 / h.cycle_min_s) <= BUDZET_ZAPISOW_NA_DOBE, (
        "minimalny cykl zatrzasku nie gwarantuje już budżetu zapisów na dobę"
    )


def test_s4b_nota_i1_mowi_prawde_i_nie_zalewa_logu():
    """Log ma tłumaczyć decyzję, a nie ją zmyślać — i ma to robić raz na przyczynę.

    Zatrzask trzymający pół godziny przy rosnącym SoC generuje notę I-1 w KAŻDYM
    tiku. Dwie rzeczy muszą być wtedy prawdziwe:
      * powód w komunikacie ma odpowiadać rzeczywistości (przy SoC=40% zdanie
        „w paśmie histerezy rezerwy 20%" jest po prostu nieprawdą),
      * tożsamość zdarzenia (`Note.key`, RR-10) nie może zawierać SoC, bo ten zmienia
        się co tick i anty-spam `executor._remember` nigdy by się nie ustabilizował.
    """
    h = ReserveHysteresis()

    def _nota(soc: float, now: float):
        wynik = apply_guards(
            {"eco_soc": 10.0},
            GuardContext(
                state=DeviceState(soc=soc),
                config=UserConfig(soc_reserve=REZERWA),
                action=Action.DISCHARGE,
                reserve_hysteresis=h,
                now_s=now,
            ),
        )
        return next(n for n in wynik.notes if n.invariant == "I-1")

    pod_rezerwa = _nota(19.0, 0.0)
    assert "< rezerwa" in pod_rezerwa.message
    assert pod_rezerwa.key == "rezerwa_ponizej_rezerwy"

    w_pasmie = _nota(21.0, 60.0)
    assert "w paśmie histerezy" in w_pasmie.message
    assert w_pasmie.key != pod_rezerwa.key, "zmiana powodu zasługuje na wpis w logu"

    ponad_pasmem = _nota(40.0, 120.0)
    assert "minimalnego czasu trwania stanu" in ponad_pasmem.message, (
        "przy SoC 40% zatrzask trzyma z powodu czasu, a nie pasma — log nie może kłamać"
    )
    assert _nota(45.0, 180.0).key == ponad_pasmem.key, (
        "ten sam powód przy innym SoC to ta sama tożsamość zdarzenia (RR-10)"
    )


def test_s4b_zwolnienia_nie_czesciej_niz_minimalny_cykl():
    """Nawet ciągła seria wyraźnych zejść nie może rozbujać zatrzasku.

    Wyraźne zejście omija czas trwania stanu zwolnionego (ochrona), więc gdyby to
    była jedyna reguła, przebieg 10/40/10/40 dawałby zapis co tick — dokładnie to,
    przed czym broni S-4b. Ograniczeniem jest wtedy minimalny odstęp między
    kolejnymi ZWOLNIENIAMI: zwolnienie nigdy nie jest pilne, bo utrzymanie rezerwy
    to strona bezpieczna.
    """
    h = ReserveHysteresis()
    zmiany = 0
    poprzedni: bool | None = None
    for i in range(TIKI_DOBY):
        stan = h.engaged(10.0 if i % 2 == 0 else 40.0, REZERWA, now=float(i) * 60.0)
        if stan != poprzedni:
            zmiany += 1
        poprzedni = stan
    assert zmiany <= BUDZET_ZAPISOW_NA_DOBE, (
        f"{zmiany} przełączeń zatrzasku na dobę przy skrajnej oscylacji 10/40"
    )
