# Volter Energy — Home Assistant Integration

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![HA](https://img.shields.io/badge/Home%20Assistant-2024.1+-blue.svg)](https://www.home-assistant.io/)

Bi-directional integration between Home Assistant and [Volter Energy](https://volter.app) — cloud-based energy optimization for solar + battery systems.

## Features

- **Telemetry** — Sends inverter data (PV, battery, grid, load) to Volter cloud every 60s for AI-powered optimization
- **Remote Control** — Receives commands from Volter app to change inverter operation mode, charge/discharge limits
- **Universal** — Works with any inverter integration in HA through entity mapping (no IP/Modbus configuration needed)
- **Auto-calibration** — Telemetry data is used to calibrate PV production forecasts via adaptive Kalman filter

## Prerequisites

1. A Volter account with an API key (generate in the Volter app: Settings > API Key)
2. An inverter integration already set up in Home Assistant (see [Recommended Integrations](#recommended-integrations))

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** > **Custom repositories**
3. Add `https://github.com/volter-labs/volter-ha-integration` as an **Integration**
4. Search for "Volter Energy" and install
5. Restart Home Assistant

### Manual

1. Copy `custom_components/volter/` to your HA `custom_components/` directory
2. Restart Home Assistant

## Configuration

### Step 1: Add Integration

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for "Volter Energy"
3. Enter your API key (`vk_...` format)
4. The integration will verify the key and register your device

### Step 2: Map Entities (Options)

After setup, click **Configure** on the Volter integration to map your inverter entities:

**Monitoring (required):**

| Field | Description | Example entity |
|---|---|---|
| Battery SoC | Battery state of charge sensor | `sensor.goodwe_battery_state_of_charge` |
| PV Power | Solar production power sensor | `sensor.goodwe_pv_power` |
| Grid Power | Grid import/export power sensor | `sensor.goodwe_active_power` |

**Monitoring (optional):**

| Field | Description |
|---|---|
| Battery Power | Battery charge/discharge power |
| House Load | Total house consumption |
| PV Total Energy | PV energy counter (kWh) |
| Grid Import Total | Grid import counter (kWh) |
| Grid Export Total | Grid export counter (kWh) |

**Control (optional — enables remote commands from app):**

| Field | Description | Example entity |
|---|---|---|
| EMS Mode | Inverter operation mode select | `select.goodwe_inverter_operation_mode` |
| Charge Limit | Battery charge current limit | `number.goodwe_battery_charge_limit` |
| Discharge Limit | Battery DOD / discharge depth | `number.goodwe_battery_discharge_depth` |
| Export Limit | Grid export limit (W) | `number.goodwe_grid_export_limit` |
| Export Limit Switch | Enable/disable export limit | `switch.goodwe_export_limit` |
| Eco Mode Power | Eco mode power setting (%) | `number.goodwe_eco_mode_power` |
| Eco Mode SOC | Eco mode SOC target (%) | `number.goodwe_eco_mode_soc` |

## Recommended Integrations

The Volter integration works with **any** inverter integration in Home Assistant. Here are tested recommendations:

### GoodWe Inverters

| Integration | Type | Link |
|---|---|---|
| **GoodWe (official)** | HA Core | [Documentation](https://www.home-assistant.io/integrations/goodwe/) |
| **GoodWe Inverter (experimental)** | HACS | [mletenay/home-assistant-goodwe-inverter](https://github.com/mletenay/home-assistant-goodwe-inverter) |

The experimental integration by @mletenay exposes additional control entities (eco mode power/SOC, battery charge limit) that enable full optimization capabilities.

### Other Inverters

Any integration that exposes power sensors (W) and battery SoC (%) will work for monitoring. For full control capabilities, the integration must expose select/number/switch entities for the inverter operation mode and limits.

## How It Works

```
Home Assistant                    Volter Cloud                     Volter App
─────────────                    ────────────                     ──────────

 State changes ──── batch 60s ──→ telemetry_raw ──→ Optimizer      Dashboard
 (PV, battery,                                      ↓               ↓
  grid, load)                                    Decisions     View & Control
                                                    ↓               ↓
 Service calls ←── Realtime WS ← device channel ←── Commands ←── User taps
 (select, number)
```

## Troubleshooting

- **"Invalid API key"** — Generate a new key in the Volter app (Settings > API Key)
- **"Cannot connect"** — Check your internet connection and that the Volter service is reachable
- **No telemetry data** — Ensure you've mapped at least the 3 required monitoring entities in Options
- **Commands not working** — Check that control entities are mapped and the inverter integration supports them

Check the Home Assistant logs (`Settings > System > Logs`) and filter by `volter` for detailed diagnostics.

## License

MIT
