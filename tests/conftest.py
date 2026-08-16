"""Ładuje czyste moduły (`guards`, `schedule`) bez importowania Home Assistanta.

`custom_components/volter/__init__.py` importuje HA, więc zwykłe
`from custom_components.volter.guards import ...` wymagałoby zainstalowanego HA.
A `guards.py` i `schedule.py` są świadomie wolne od zależności od HA — chcemy je
testować zwykłym `pytest`, tak jak później firmware będzie testowane na hoście.

Dlatego rejestrujemy je pod sztucznym pakietem `volter_pure`, pomijając `__init__.py`
prawdziwego komponentu. Kolejność ładowania ma znaczenie: `schedule` robi
`from .guards import Action`.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

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
