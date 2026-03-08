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
TELEMETRY_BATCH_INTERVAL = 60  # sekund
TELEMETRY_MAX_BATCH_SIZE = 120

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
