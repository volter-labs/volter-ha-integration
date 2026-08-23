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

import math
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable

# ── Parametry i ich kolejność stosowania ─────────────────────────────────────

#: Jawna kolejność zapisu parametrów (inwariant I-3 / luka L-6).
#: W części falowników zmiana trybu resetuje limity, więc limity muszą iść PO trybie.
PARAM_ORDER: tuple[str, ...] = (
    "mode",
    # Dolny próg SoC idzie ZARAZ po trybie: to ochrona rezerwy, a zmiana trybu
    # w części falowników resetuje limity. Moc dopiero po progach — inaczej
    # falownik przez moment pracowałby mocą planu przy starej głębokości.
    "eco_soc",
    "soc_upper",
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
    #: Dolny próg SoC (%). Zapisywany jako DoD — przeliczenie w `const.PARAM_VALUE_TRANSFORM`.
    "eco_soc": ParamSpec(0, 100, "%"),
    #: Górny próg SoC (%) — do ilu ładować.
    "soc_upper": ParamSpec(0, 100, "%"),
    #: Głębokość rozładowania podana wprost (stary kontrakt). Encja GoodWe: 0..99.
    "discharge_limit": ParamSpec(0, 99, "%"),
    #: Nastawa mocy `Xset` trybów EMS, w WATACH po stronie baterii.
    #: Było `0..200 A` — to była hipoteza z czasów, gdy sądziliśmy, że sterujemy
    #: limitem prądu ładowania. Realna encja (`ems_power_limit`) przyjmuje waty,
    #: a jej `max` czyta guard I-3 wprost z atrybutów encji (R-8).
    "charge_limit": ParamSpec(0, 30000, "W", to_confirm=True),
    "export_limit": ParamSpec(0, 30000, "W", to_confirm=True),
}

#: Parametry, których obecność sugeruje intencję ładowania / rozładowania.
#:
#: R-1: to jest WYŁĄCZNIE fallback dla starego kontraktu SET_WORK_MODE z surowymi
#: parametrami — czyli dla `GuardContext.action is None`. Na ścieżce harmonogramu
#: heurystyka jest ślepa, bo `slot_to_params` tych kluczy nigdy nie emituje (intencja
#: slotu siedzi w nieprzezroczystej dla guardów nazwie trybu), więc I-1 i I-2 były tam
#: martwe. Pierwszeństwo ma zawsze jawna intencja z planu.
#:
#: TODO(Etap-1): heurystyka jest dodatkowo słaba, bo w GoodWe `discharge_limit` to
#: głębokość rozładowania (DoD, %), czyli limit, a nie intencja. Potwierdzić semantykę
#: przy okazji mapy nastaw GoodWe.
_CHARGE_HINTS: tuple[str, ...] = ()
#: `charge_limit` USUNIETY z podpowiedzi kierunku (Etap 3). Nazwa jest historyczna:
#: parametr celuje w `ems_power_limit`, czyli nastawe mocy `Xset` trybow EMS, ktora
#: jest DWUKIERUNKOWA — ten sam rejestr niesie moc ladowania i rozladowania.
#: Dopoki byl podpowiedzia ladowania, slot rozladowania niosacy moc wygladal dla I-2
#: jak jednoczesne ladowanie i rozladowanie i cala komenda byla odrzucana.
#: Kierunek dla starego kontraktu wnioskuje teraz `infer_action` z nazwy trybu
#: (`discharge_battery` / `charge_battery` / `charge_pv`), co jest jednoznaczne.
_DISCHARGE_HINTS = ("discharge_limit",)


# ── Wejście guardów ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParamBounds:
    """Realna granica nastawy, odczytana z atrybutów `min`/`max` encji (I-3).

    R-8: to jest wiedza o SPRZĘCIE, a nie nasze przypuszczenie — dlatego ma
    pierwszeństwo nad `PARAM_SPECS`, które do czasu mapy nastaw z Etapu 3 zgadują.
    """

    lo: float
    hi: float


@dataclass(frozen=True)
class InverterLimits:
    """Twarde granice sprzętowe (I-3). Docelowo czytane z falownika."""

    #: Granice w WATACH — nikt ich dziś nie ustawia i nikt na nich nie pracuje.
    #: R-8: świadomie zostają puste do Etapu 3: przeliczenie ich na nastawy falownika
    #: wymaga napięcia baterii (`charge_limit` jest w A) i mapy nastaw. Część mocową I-3
    #: realizuje `param_bounds`, czyli granice w jednostkach samych encji.
    max_charge_w: float | None = None
    max_discharge_w: float | None = None
    soc_min_hw: float = 0.0
    soc_max_hw: float = 100.0
    temperature_ok: bool = True
    #: Lista dopuszczalnych opcji encji select trybu pracy. Jeśli None — brak walidacji
    #: enuma (nie znamy jeszcze listy). Docelowo czytana z atrybutu `options` encji.
    allowed_modes: tuple[str, ...] | None = None
    #: R-8: granice per-parametr z encji `number` (nazwa parametru -> `ParamBounds`).
    #: Pusty słownik znaczy „nie wiem" — wtedy jedyną obroną zostaje sanity-check
    #: z `PARAM_SPECS`, bo zmyślona granica byłaby groźniejsza niż jej brak.
    param_bounds: dict[str, ParamBounds] = field(default_factory=dict)


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
    #: RR-1: ile czasu upłynęło między poprzednią ZAUFANĄ próbką SoC a bieżącym
    #: odczytem. Bez tego I-9 nie odróżnia niemożliwego skoku od normalnej zmiany po
    #: przerwie w telemetrii i po jednym rozjeździe blokuje tor zapisu na stałe.
    #: `None` = odstępu nie znamy (stary kontrakt wołającego).
    previous_soc_age_s: float | None = None


#: S-4 — domyślna szerokość histerezy progu rezerwy (I-1), w punktach procentowych.
#:
#: DLACZEGO 3 pp: sensory SoC raportują wartość skwantowaną (zwykle co 1 pp), a estymata
#: SoC z BMS dodatkowo faluje o ułamki punktu. Pasmo RÓWNE rozdzielczości niczego nie
#: rozwiązuje — oscylacja 19/21 wokół progu 20 nadal przekraczałaby punkt zwolnienia.
#: Pasmo musi być więc wielokrotnością rozdzielczości: przy 3 pp zwolnienie zatrzasku
#: wymaga REALNEGO naładowania (na typowym magazynie 10 kWh to ~0,3 kWh), czego szum
#: czujnika nie podrobi. Górne ograniczenie: w oknie zatrzasku bateria jest trzymana
#: na rezerwie, mimo że SoC jest już do `band_pp` ponad nią — zbyt szerokie pasmo
#: podnosiłoby efektywną rezerwę i marnowało pojemność. 3 pp to mniej niż jeden tick
#: typowego poboru domowego, więc plan wraca praktycznie natychmiast po realnym ładowaniu.
RESERVE_HYSTERESIS_PP: float = 3.0

#: S-4b — minimalny czas trwania stanu ZAŁĄCZONEGO zatrzasku, w sekundach.
#:
#: DLACZEGO 30 min: to jest cena ekonomiczna naprawy. W oknie zatrzasku bateria stoi
#: na rezerwie, więc każda sekunda tego stanu ponad realną potrzebę to zablokowany
#: slot planu. 30 min = pół slotu godzinowego: wystarczająco długo, żeby stłumić
#: drgania czujnika (te trwają sekundy), i wystarczająco krótko, żeby po REALNYM
#: naładowaniu baterii plan wrócił jeszcze w tej samej godzinie rozliczeniowej.
RESERVE_LATCH_ENGAGED_MIN_S: float = 1800.0

#: S-4b — minimalny czas trwania stanu ZWOLNIONEGO, po którym PŁYTKIE zejście
#: pod rezerwę (mniej niż `band_pp`) może zatrzask ponownie założyć.
#:
#: DLACZEGO 2 h i dlaczego to bezpieczne: ten czas dotyczy WYŁĄCZNIE zejść mieszczących
#: się w paśmie, czyli w tej samej niepewności ±3 pp, którą pasmo już akceptuje w drugą
#: stronę. Realne zejście nie zatrzymuje się w paśmie — bateria pod obciążeniem schodzi
#: 3 pp w kilka minut i wpada w ścieżkę awaryjną (`soc < reserve - band_pp`), która ten
#: czas OMIJA. Maksymalna ekspozycja to więc `band_pp` poniżej rezerwy, dokładnie tyle,
#: ile pasmo kosztuje powyżej — a nie „2 h bez ochrony".
RESERVE_LATCH_RELEASED_MIN_S: float = 7200.0


class ReserveHysteresis:
    """S-4/S-4b: zatrzask progu rezerwy dla I-1 — chroni pamięć nieulotną falownika.

    Sam warunek `state.soc < cfg.soc_reserve` nie ma histerezy, a I-6 go nie ratuje:
    przy baterii stojącej dokładnie na progu `eco_soc` REALNIE zmienia wartość w każdym
    tiku (raz `soc_target` z planu, raz rezerwa użytkownika), więc throttle „bez zmiany"
    nie ma czego pominąć. I-8 też nie łapie, bo akcja pozostaje `SELF_CONSUME` — to nie
    jest zmiana kierunku. Efekt: jeden zapis do NVM na tick, ~1440 na dobę, bezterminowo.

    **S-4b: samo pasmo tego nie domyka.** Zatrzask oparty wyłącznie na `band_pp` chroni
    tylko oscylacje WĘŻSZE niż pasmo — zmierzone na 1440 tikach: amplituda 3 pp daje
    2 zapisy na dobę, ale amplituda 5 pp znowu 1440. Poszerzanie pasma nie pomaga, bo
    dla każdego pasma istnieje większa amplituda, a szerokie pasmo podnosi efektywną
    rezerwę. Ograniczenie niezależne od amplitudy daje dopiero DRUGI WYMIAR: minimalny
    czas trwania stanu. Trzy reguły, każda z innego powodu:

      1. `band_pp` — próg zwolnienia jest wyżej niż próg założenia (tłumi szum czujnika),
      2. `engaged_min_s` / `released_min_s` — raz przyjęty stan musi potrwać, niezależnie
         od tego, co robi SoC (tłumi drgania o DOWOLNEJ amplitudzie),
      3. `cycle_min_s` — minimalny odstęp między kolejnymi ZWOLNIENIAMI. To jest domknięcie
         ścieżki awaryjnej: wyraźne zejście pod rezerwę omija regułę 2 (bo ochrona nie może
         czekać), więc bez reguły 3 przebieg 10/40/10/40 rozbujałby zatrzask na nowo.
         Zwolnienie NIGDY nie jest pilne — utrzymanie rezerwy to strona bezpieczna — więc
         wolno je ograniczać czasem, w przeciwieństwie do założenia.

    Skutek liczbowy: jeden pełny cykl zatrzasku trwa co najmniej `cycle_min_s`, a każdy
    cykl to najwyżej dwa zapisy `eco_soc` — czyli twardy budżet ~20 zapisów na dobę
    dla DOWOLNEGO przebiegu SoC.

    Czego zatrzask NIE robi: nie opóźnia ochrony przed realnym zejściem pod rezerwę.
    `soc < reserve - deep_pp` załącza go natychmiast, w każdym stanie i o każdej porze.
    """

    def __init__(
        self,
        band_pp: float = RESERVE_HYSTERESIS_PP,
        engaged_min_s: float = RESERVE_LATCH_ENGAGED_MIN_S,
        released_min_s: float = RESERVE_LATCH_RELEASED_MIN_S,
        deep_pp: float | None = None,
    ) -> None:
        self.band_pp = band_pp
        self.engaged_min_s = engaged_min_s
        self.released_min_s = released_min_s
        #: Jak głęboko pod rezerwą kończy się „szum czujnika", a zaczyna realne
        #: zjadanie rezerwy backup. Domyślnie tyle samo, ile pasmo daje w górę —
        #: koszt zatrzasku ma być symetryczny wokół rezerwy.
        self.deep_pp = band_pp if deep_pp is None else deep_pp
        self._engaged = False
        #: Kiedy bieżący stan został przyjęty. `None` = stan nigdy się nie zmienił,
        #: czyli nie ma jeszcze czego utrzymywać (świeży executor nie może startować
        #: z ochroną zamrożoną na dwie godziny).
        self._since: float | None = None
        #: Kiedy zatrzask ostatnio ZWOLNIŁ — baza minimalnego cyklu (reguła 3).
        self._last_release: float | None = None

    @property
    def cycle_min_s(self) -> float:
        """Minimalny odstęp między kolejnymi zwolnieniami zatrzasku.

        Suma obu czasów trwania, a nie osobna stała — bo dokładnie tyle trwa cykl
        w przebiegu bez ścieżki awaryjnej. Reguła 3 ma domykać lukę, a nie tworzyć
        drugiego, niezgodnego z resztą ograniczenia.
        """
        return self.engaged_min_s + self.released_min_s

    def engaged(self, soc: float, reserve: float, now: float | None = None) -> bool:
        """Czy I-1 ma zadziałać. Aktualizuje zatrzask — wołać tylko z REALNEGO przebiegu.

        `now` to zegar monotoniczny wołającego (jak w `WriteThrottle.filter`
        i `DirectionLimiter.allows`) — stan zatrzasku jest teraz funkcją czasu, więc
        musi go dostać z tego samego źródła co reszta guardów czasowych.
        """
        teraz = time.monotonic() if now is None else now
        nowy = self._decide(soc, reserve, teraz)
        if nowy != self._engaged:
            self._since = teraz
            if not nowy:
                self._last_release = teraz
        self._engaged = nowy
        return self._engaged

    def _decide(self, soc: float, reserve: float, now: float) -> bool:
        trwanie = None if self._since is None else max(0.0, now - self._since)

        if self._engaged:
            if soc < reserve + self.band_pp:
                return True  # pasmo: odczyt tuż nad rezerwą to nadal szum
            if trwanie is not None and trwanie < self.engaged_min_s:
                return True  # czas trwania stanu: drgania o dowolnej amplitudzie
            if self._last_release is not None and (now - self._last_release) < self.cycle_min_s:
                return True  # minimalny cykl: nie wolno zwalniać częściej
            return False

        # Stan zwolniony. Wyraźne zejście pod rezerwę to nie jest szum — to bateria,
        # która realnie zjada rezerwę backup. Ochrona wchodzi natychmiast, bez oglądania
        # się na czas trwania stanu; inaczej tłumienie drgań stałoby się opóźnianiem
        # reakcji, a I-1 jest ochroną, nie wygładzaniem.
        if soc < reserve - self.deep_pp:
            return True
        if soc < reserve:
            if trwanie is not None and trwanie < self.released_min_s:
                return False
            return True
        return False

    def snapshot(self) -> "ReserveHysteresis":
        """Kopia dla SUCHEGO przebiegu (`executor.async_diagnose`).

        Ten sam kontrakt co `ha_state.ParamBoundsCache.snapshot` (RR-2) i co
        `DirectionLimiter.would_allow` (RR-7): diagnoza musi widzieć dokładnie ten
        sam zatrzask co realny tor zapisu, ale nie wolno jej go założyć ani zwolnić —
        inaczej pytanie „co byś teraz zrobił" ZMIENIAŁOBY kolejny realny przebieg.
        Kopia zamiast osobnej metody `would_*`, bo to `apply_guards` decyduje o I-1
        i musiałoby wtedy znać tryb wołania.
        """
        kopia = ReserveHysteresis(
            self.band_pp, self.engaged_min_s, self.released_min_s, self.deep_pp
        )
        kopia._engaged = self._engaged
        # S-4b: kopiujemy też ZNACZNIKI CZASU — bez nich suchy przebieg pracowałby
        # na zatrzasku bez historii, czyli pokazywałby decyzję, której realny tor
        # zapisu w tej chwili by nie podjął.
        kopia._since = self._since
        kopia._last_release = self._last_release
        return kopia


@dataclass
class GuardContext:
    """Kontekst wykonania guardów."""

    state: DeviceState
    limits: InverterLimits = field(default_factory=InverterLimits)
    config: UserConfig = field(default_factory=UserConfig)
    price_pln_kwh: float | None = None
    #: S-4: zatrzask progu rezerwy (I-1). `None` = wołający nie ma stanu między
    #: przebiegami (testy jednostkowe, stary kontrakt) — wtedy zostaje goły próg,
    #: bo histereza bez pamięci byłaby tylko przesuniętym progiem.
    reserve_hysteresis: "ReserveHysteresis | None" = None
    #: S-4b: zegar monotoniczny wołającego dla zatrzasku rezerwy. Ten sam wzorzec co
    #: `now_ts` w `WriteThrottle.filter` — stan zatrzasku jest funkcją czasu, więc musi
    #: iść z tego samego źródła co I-6/I-8, inaczej dwa guardy mierzyłyby dwa czasy.
    #: `None` = wołający zegara nie podał, zatrzask weźmie własny.
    now_s: float | None = None
    #: Jawna intencja planu (R-1). Gdy podana, I-1 i I-2 pracują na niej, bo tylko ona
    #: przetrwa mapowanie na nastawy falownika — nazwa trybu jest dla guardów
    #: nieprzezroczysta. `None` oznacza stary kontrakt SET_WORK_MODE z surowymi
    #: parametrami i włącza heurystykę po kluczach.
    action: "Action | None" = None
    #: Maksymalny dopuszczalny wiek odczytu (I-9).
    max_state_age_s: float = 300.0
    #: Maksymalny sensowny skok SoC między odczytami w punktach procentowych (I-9).
    #: RR-1: używany już tylko awaryjnie — gdy `previous_soc_age_s is None`, czyli gdy
    #: odstępu między próbkami nie znamy i nie da się policzyć tempa.
    max_soc_jump_pp: float = 20.0
    #: RR-1: maksymalne realne tempo zmiany SoC (pp/minutę). To jest właściwy test
    #: I-9 — próg skoku ma sens WYŁĄCZNIE odniesiony do czasu, jaki upłynął.
    max_soc_rate_pp_per_min: float = 4.0
    #: RR-1: podłoga tolerancji dla bardzo krótkich odstępów (kwantyzacja czujnika).
    min_soc_jump_tolerance_pp: float = 5.0


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
    #: RR-10 (ustalenie 2. kontrolera z rundy 2): tożsamość zdarzenia dla anty-spamu
    #: w `executor._remember`, NIEZALEŻNA od sformatowanej treści `message`. Dwie
    #: noty tej samej PRZYCZYNY (np. I-9 „odczyt starszy niż Xs (wiek Ys)") mogą
    #: różnić się treścią w KAŻDYM ticku, bo `wiek` rośnie z czasem — klucz oparty
    #: na dosłownej treści wtedy nigdy się nie powtarza i anty-spam nigdy się nie
    #: stabilizuje. Puste `""` (domyślne) znaczy „brak jawnej tożsamości" —
    #: `_remember` wraca wtedy do treści `message` jako klucza (zachowanie sprzed
    #: RR-10, świadomie zachowane tam, gdzie treść realnie niesie inny fakt).
    key: str = ""


@dataclass
class GuardResult:
    """Wynik przejścia komendy przez guardy."""

    params: dict[str, Any]
    status: Status
    notes: list[Note] = field(default_factory=list)
    #: Parametry FAKTYCZNIE zapisane na encjach. Różni się od `params`: throttle I-6
    #: mógł część pominąć, a zapis mógł się nie powieść. Raport do chmury musi
    #: opierać się na tym polu, nie na `params` (N-4).
    executed: list[str] = field(default_factory=list)
    #: Akcja, którą guardy WYMUSZAJĄ zamiast intencji planu (R-1). Ustawiana, gdy I-1
    #: blokuje rozładowanie: samo usunięcie parametrów nie wystarcza, bo falownik
    #: zostałby w trybie z poprzedniego slotu. Guardy nie znają nazw trybów falownika,
    #: więc zwracają samą intencję — przetłumaczy ją mapper, jedyne miejsce, które te
    #: nazwy zna.
    forced_action: "Action | None" = None
    #: R-9: realne błędy zapisu per-encja (z `applier.apply_params`). Bez tego pola
    #: `command_handler._report_guard_result` nie miał skąd wziąć czegokolwiek innego
    #: niż `[]` — błędy zostawały uwięzione w `executor._last` i nigdy nie docierały
    #: do chmury. To regresja wobec implementacji sprzed Fazy A.
    errors: list[dict[str, str]] = field(default_factory=list)
    #: RR-1: czy bieżący odczyt SoC wolno przyjąć jako baseline NASTĘPNEGO przebiegu.
    #: `False` tylko wtedy, gdy to właśnie I-9 zakwestionowało sam odczyt (brak, wiek,
    #: wartość poza fizyką, tempo). Wołający nie może tego wnioskować z obecności noty
    #: I-9, bo notę zostawia też przyjęcie NOWEGO baseline'u po długiej przerwie —
    #: i to właśnie mylenie tych dwóch przypadków dawało trwały lockout toru zapisu.
    soc_baseline_ok: bool = True
    #: RR-3: nazwy parametrów, które guard bezpieczeństwa SAM wstawił/wymusił
    #: (I-1 podnoszące próg eco_soc, I-4 blokujące eksport) — w odróżnieniu od
    #: parametrów pochodzących z normalnego mapowania planu (`mappers.slot_to_params`
    #: emituje część z nich ZAWSZE, niezależnie od tego, czy użytkownik zmapował
    #: odpowiednią encję). `applier.apply_params` musi traktować te dwie grupy różnie:
    #: encja bez mapowania dla parametru WYMUSZONEGO to BŁĄD (ochrona, która nie może
    #: się zastosować, musi być głośna — R-3), encja bez mapowania dla parametru
    #: z normalnego planu to tylko widoczna NOTA (użytkownik świadomie nie zmapował
    #: opcjonalnej encji — dosłowne traktowanie obu grup identycznie zamieniało
    #: legalną konfigurację w trwały ERROR co przebieg pętli, RR-3).
    forced_params: set[str] = field(default_factory=set)

    @property
    def rejected(self) -> bool:
        return self.status in (Status.ERROR, Status.DEGRADED, Status.DUPLICATE)

    def note(self, invariant: str, message: str, *, key: str = "") -> None:
        self.notes.append(Note(invariant, message, key=key))

    def as_report(self) -> dict[str, Any]:
        # R-14: kopia obronna. `params` bez `dict(...)` to żywa referencja do stanu
        # guarda — konsument modyfikujący odpowiedź serwisu w miejscu (np. chmura
        # albo test) mógłby podmienić `GuardResult.params`, mimo że kontrakt "raport"
        # ma być migawką, nie uchwytem do stanu wewnętrznego.
        return {
            "status": self.status.value,
            "params": dict(self.params),
            "executed": list(self.executed),
            # R-1: chmura musi widzieć, że guard podmienił intencję planu — inaczej
            # „plan mówił sprzedawaj, a falownik stoi" wygląda na awarię łącza.
            "forced_action": self.forced_action.value if self.forced_action else None,
            "notes": [{"invariant": n.invariant, "message": n.message} for n in self.notes],
            # R-9: błędy zapisu per-encja muszą być widoczne w raporcie, nie tylko w logu HA.
            "errors": list(self.errors),
            # RR-3: chmura musi widzieć, które parametry są zabezpieczeniem wymuszonym
            # przez guardy — to jest kontekst potrzebny do interpretacji `errors` (patrz
            # `forced_params` na klasie).
            "forced_params": sorted(self.forced_params),
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

        # RR-2: skończoność sprawdzamy ZAWSZE i PRZED jakąkolwiek ścieżką skróconą,
        # bo NaN/inf przechodzą `isinstance(value, (int, float))`, a przycinanie do
        # granicy w I-3 (`min(max(nan, lo), hi)`) zwraca NaN — wartość szła prosto
        # na encję falownika. Osiągalne z chmury: `json.loads` przyjmuje literał `NaN`.
        try:
            numeric = float(value)
        except OverflowError as err:
            # Druga postać tego samego wektora: JSON nie ogranicza precyzji liczb
            # całkowitych, a `float(10**400)` rzuca wyjątek spoza kontraktu guarda.
            # Fail-closed ma tu ODRZUCIĆ komendę, nie wysypać sanityzację.
            raise InvalidCommand(
                "I-10", f"{key}={value!r} nie mieści się w liczbie zmiennoprzecinkowej"
            ) from err
        if not math.isfinite(numeric):
            raise InvalidCommand(
                "I-10", f"{key}={value!r} nie jest liczbą skończoną (NaN/inf)"
            )

        # R-8: gdy znamy REALNĄ granicę z encji, zgadywany GÓRNY zakres z `PARAM_SPECS`
        # (`to_confirm=True`) musi ustąpić — inaczej I-10 odrzuca całą komendę tam,
        # gdzie specyfikacja (T-3) każe przyciąć do granicy sprzętu. Przycięciem
        # zajmuje się I-3 w `apply_guards`, które od RR-2 raportuje je jako PARTIAL.
        # Zakresów pewnych (`to_confirm=False`, np. procenty 0..100) nie rozmiękczamy:
        # to fizyczna domena parametru, której żadna encja nie poszerzy (T-11).
        #
        # RR-2: granica z encji ZASTĘPUJE zgadywany zakres, ale nie zwalnia z walidacji.
        # Dolna granica z `PARAM_SPECS` (0 A / 0 W) to domena fizyczna, nie zgadywanie,
        # więc obowiązuje dalej: wartość ujemna nie ma sensu niezależnie od tego, co
        # wystawia encja, i nie wolno jej "naprawiać" cichym przycięciem do zera.
        if spec.to_confirm and key in limits.param_bounds:
            if numeric < spec.lo:
                raise InvalidCommand(
                    "I-10",
                    f"{key}={value} poniżej fizycznej dolnej granicy {spec.lo} {spec.unit}",
                )
            clean[key] = numeric
            continue

        if not (spec.lo <= numeric <= spec.hi):
            raise InvalidCommand(
                "I-10", f"{key}={value} poza zakresem {spec.lo}..{spec.hi} {spec.unit}"
            )
        clean[key] = numeric

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
        # RR-1: nie ma czego przyjąć za baseline — poprzednia próbka i jej znacznik
        # czasu muszą zostać nietknięte, żeby odstęp liczył się od realnego odczytu.
        result.soc_baseline_ok = False
        # RR-10: `key` jawny mimo że TA treść akurat jest stała — konsekwentnie ze
        # wszystkimi gałęziami DEGRADED I-9, żeby żadna z nich nie zależała
        # przypadkiem od tego, czy komunikat akurat nie zawiera liczby.
        result.note("I-9", "brak odczytu SoC — wstrzymuję zapisy", key="missing_reading")
        result.params = {}
        return result
    if state.age_s > ctx.max_state_age_s:
        result.status = Status.DEGRADED
        result.soc_baseline_ok = False
        # RR-10: `wiek` rośnie z czasem, więc treść zmienia się na KAŻDYM ticku —
        # bez jawnego `key` anty-spam w `executor._remember` nigdy się nie stabilizuje
        # (dosłowny tekst nigdy się nie powtarza), mimo że PRZYCZYNA jest ta sama.
        result.note(
            "I-9",
            f"odczyt starszy niż {ctx.max_state_age_s:.0f}s (wiek {state.age_s:.0f}s)",
            key="stale_reading",
        )
        result.params = {}
        return result
    if not (0.0 <= state.soc <= 100.0):
        result.status = Status.DEGRADED
        result.soc_baseline_ok = False
        result.note("I-9", f"SoC={state.soc} fizycznie niemożliwy", key="invalid_range")
        result.params = {}
        return result

    # RR-1: test wiarygodności skoku SoC jest testem TEMPA, nie różnicy bezwzględnej.
    #
    # Różnica bezwzględna odpowiada na złe pytanie: 35 pp między dwoma odczytami
    # oddalonymi o 10 s to awaria czujnika, a te same 35 pp po 40 minutach przerwy
    # w telemetrii to bateria, która naprawdę się naładowała. Poprzednia wersja
    # (naprawa R-5) odrzucała oba tak samo i — skoro baseline aktualizował się tylko
    # z odczytów, które przeszły I-9 — po jednym rozjeździe zamrażała punkt odniesienia
    # na stałe: każdy kolejny tick też był odrzucany, aż do restartu HA. Falownik
    # zostawał na nastawie sprzed awarii bezterminowo (scenariusz L-2).
    if state.previous_soc is not None:
        delta_pp = abs(state.soc - state.previous_soc)
        gap_s = state.previous_soc_age_s

        if gap_s is not None and gap_s > ctx.max_state_age_s:
            # RR-1: ŚCIEŻKA WYJŚCIA. Próbka starsza niż okno świeżości przestaje być
            # wiarygodnym punktem odniesienia — porównywanie się z nią nie niesie już
            # informacji. Bieżący odczyt (świeży i mieszczący się w 0..100) przyjmujemy
            # jako NOWY baseline z jawną notą, zamiast odrzucać go w nieskończoność.
            # RR-10: `gap_s` i `state.soc` zmieniają się co tick — jawny `key`
            # zapewnia, że powtarzające się przyjęcie nowego baseline'u (np. trwała
            # przerwa w telemetrii) nie zalewa logu INFO co 60s.
            result.note(
                "I-9",
                f"poprzednia próbka SoC sprzed {gap_s:.0f}s (> {ctx.max_state_age_s:.0f}s) "
                f"— przyjmuję {state.soc}% jako nowy baseline",
                key="baseline_reset",
            )
        else:
            if gap_s is None:
                # Odstępu nie znamy (stary kontrakt wołającego) — nie da się policzyć
                # tempa, więc zostaje konserwatywny próg bezwzględny. Brak wiedzy
                # o czasie nie może rozmiękczać I-9.
                allowed_pp = ctx.max_soc_jump_pp
            else:
                # Limit proporcjonalny do upłynionego czasu, z podłogą na kwantyzację
                # czujnika. Przy `gap_s = max_state_age_s` wychodzi dokładnie
                # `max_soc_jump_pp`, więc w oknie świeżości nic nie jest luźniejsze
                # niż przed naprawą.
                allowed_pp = max(
                    ctx.min_soc_jump_tolerance_pp,
                    ctx.max_soc_rate_pp_per_min * (gap_s / 60.0),
                )

            if delta_pp > allowed_pp:
                result.status = Status.DEGRADED
                # R-5: odczyt zakwestionowany przez I-9 NIE MOŻE stać się zaufanym
                # baseline'em następnego przebiegu — inaczej guard chroniłby przez
                # dokładnie jeden tick.
                result.soc_baseline_ok = False
                # RR-10: `delta_pp`/`gap_s` zmienne co tick — jawny `key`, ta sama
                # przyczyna (skok SoC odrzucony) nie ma zalewać logu INFO w kółko.
                result.note(
                    "I-9",
                    f"skok SoC {state.previous_soc}->{state.soc} ({delta_pp:.1f} pp) "
                    f"przekracza {allowed_pp:.1f} pp dopuszczalne przy odstępie "
                    + (f"{gap_s:.0f}s" if gap_s is not None else "nieznanym"),
                    key="soc_jump",
                )
                result.params = {}
                return result

    # I-3: temperatura / okno pracy sprzętu — najwyższy priorytet.
    if not limits.temperature_ok:
        result.status = Status.DEGRADED
        result.note("I-3", "falownik/BMS poza oknem temperatur — wstrzymuję zapisy")
        result.params = {}
        return result

    # Intencja komendy. R-1: pierwszeństwo ma to, co powiedział plan (`ctx.action`),
    # bo tylko ono przeżywa mapowanie na nastawy falownika. Heurystyka po kluczach
    # dokłada się do intencji (a nie zastępuje jej), żeby I-2 wyłapało sprzeczność
    # między planem a surowymi parametrami; bez `ctx.action` zostaje jedynym źródłem.
    hint_charge = any(out.get(k, 0) for k in _CHARGE_HINTS)
    hint_discharge = any(out.get(k, 0) for k in _DISCHARGE_HINTS)
    wants_charge = hint_charge or ctx.action is Action.CHARGE
    wants_discharge = hint_discharge or ctx.action is Action.DISCHARGE

    # I-2: sprzeczna intencja (jednoczesne ładowanie i rozładowanie).
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
    # Rezerwa broni wyłącznie przed rozładowaniem — ładowanie poniżej rezerwy jest
    # dokładnie tym, czego użytkownik chce, więc nie wolno go tu blokować.
    #
    # R-13a: inwariant brzmi `SoC >= soc_reserve` — przy SoC RÓWNYM rezerwie
    # inwariant jest spełniony, więc warunek musi być ostry (`<`), nie `<=`.
    # Dawne `<=` traktowało dokładną równość jako naruszenie.
    #
    # S-4: goły próg zużywał pamięć nieulotną falownika, bo przy baterii stojącej na
    # progu każdy tick przerzucał `eco_soc` między planem a rezerwą (~1440 zapisów/dobę).
    # Zatrzask utrzymuje raz uruchomioną ochronę aż do WYRAŹNEGO powrotu ponad próg.
    # S-4b: „wyraźny powrót" to nie tylko pasmo, ale i czas — samo pasmo przepuszczało
    # w całości każdą oscylację od niego szerszą (5 pp = znowu 1440 zapisów na dobę).
    if ctx.reserve_hysteresis is not None:
        ponizej_rezerwy = ctx.reserve_hysteresis.engaged(
            state.soc, cfg.soc_reserve, ctx.now_s
        )
    else:
        ponizej_rezerwy = state.soc < cfg.soc_reserve

    if ponizej_rezerwy:
        removed: list[str] = []
        for key in _DISCHARGE_HINTS:
            if out.pop(key, None) is not None:
                removed.append(key)
        if wants_discharge:
            # R-1: intencja rozładowania musi zostać wygaszona u źródła. Bez tego
            # falownik dostałby (albo zachował) tryb rozładowania i zjadł rezerwę
            # backup mimo zadziałania guarda.
            result.forced_action = Action.SELF_CONSUME
        # R-13a: PARTIAL ma znaczyć "coś realnie zmieniłem", nie "wynikowy eco_soc
        # wypadł równy rezerwie". Bez osobnej flagi `eco_soc_raised` guard fałszywie
        # zgłaszał PARTIAL, gdy komenda już niosła `eco_soc == soc_reserve` i nic
        # więcej nie było do zrobienia.
        eco_soc_raised = out.get("eco_soc", 0) < cfg.soc_reserve
        if eco_soc_raised:
            out["eco_soc"] = cfg.soc_reserve
        # RR-8 (ustalenie 2. kontrolera z rundy 2): `forced_params` dla `eco_soc`
        # dodajemy NIEZALEŻNIE od `eco_soc_raised` — dokładnie ten sam wzorzec, który
        # dla I-4 świadomie zrobiono niezależnie od `changed` (patrz niżej, linia
        # ok. 665). Plan mógł już sam nieść `eco_soc == soc_reserve` (nic do
        # "podniesienia"), a mimo to ochrona I-1 nadal WYMAGA, żeby ta wartość
        # FAKTYCZNIE dotarła do falownika — bez tego rezerwa backup znika po cichu
        # przy braku zmapowanej encji dokładnie wtedy, gdy plan "przypadkiem" trafił
        # w bezpieczną wartość, czyli RR-3 chroniło tylko połowę tego samego wektora.
        result.forced_params.add("eco_soc")
        if removed or wants_discharge or eco_soc_raised:
            result.status = Status.PARTIAL
            # S-4: komunikat musi rozróżniać „SoC pod rezerwą" od „zatrzask jeszcze
            # trzyma" — inaczej log twierdzi `SoC=21% < rezerwa 20%`, czyli kłamie
            # dokładnie tam, gdzie ma tłumaczyć decyzję.
            # S-4b: trzeci powód — SoC jest już PONAD pasmem, a zatrzask trzyma
            # wyłącznie dlatego, że nie upłynął minimalny czas trwania stanu. Bez tego
            # rozróżnienia log przy SoC=40% twierdziłby „w paśmie histerezy 20%".
            pasmo = ctx.reserve_hysteresis.band_pp if ctx.reserve_hysteresis else 0.0
            if state.soc < cfg.soc_reserve:
                powod = f"SoC={state.soc}% < rezerwa {cfg.soc_reserve}%"
                powod_key = "ponizej_rezerwy"
            elif state.soc < cfg.soc_reserve + pasmo:
                powod = (
                    f"SoC={state.soc}% w paśmie histerezy rezerwy {cfg.soc_reserve}% "
                    f"(zatrzask trzyma do powrotu wyraźnie ponad próg) [S-4]"
                )
                powod_key = "pasmo_histerezy"
            else:
                powod = (
                    f"SoC={state.soc}% ponad pasmem, ale zatrzask rezerwy nie przetrwał "
                    f"jeszcze minimalnego czasu trwania stanu [S-4b]"
                )
                powod_key = "czas_trwania"
            result.note(
                "I-1",
                f"{powod}: "
                f"rozładowanie zablokowane={wants_discharge}, usunięto {removed or 'brak'}, "
                f"eco_soc podniesiony do {cfg.soc_reserve}%",
                # RR-10: tożsamość zdarzenia NIE MOŻE zawierać `state.soc` — przy
                # zatrzasku trzymającym pół godziny SoC zmienia się w każdym tiku,
                # więc klucz oparty na treści dawałby INFO co 60 s. Powód (a nie
                # wartość) jest tym, co realnie się zmienia i zasługuje na wpis.
                key=f"rezerwa_{powod_key}",
            )

    # I-3: przycięcie do granic sprzętowych.
    #
    # R-8: granice biorą się z atrybutów `min`/`max` encji `number` — falownik sam mówi,
    # co przyjmie, więc nie musimy czekać na mapę nastaw z Etapu 3. Wcześniej ta część
    # I-3 była martwa (literalne `pass` dla `max_charge_w`, a `read_inverter_limits`
    # i tak nigdy żadnej granicy mocowej nie ustawiało).
    #
    # RR-2: druga linia obrony przed NaN/inf. `sanitize_params` już je odrzuca, ale to
    # TUTAJ rodził się zapis na encję i żadna gałąź I-3 sama go nie zatrzyma:
    # `min(max(nan, lo), hi)` zwraca NaN, a `soc_max_hw < nan` jest fałszem, więc bez
    # jawnego testu wartość przechodziła przez cały guard nietknięta. Sprawdzamy CAŁY
    # zestaw, nie tylko parametry z granicami — wołający spoza executora (albo przyszła
    # ścieżka omijająca I-10) nie może tego obejść.
    for key, raw in out.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        if not math.isfinite(float(raw)):
            result.status = Status.ERROR
            result.note("I-3", f"{key}={raw!r} nie jest liczbą skończoną — odrzucam komendę")
            result.params = {}
            return result

    # RR-2 (decyzja właściciela): przycięcie daje `partial`, nie `success`. Litera
    # wektora T-3 mówi „success z adnotacją", ale wtedy chmura nie odróżnia
    # „zastosowano 3000" od „zastosowano 100 zamiast 3000" — a informacja
    # o NIEZREALIZOWANYM setpoincie jest ważniejsza niż litera specyfikacji.
    # Podział ról: status niesie sygnał, nota niesie szczegół.
    for key, bounds in limits.param_bounds.items():
        raw = out.get(key)
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        clipped = min(max(float(raw), bounds.lo), bounds.hi)
        if clipped != float(raw):
            result.note(
                "I-3",
                f"{key} przycięty {raw}->{clipped} (granica encji {bounds.lo}..{bounds.hi})",
            )
            out[key] = clipped
            if result.status is Status.SUCCESS:
                result.status = Status.PARTIAL

    if limits.soc_max_hw < out.get("eco_soc", 0):
        clipped = limits.soc_max_hw
        result.note("I-3", f"eco_soc przycięty {out['eco_soc']}->{clipped} (limit sprzętowy)")
        out["eco_soc"] = clipped
        # RR-2: ta sama zasada dla drugiej gałęzi I-3 — chmura nie może zgadywać,
        # którym torem poszło przycięcie, żeby wiedzieć, czy plan się wykonał.
        if result.status is Status.SUCCESS:
            result.status = Status.PARTIAL

    # I-4: nie eksportuj przy cenie <= 0.
    if ctx.price_pln_kwh is not None and ctx.price_pln_kwh <= 0:
        changed = False
        if out.get("export_limit", None) != 0:
            out["export_limit"] = 0.0
            changed = True
        if out.get("export_limit_enabled") is not True:
            out["export_limit_enabled"] = True
            changed = True
        # RR-3: oznaczamy OBA parametry jako wymuszone niezależnie od `changed` —
        # brak zmapowanej encji jest równie groźny, gdy plan sam trafił w bezpieczną
        # wartość (wtedy `changed=False`), bo ochrona nadal wymaga, żeby ta wartość
        # FAKTYCZNIE dotarła do falownika, a nie tylko żeby guard nie musiał jej zmieniać.
        result.forced_params.add("export_limit")
        result.forced_params.add("export_limit_enabled")
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
        if not self._directional_change(action):
            return True, None

        self._prune(now_ts)
        return self._decide(action, len(self._history))

    def would_allow(self, action: Action, now_ts: float) -> tuple[bool, Note | None]:
        """RR-7: jak `allows`, ale bez efektu ubocznego — dla suchego przebiegu
        (`async_diagnose`). `allows` woła `_prune`, które PRZYPISUJE `self._history`
        (nową, przyciętą listę) — mutacja stanu, nawet gdy logiczny wynik się nie
        zmienia. Ta metoda liczy przycięcie w locie, nie zapisując wyniku, żeby
        kontrakt "suchy przebieg" (zero mutacji) był prawdziwy."""
        if not self._directional_change(action):
            return True, None

        cutoff = now_ts - self.window_s
        changes = sum(1 for ts, _ in self._history if ts >= cutoff)
        return self._decide(action, changes)

    def _directional_change(self, action: Action) -> bool:
        """Czy `action` w ogóle MOŻE zużywać budżet I-8 (patrz `allows`/`would_allow`)."""
        directional = (Action.CHARGE, Action.DISCHARGE)
        if action not in directional:
            return False
        if self._current == action:
            return False
        # R-13b: `_current is None` znaczy "kierunek jeszcze nigdy nie był ustawiony"
        # — pierwsze ustawienie nie jest ZMIANĄ kierunku (nie ma poprzedniego
        # kierunku, względem którego mogłaby zajść zmiana), więc nie zużywa budżetu.
        # Bez tego wyjątku efektywny budżet po starcie był o 1 mniejszy niż N.
        if self._current is None:
            return False
        return True

    def _decide(self, action: Action, changes: int) -> tuple[bool, Note | None]:
        if changes >= self.max_changes:
            return False, Note(
                "I-8",
                f"{changes} zmian kierunku w ostatniej godzinie (limit {self.max_changes}) "
                f"— zmiana na {action.value} zignorowana",
            )
        return True, None

    def record(self, action: Action, now_ts: float) -> None:
        # R-13b: symetrycznie do `allows` — pierwsze ustawienie kierunku (z
        # `_current is None`) nie trafia do historii zmian, bo nią nie jest.
        if (
            action in (Action.CHARGE, Action.DISCHARGE)
            and self._current is not None
            and self._current != action
        ):
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


class WritePermit:
    """S-5b: odbieralne pozwolenie sekwencji nastaw na dotknięcie falownika.

    DLACZEGO w ogóle istnieje: naprawa S-5 zrobiła sekwencję zapisu nieprzerywalną
    (`asyncio.shield` + zadanie odpalane poza śledzeniem HA), żeby anulowanie nie
    zostawiało falownika w nowym trybie ze starymi limitami. Kupiła tym jednak wadę
    o piętro wyżej: sekwencja przeżywała `async_stop()` i pisała do falownika już po
    wyładowaniu integracji, ścigając się z NOWYM executorem powołanym przez reload.
    Zapis, który przeżył swojego właściciela, nie ma prawa dotknąć falownika.

    DLACZEGO przepustka, a nie `task.cancel()`: anulowanie przerywa zadanie w dowolnym
    miejscu, także w środku `await` na service callu — czyli dokładnie tam, gdzie
    przerwanie jest niewidoczne i nierozliczalne (sonda E, pierwotne S-5). Przepustka
    przerywa sekwencję WYŁĄCZNIE na granicy między nastawami, więc przerwanie jest
    zawsze policzalne: wiadomo, co poszło do falownika, a co nie.

    Obiekt trzymają DWIE strony naraz: executor (żeby móc odebrać prawo) i sama
    sekwencja (żeby przeżyć wyzerowanie uchwytu w executorze). Dlatego jest zwykłym
    obiektem, a nie polem executora — sekwencja musi widzieć odebranie prawa także
    wtedy, gdy jej właściciel już nie istnieje.
    """

    def __init__(self) -> None:
        self._valid = True
        self.reason = ""
        #: Ustawia `applier.apply_params`, gdy przepustka faktycznie PRZERWAŁA sekwencję.
        #: Odbieranie przepustki tuż po ostatniej nastawie niczego nie przerywa, a
        #: nota o złamanym PARAM_ORDER musi opisywać to, co naprawdę zaszło.
        self.aborted = False

    def valid(self) -> bool:
        return self._valid

    def revoke(self, reason: str) -> None:
        """Odbierz prawo zapisu. Powód jedzie dalej do logu i do śladu w `_last`."""
        if not self._valid:
            return
        self._valid = False
        self.reason = reason


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
    "ParamBounds",
    "RESERVE_HYSTERESIS_PP",
    "RESERVE_LATCH_ENGAGED_MIN_S",
    "RESERVE_LATCH_RELEASED_MIN_S",
    "RequestDeduplicator",
    "ReserveHysteresis",
    "Status",
    "UserConfig",
    "WritePermit",
    "WriteThrottle",
    "apply_guards",
    "infer_action",
    "ordered",
    "sanitize_params",
]
