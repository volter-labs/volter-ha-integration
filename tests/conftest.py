"""Wspólny harness testowy integracji Volter.

Robi dwie rzeczy:

1. **Stubuje moduły `homeassistant`** w `sys.modules`, dzięki czemu moduły
   komponentu (`executor`, `applier`, `ha_state`, `command_handler`) dają się
   importować i testować **bez zainstalowanego Home Assistanta** — te same testy
   uruchomi CI i laptop. Wzorzec z `volcast-ha-integration/tests/conftest.py`.

2. **Ładuje czyste moduły** (`guards`, `schedule`, `mappers`) pod sztucznym
   pakietem `volter_pure`, z pominięciem `custom_components/volter/__init__.py`.
   Te moduły są świadomie wolne od zależności od HA — chcemy je testować zwykłym
   `pytest`, tak jak później firmware będzie testowane na hoście.

Kolejność w tym pliku ma znaczenie: stuby HA muszą wylądować w `sys.modules`
**zanim** cokolwiek zaimportuje `custom_components.volter.*`, bo te moduły robią
`from homeassistant... import ...` już w czasie importu.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

# ── Stuby Home Assistanta ───────────────────────────────────────────────────
# Wzorzec z volcast-ha-integration/tests/conftest.py. Pozwala importować moduły
# komponentu bez instalacji HA — te same testy uruchomi CI i laptop.


def _make_module(name: str, attrs: dict[str, Any] | None = None) -> types.ModuleType:
    """Zarejestruj sztuczny moduł w `sys.modules` (z opcjonalnymi atrybutami)."""
    mod = types.ModuleType(name)
    for key, value in (attrs or {}).items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _FakeState:
    """Odpowiednik homeassistant.core.State."""

    def __init__(self, state: Any, attributes: dict | None = None, last_updated=None):
        self.state = state
        self.attributes = attributes or {}
        self.last_updated = last_updated or datetime.now(timezone.utc)


class _FakeStates:
    """hass.states — słownik encja → _FakeState."""

    def __init__(self) -> None:
        self._data: dict[str, _FakeState] = {}

    def set(
        self,
        entity_id: str,
        state: Any,
        attributes: dict | None = None,
        last_updated=None,
    ) -> None:
        self._data[entity_id] = _FakeState(state, attributes, last_updated)

    def get(self, entity_id: str) -> _FakeState | None:
        return self._data.get(entity_id) if entity_id else None


class _FakeServices:
    """hass.services — rejestruje wywołania, może symulować błąd zapisu."""

    def __init__(self) -> None:
        self.registered: dict[tuple[str, str], Any] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_with: Exception | None = None

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.registered

    def async_register(self, domain, service, handler, schema=None, supports_response=None):
        self.registered[(domain, service)] = handler

    def async_remove(self, domain: str, service: str) -> None:
        self.registered.pop((domain, service), None)

    async def async_call(self, domain, service, data, blocking=False, **_kwargs):
        self.calls.append((domain, service, dict(data)))
        if self.fail_with is not None:
            raise self.fail_with


class _FakeBus:
    """hass.bus — zapamiętuje wystrzelone zdarzenia i nasłuchy."""

    def __init__(self) -> None:
        self.fired: list[tuple[str, dict]] = []
        self.listeners: list[tuple[str, Any]] = []

    def async_fire(self, event_type: str, data: dict | None = None) -> None:
        self.fired.append((event_type, data or {}))

    def async_listen_once(self, event_type: str, listener) -> Any:
        self.listeners.append((event_type, listener))
        return lambda: None


class _FakeStore:
    """helpers.storage.Store — trzyma dane w pamięci."""

    def __init__(self, hass=None, version: int = 1, key: str = ""):
        self._version = version
        self._key = key
        self._data: Any = None

    async def async_load(self) -> Any:
        return self._data

    async def async_save(self, data: Any) -> None:
        self._data = data

    async def async_remove(self) -> None:
        self._data = None


class _FakeConfigEntry:
    """Odpowiednik homeassistant.config_entries.ConfigEntry."""

    def __init__(self, options: dict | None = None, data: dict | None = None):
        self.entry_id = "test_entry"
        self.data = data or {}
        self.options = options or {}

    def add_update_listener(self, _listener): ...

    def async_on_unload(self, _fn): ...


class _FakeConfigFlow:
    """Odpowiednik homeassistant.config_entries.ConfigFlow.

    Prawdziwy ConfigFlow obsługuje `class X(ConfigFlow, domain=...)` przez
    `__init_subclass__` — zwykły `object` tego nie umie i wywala TypeError.
    """

    def __init_subclass__(cls, *, domain: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)


class _FakeOptionsFlow:
    """Odpowiednik homeassistant.config_entries.OptionsFlowWithConfigEntry."""

    def __init__(self, config_entry: _FakeConfigEntry) -> None:
        self.config_entry = config_entry


class FakeHass:
    """Minimalny odpowiednik obiektu `hass` — tyle, ile potrzebują nasze moduły."""

    class _Config:
        time_zone = "Europe/Warsaw"

    config = _Config()

    def __init__(self) -> None:
        self.data: dict = {}
        self.states = _FakeStates()
        self.services = _FakeServices()
        self.bus = _FakeBus()
        self.created_tasks: list[Any] = []

    def async_create_task(self, coro, *_a, **_kw):
        # Nie planujemy realnie — zapamiętujemy i zamykamy korutynę,
        # żeby nie sypało ostrzeżeniem "coroutine was never awaited".
        self.created_tasks.append(coro)
        try:
            coro.close()
        except Exception:  # noqa: BLE001
            pass
        return MagicMock()


class ZegarSterowany:
    """Sterowany zegar monotoniczny — podmieniany za moduł `time` widziany przez kod.

    S-4b: część guardów (I-6 odstęp zapisów, I-8 okno kierunków, I-9 tempo SoC,
    zatrzask rezerwy) to funkcje CZASU. Test bez sterowanego zegara przepuszcza dobę
    przez pętlę w mikrosekundach, czyli nie mierzy żadnej z tych reguł — mierzy
    wyłącznie kolejność wywołań.
    """

    def __init__(self, start: float = 10_000.0) -> None:
        self._t = start

    def monotonic(self) -> float:
        return self._t

    def przesun(self, sekundy: float = 60.0) -> None:
        self._t += sekundy


_make_module("homeassistant")
_make_module("homeassistant.core", {
    "HomeAssistant": FakeHass,
    "State": _FakeState,
    "CALLBACK_TYPE": Any,
    "Event": MagicMock(),
    "EventStateChangedData": MagicMock(),
    "ServiceCall": MagicMock(),
    "SupportsResponse": MagicMock(),
    "callback": lambda fn: fn,
})
_make_module("homeassistant.const", {
    "Platform": MagicMock(),
    "UnitOfPower": MagicMock(),
})
_make_module("homeassistant.exceptions", {"HomeAssistantError": Exception})
_make_module("homeassistant.config_entries", {
    "ConfigEntry": _FakeConfigEntry,
    "ConfigFlow": _FakeConfigFlow,
    "ConfigFlowResult": dict,
    "OptionsFlow": _FakeConfigFlow,
    "OptionsFlowWithConfigEntry": _FakeOptionsFlow,
})
_make_module("homeassistant.helpers")
_make_module("homeassistant.helpers.storage", {"Store": _FakeStore})
# Loader HA — stad bierzemy wersje integracji do adresu karty. Odczyt `manifest.json`
# wprost z dysku byl blokujacym wejsciem-wyjsciem w petli zdarzen.
_make_module("homeassistant.loader", {"async_get_integration": MagicMock()})
_make_module("homeassistant.helpers.event", {
    "async_track_time_interval": MagicMock(return_value=MagicMock()),
    "async_track_state_change_event": MagicMock(return_value=MagicMock()),
    "async_call_later": MagicMock(return_value=MagicMock()),
})
# Selektory formularza. Renderowanie nas nie interesuje, ale krok Options Flow,
# ktory WRACA z bledem, musi zbudowac schemat — inaczej nie da sie przetestowac
# ani jednej walidacji pola.
_make_module("homeassistant.helpers.selector", {
    "selector": MagicMock(),
    "EntitySelector": MagicMock(),
    "EntitySelectorConfig": MagicMock(),
    "NumberSelector": MagicMock(),
    "NumberSelectorConfig": MagicMock(),
    "NumberSelectorMode": MagicMock(),
    "SelectSelector": MagicMock(),
    "SelectSelectorConfig": MagicMock(),
    "SelectSelectorMode": MagicMock(),
    "TextSelector": MagicMock(),
    "TextSelectorConfig": MagicMock(),
    "BooleanSelector": MagicMock(),
})

# --- platformy i frontend (encje Voltera + karta Lovelace) ---
class _FakeEntity:
    _attr_has_entity_name = False
    _attr_unique_id = None
    _attr_device_info = None
    _attr_translation_key = None

    def async_write_ha_state(self):  # encje wołają to po zmianie stanu
        pass


class _FakeSwitchEntity(_FakeEntity):
    pass


_make_module("homeassistant.components")
_make_module("homeassistant.components.sensor", {
    "SensorEntity": _FakeEntity,
    "SensorDeviceClass": MagicMock(),
    "SensorStateClass": MagicMock(),
})
_make_module("homeassistant.components.binary_sensor", {
    "BinarySensorEntity": _FakeEntity,
    "BinarySensorDeviceClass": MagicMock(),
})
_make_module("homeassistant.components.switch", {"SwitchEntity": _FakeSwitchEntity})
_make_module("homeassistant.components.http", {"StaticPathConfig": MagicMock()})
_make_module("homeassistant.components.frontend", {"add_extra_js_url": MagicMock()})
_make_module("homeassistant.helpers.entity", {"Entity": _FakeEntity})
_make_module("homeassistant.helpers.device_registry", {"DeviceInfo": dict})
_make_module("homeassistant.helpers.entity_platform", {"AddEntitiesCallback": Any})

# ── Czyste moduły bez HA (volter_pure) ──────────────────────────────────────
# `custom_components/volter/__init__.py` importuje HA, więc zwykłe
# `from custom_components.volter.guards import ...` przeciągnęłoby cały komponent.
# Rejestrujemy je pod sztucznym pakietem `volter_pure`, pomijając `__init__.py`.
# Kolejność ładowania ma znaczenie: `schedule` robi `from .guards import Action`.

PACKAGE = "volter_pure"
_MODULES = ("guards", "schedule", "mappers")

_base = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "volter"

if PACKAGE not in sys.modules:
    _pkg = types.ModuleType(PACKAGE)
    _pkg.__path__ = [str(_base)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE] = _pkg

    for _name in _MODULES:
        _spec = importlib.util.spec_from_file_location(
            f"{PACKAGE}.{_name}", _base / f"{_name}.py"
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{PACKAGE}.{_name}"] = _mod
        _spec.loader.exec_module(_mod)


# ── Fikstury ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_hass() -> FakeHass:
    """Świeży `hass` na każdy test — stan encji i wywołania serwisów nie przeciekają."""
    return FakeHass()


@pytest.fixture
def fake_entry() -> _FakeConfigEntry:
    """Świeży config entry z pustymi `options` — test dopisuje sobie mapowanie encji."""
    return _FakeConfigEntry()

@pytest.fixture(autouse=True)
def _sterowanie_wlaczone_w_testach(monkeypatch):
    """Testy toru zapisu zakładają, że zapis do falownika NASTĘPUJE.

    Produkcyjny `DEFAULT_CONTROL_ENABLED` to False — integracja nie dotyka
    falownika, dopóki właściciel świadomie nie włączy sterowania w opcjach.
    Gdyby testy dziedziczyły ten default, kilkadziesiąt z nich testowałoby
    wyłącznik zamiast tego, co mają testować.

    Testy SAMEGO wyłącznika (`test_wylacznik_sterowania.py`) ustawiają opcję
    jawnie w `entry.options`, więc ta podmianka ich nie dotyczy — opcja jawna
    ma pierwszeństwo nad defaultem.
    """
    from custom_components.volter import executor as _executor

    monkeypatch.setattr(_executor, "DEFAULT_CONTROL_ENABLED", True)
