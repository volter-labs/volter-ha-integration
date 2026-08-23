"""Adres karty musi się zmieniać razem z jej wersją.

Karta jest wstrzykiwana przez `add_extra_js_url`, a frontend HA cache'uje moduły
także wtedy, gdy serwer odpowiada `no-cache` — między innymi przez service workera.
Po podmianie pliku pod tym samym adresem przeglądarka potrafi trzymać STARY moduł,
a wtedy dashboard pokazuje błąd, którego nie widać po żadnej stronie serwera:
plik na dysku jest nowy, HTTP zwraca 200, testy przechodzą, a użytkownik ma
w karcie kod sprzed dwóch wydań.

Wersja w adresie rozstrzyga to deterministycznie — nowe wydanie to nowy adres.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.volter import CARD_URL, url_karty


def _wersja() -> str:
    manifest = Path(__file__).parents[1] / "custom_components" / "volter" / "manifest.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


def test_adres_karty_niesie_wersje_integracji():
    assert url_karty() == f"{CARD_URL}?v={_wersja()}"


def test_sciezka_statyczna_zostaje_bez_parametru():
    """Parametr zapytania jest dla przeglądarki, nie dla routera — ścieżka
    rejestrowana w `async_register_static_paths` musi zostać goła, inaczej
    żądanie o `?v=...` nie trafi w żaden handler."""
    assert "?" not in CARD_URL


def test_brak_manifestu_nie_wysadza_rejestracji(monkeypatch):
    """Karta bez wersji jest gorsza niż karta z wersją, ale wciąż lepsza niż
    integracja, która nie wstaje."""
    import custom_components.volter as modul

    monkeypatch.setattr(modul, "_wersja_integracji", lambda: None)

    assert url_karty() == CARD_URL


def test_wersja_w_karcie_nie_rozjezdza_sie_z_manifestem():
    """Karta pokazuje swoją wersję w stopce — po to, żeby dało się jednym
    spojrzeniem odróżnić „kod jest zły" od „przeglądarka trzyma stary plik".
    Znacznik, który może się rozjechać z manifestem, kłamałby w dokładnie tym
    momencie, w którym się na nim polega."""
    karta = (Path(__file__).parents[1] / "custom_components" / "volter" / "www"
             / "volter-plan-card.js").read_text(encoding="utf-8")

    assert f"const WERSJA = '{_wersja()}';" in karta
