"""Stałe integracji Volter Energy."""

DOMAIN = "volter"
MANUFACTURER = "Volter Labs"

# ── Supabase endpoints ──────────────────────────────────────────────────────
SUPABASE_PROJECT_REF = "japquphqgvvsaoxotnbl"
DEFAULT_SUPABASE_URL = f"https://{SUPABASE_PROJECT_REF}.supabase.co"
CLAIM_DEVICE_PATH = "/functions/v1/claim-device"
DEVICE_TELEMETRY_PATH = "/functions/v1/device-telemetry"
GET_SCHEDULE_PATH = "/functions/v1/get-schedule"

# ── Config keys ─────────────────────────────────────────────────────────────
CONF_API_KEY = "api_key"
CONF_DEVICE_ID = "device_id"
CONF_SUPABASE_URL = "supabase_url"
CONF_SUPABASE_ANON_KEY = "supabase_anon_key"

# ── Options keys: Monitoring entities ────────────────────────────────────────
OPT_ENTITY_SOC = "entity_soc"
OPT_ENTITY_PV_POWER = "entity_pv_power"
OPT_ENTITY_GRID_POWER = "entity_grid_power"
OPT_ENTITY_BATTERY_POWER = "entity_battery_power"
OPT_ENTITY_LOAD_POWER = "entity_load_power"
OPT_ENTITY_PV_ENERGY_TOTAL = "entity_pv_energy_total"
OPT_ENTITY_GRID_IMPORT_TOTAL = "entity_grid_import_total"
OPT_ENTITY_GRID_EXPORT_TOTAL = "entity_grid_export_total"

# ── Options keys: Control entities ──────────────────────────────────────────
OPT_ENTITY_EMS_MODE = "entity_ems_mode"
OPT_ENTITY_CHARGE_LIMIT = "entity_charge_limit"
OPT_ENTITY_DISCHARGE_LIMIT = "entity_discharge_limit"
OPT_ENTITY_EXPORT_LIMIT = "entity_export_limit"
OPT_ENTITY_EXPORT_LIMIT_SWITCH = "entity_export_limit_switch"
OPT_ENTITY_ECO_MODE_POWER = "entity_eco_mode_power"
OPT_ENTITY_ECO_MODE_SOC = "entity_eco_mode_soc"
#: Gorny prog SoC (do ilu ladowac). GoodWe: `number.goodwe_soc_upper_limit`, 0-100%.
#: Osobna encja, bo `eco_mode_soc` jest w integracji mletenay TYLKO DO ODCZYTU
#: (`setter=None`) — zapis tam nigdy by nie doszedl do falownika.
OPT_ENTITY_SOC_UPPER = "entity_soc_upper"

# ── Telemetry ───────────────────────────────────────────────────────────────
TELEMETRY_BATCH_INTERVAL = 60  # sekund — zapis do telemetry_raw
TELEMETRY_MAX_BATCH_SIZE = 120
LIVE_BROADCAST_INTERVAL = 5  # sekund — broadcast-only (bez zapisu, live dashboard)

# ── Realtime (Phoenix channels) ─────────────────────────────────────────────
REALTIME_HEARTBEAT_INTERVAL = 30  # sekund
REALTIME_RECONNECT_BASE = 2  # sekundy (exponential backoff)
REALTIME_RECONNECT_MAX = 120  # max sekundy między próbami

# ── Mapowanie komend na service calls ────────────────────────────────────────
# command param -> (option_key, ha_domain, ha_service, data_key)
COMMAND_ENTITY_MAP = {
    "mode": (OPT_ENTITY_EMS_MODE, "select", "select_option", "option"),
    #: Nastawa mocy `Xset` trybow EMS 11/12, w WATACH po stronie baterii.
    #: Nazwa parametru jest historyczna (kiedys limit pradu ladowania); encja to
    #: `number.goodwe_ems_power_limit`, ktora obsluguje OBA kierunki.
    "charge_limit": (OPT_ENTITY_CHARGE_LIMIT, "number", "set_value", "value"),
    #: Glebokosc rozladowania (DoD, %) podana WPROST — sciezka dla starego kontraktu.
    "discharge_limit": (OPT_ENTITY_DISCHARGE_LIMIT, "number", "set_value", "value"),
    "export_limit": (OPT_ENTITY_EXPORT_LIMIT, "number", "set_value", "value"),
    #: Dolny prog SoC (%). UWAGA: `OPT_ENTITY_ECO_MODE_SOC` to od tej wersji
    #: "encja sterujaca dolnym progiem SoC", a nie doslownie `eco_mode_soc` —
    #: tamta jest w integracji mletenay TYLKO DO ODCZYTU (`setter=None`).
    #: Na GoodWe wskazuje sie tu encje GLEBOKOSCI ROZLADOWANIA (DoD), a odwrocenie
    #: wartosci robi PARAM_VALUE_TRANSFORM przy zapisie — dzieki temu guardy, plan
    #: i testy operuja progiem, a nie glebokoscia.
    "eco_soc": (OPT_ENTITY_ECO_MODE_SOC, "number", "set_value", "value"),
    #: Gorny prog SoC (%) — do ilu ladowac.
    "soc_upper": (OPT_ENTITY_SOC_UPPER, "number", "set_value", "value"),
}

#: Przeliczenie wartosci parametru na wartosc encji falownika.
#:
#: `eco_soc` to DOLNY PROG SoC (np. 10% = "nie schodz ponizej 10%"), a GoodWe
#: przyjmuje GLEBOKOSC rozladowania (DoD 90% = to samo). Kierunek tego przeliczenia
#: jest krytyczny: pomylka zamienia "nie schodz ponizej 10%" w "zuzyj najwyzej 10%".
#: Zakres encji to 0..99, wiec prog 0% jest nieosiagalny — i dobrze.
PARAM_VALUE_TRANSFORM = {
    "eco_soc": lambda prog: round(100.0 - float(prog), 1),
}

#: Parametry, ktorych encje w integracji mletenay sa TYLKO DO ODCZYTU (`setter=None`).
#: Zapis by sie nie powiodl, wiec nie emitujemy ich w ogole — patrz mappers.py.
PARAMETRY_TYLKO_ODCZYT = frozenset({"eco_power"})

# Mapowanie encji monitoringu na klucze telemetrii
MONITORING_ENTITY_MAP = {
    OPT_ENTITY_SOC: "battery_soc",
    OPT_ENTITY_PV_POWER: "pv_power_w",
    OPT_ENTITY_GRID_POWER: "grid_power_w",
    OPT_ENTITY_BATTERY_POWER: "battery_power_w",
    OPT_ENTITY_LOAD_POWER: "load_power_w",
    OPT_ENTITY_PV_ENERGY_TOTAL: "pv_energy_total_kwh",
    OPT_ENTITY_GRID_IMPORT_TOTAL: "grid_import_total_kwh",
    OPT_ENTITY_GRID_EXPORT_TOTAL: "grid_export_total_kwh",
    OPT_ENTITY_EMS_MODE: "ems_mode",
}

# ── Executor, harmonogram i guardy ──────────────────────────────────────────
# Specyfikacja: Volter-BOX/03-produkt/guardy-i-inwarianty.md

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.schedule"

#: Interwał pętli wykonawczej. Wymóg ze specyfikacji: <= 60 s.
EXECUTOR_INTERVAL = 60

#: I-6 — minimalny odstęp między zapisami tego samego parametru.
#: Nastawy eco mode w GoodWe idą do pamięci nieulotnej falownika: częste zapisy
#: zużywają sprzęt. Nie zmniejszać bez potwierdzenia w Etapie 1.
WRITE_MIN_INTERVAL_S = 60

#: I-9 — maksymalny wiek odczytu telemetrii i maksymalny sensowny skok SoC.
MAX_STATE_AGE_S = 300
#: RR-1: próg BEZWZGLĘDNY został awaryjny — stosuje się tylko wtedy, gdy odstępu
#: między próbkami nie znamy. Zwykłą regułą jest tempo (niżej), bo różnica bezwzględna
#: myli „niemożliwy skok" z „normalną zmianą po przerwie w telemetrii".
MAX_SOC_JUMP_PP = 20

#: RR-1 — maksymalne realne TEMPO zmiany SoC w punktach procentowych na minutę.
#: Wyprowadzenie z fizyki: domowy magazyn ładuje/rozładowuje się mocą rzędu 1C
#: (pojemność [kWh] ≈ moc [kW]), czyli pełne 100 pp w ~60 min = 1,67 pp/min. Nawet
#: agresywne 2C daje 3,3 pp/min. Bierzemy 4,0 pp/min jako wartość konserwatywną:
#: ma odsiewać rozjazdy czujnika, a nie karać realną instalację. Wartość jest spójna
#: z resztą I-9 — przez `MAX_STATE_AGE_S` (5 min) daje dokładnie `MAX_SOC_JUMP_PP`,
#: więc w oknie świeżości nic nie robi się luźniejsze niż przed naprawą.
MAX_SOC_RATE_PP_PER_MIN = 4.0

#: RR-1 — podłoga tolerancji niezależna od czasu. Sensory SoC raportują wartości
#: skwantowane (często co 1%), a przy odstępie kilku sekund limit z tempa zszedłby
#: poniżej rozdzielczości czujnika i degradował instalację na zaokrągleniu.
MIN_SOC_JUMP_TOLERANCE_PP = 5.0

#: RR-2 (reszta R-8) — czas życia zapamiętanych granic `min`/`max` encji `number`.
#:
#: Po co w ogóle pamięć: bez niej werdykt I-10 zależał od DOSTĘPNOŚCI encji — ta sama
#: komenda dostawała raz `success`, raz `error`, zależnie od tego, czy integracja
#: falownika akurat odpowiada. To niedeterminizm widoczny dla użytkownika.
#:
#: Dlaczego 6 h: `min`/`max` to zakres rejestru falownika, czyli właściwość sprzętu —
#: zmienia się przy rekonfiguracji, aktualizacji firmware albo wymianie urządzenia,
#: a nie w czasie pracy. TTL musi być DŁUŻSZY niż realna przerwa w telemetrii (sonda
#: RR-1: 40 min niedostępności; restart HA/falownika: minuty) i KRÓTSZY niż czas,
#: w którym sprzęt mógłby się realnie zmienić bez restartu HA. Cache żyje wyłącznie
#: w pamięci procesu, więc górną granicą ryzyka i tak jest jedna sesja HA.
PARAM_BOUNDS_CACHE_TTL_S = 21600.0

#: I-8 — maksymalna liczba zmian kierunku ładowanie<->rozładowanie na godzinę.
MAX_DIRECTION_CHANGES_PER_HOUR = 4

#: T-14 — liczba prób zapisu przed zgłoszeniem błędu (bez pętli).
WRITE_RETRIES = 3

#: S-4 — szerokość histerezy progu rezerwy (I-1) w punktach procentowych.
#: Pełne uzasadnienie doboru przy `guards.RESERVE_HYSTERESIS_PP`. W skrócie: sensor SoC
#: ma rozdzielczość ~1 pp, więc pasmo musi być jej wielokrotnością, inaczej zatrzask
#: zwalniałby na samym zaokrągleniu i zapisy do NVM wracałyby.
RESERVE_HYSTERESIS_PP = 3.0

#: S-4b — minimalne czasy trwania stanów zatrzasku rezerwy (I-1), w sekundach.
#: Pełne uzasadnienie doboru przy `guards.RESERVE_LATCH_ENGAGED_MIN_S`. W skrócie:
#: samo pasmo chroni tylko oscylacje od siebie węższe (5 pp = znowu 1440 zapisów na
#: dobę), a dla każdego pasma istnieje większa amplituda. Dopiero pasmo + czas trwania
#: stanu dają budżet zapisów NIEZALEŻNY od amplitudy (~20/dobę).
#: Krótszy czas dotyczy stanu ZAŁĄCZONEGO, bo to on kosztuje ekonomicznie (bateria stoi
#: na rezerwie); dłuższy dotyczy stanu ZWOLNIONEGO i obowiązuje wyłącznie dla płytkich,
#: mieszczących się w paśmie zejść — wyraźne zejście pod rezerwę omija go natychmiast.
RESERVE_LATCH_ENGAGED_MIN_S = 1800.0
RESERVE_LATCH_RELEASED_MIN_S = 7200.0

#: S-5 — ile `executor.async_stop()` czeka na TRWAJĄCY zapis, zanim odpuści.
#:
#: Dobór: sekwencja PARAM_ORDER to najwyżej 7 nastaw, każda z `WRITE_RETRIES=3` próbami
#: i backoffem 1 s + 2 s. Zdrowy falownik odpowiada w milisekundach, jeden ponowiony zapis
#: to ~3 s. 10 s pokrywa więc realny najgorszy przypadek zdrowego sprzętu i JEDNOCZEŚNIE
#: stawia twardą granicę: zablokowana integracja nie może wstrzymywać wyładowania ani
#: restartu Home Assistanta w nieskończoność. Po przekroczeniu logujemy błąd i puszczamy.
STOP_WRITE_TIMEOUT_S = 10.0

#: S-5 — ile `command_handler.async_stop()` czeka na anulowany task nasłuchu.
#: Krócej niż zapis, bo tu chodzi wyłącznie o to, żeby obsługa anulowania (raport
#: do chmury) w ogóle dostała szansę się wykonać, a nie o fizyczny zapis do falownika.
STOP_LISTEN_TIMEOUT_S = 5.0

#: S-5 — budżet na raport do chmury o anulowanej komendzie. Raport jest „best effort":
#: wyładowanie integracji nie może na niego czekać dłużej, niż trwa zwykły POST.
CANCEL_REPORT_TIMEOUT_S = 3.0

# ── Options: konfiguracja użytkownika dla guardów ───────────────────────────
OPT_SOC_RESERVE = "soc_reserve"
OPT_USER_MODE = "user_mode"          # earn | autarky | backup
OPT_RATED_POWER_W = "rated_power_w"  # moc znamionowa falownika, do przeliczenia eco_power

#: Wyłącznik sterowania. DOMYŚLNIE WYŁĄCZONY — integracja liczy plan, guardy
#: i throttle, loguje decyzję i pokazuje ją w `volter.diagnose`, ale NIE zapisuje
#: nic do falownika, dopóki właściciel świadomie tego nie włączy. Powód: mapowanie
#: trybów falownika jest hipotezą do potwierdzenia na żywym sprzęcie (Etap 3),
#: a integracja nie ma innego hamulca niż usunięcie mapowania encji.
OPT_CONTROL_ENABLED = "control_enabled"
DEFAULT_CONTROL_ENABLED = False

DEFAULT_SOC_RESERVE = 20.0
DEFAULT_RATED_POWER_W = 10000.0

# ── Task 16 — pobieranie harmonogramu z chmury ──────────────────────────────
#: Interwał pobierania planu z `get-schedule`. Plan zmienia się rzadko (planer
#: raz dziennie, reaktor przy odchyleniu), więc 5 min jest z dużym zapasem —
#: ten sam rząd wielkości co CRON planera po stronie chmury.
SCHEDULE_FETCH_INTERVAL = 300
