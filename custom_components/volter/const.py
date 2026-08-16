"""Stałe integracji Volter Energy."""

DOMAIN = "volter"
MANUFACTURER = "Volter Labs"

# ── Supabase endpoints ──────────────────────────────────────────────────────
SUPABASE_PROJECT_REF = "japquphqgvvsaoxotnbl"
DEFAULT_SUPABASE_URL = f"https://{SUPABASE_PROJECT_REF}.supabase.co"
CLAIM_DEVICE_PATH = "/functions/v1/claim-device"
DEVICE_TELEMETRY_PATH = "/functions/v1/device-telemetry"

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
    "charge_limit": (OPT_ENTITY_CHARGE_LIMIT, "number", "set_value", "value"),
    "discharge_limit": (OPT_ENTITY_DISCHARGE_LIMIT, "number", "set_value", "value"),
    "export_limit": (OPT_ENTITY_EXPORT_LIMIT, "number", "set_value", "value"),
    "eco_power": (OPT_ENTITY_ECO_MODE_POWER, "number", "set_value", "value"),
    "eco_soc": (OPT_ENTITY_ECO_MODE_SOC, "number", "set_value", "value"),
}

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

#: I-8 — maksymalna liczba zmian kierunku ładowanie<->rozładowanie na godzinę.
MAX_DIRECTION_CHANGES_PER_HOUR = 4

#: T-14 — liczba prób zapisu przed zgłoszeniem błędu (bez pętli).
WRITE_RETRIES = 3

# ── Options: konfiguracja użytkownika dla guardów ───────────────────────────
OPT_SOC_RESERVE = "soc_reserve"
OPT_USER_MODE = "user_mode"          # earn | autarky | backup
OPT_RATED_POWER_W = "rated_power_w"  # moc znamionowa falownika, do przeliczenia eco_power

DEFAULT_SOC_RESERVE = 20.0
DEFAULT_RATED_POWER_W = 10000.0
