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
    assert url_karty(_wersja()) == f"{CARD_URL}?v={_wersja()}"


def test_sciezka_statyczna_zostaje_bez_parametru():
    """Parametr zapytania jest dla przeglądarki, nie dla routera — ścieżka
    rejestrowana w `async_register_static_paths` musi zostać goła, inaczej
    żądanie o `?v=...` nie trafi w żaden handler."""
    assert "?" not in CARD_URL


def test_brak_wersji_nie_wysadza_rejestracji():
    """Karta bez wersji jest gorsza niż karta z wersją, ale wciąż lepsza niż
    integracja, która nie wstaje. Wersję podaje loader HA — gdy jej nie poda,
    zostaje goły adres.

    Odczyt `manifest.json` wprost z dysku byłby blokującym wejściem-wyjściem
    w pętli zdarzeń; HA zgłasza to jako błąd integracji, i słusznie.
    """
    assert url_karty(None) == CARD_URL


def test_wersja_w_karcie_nie_rozjezdza_sie_z_manifestem():
    """Karta pokazuje swoją wersję w stopce — po to, żeby dało się jednym
    spojrzeniem odróżnić „kod jest zły" od „przeglądarka trzyma stary plik".
    Znacznik, który może się rozjechać z manifestem, kłamałby w dokładnie tym
    momencie, w którym się na nim polega."""
    karta = (Path(__file__).parents[1] / "custom_components" / "volter" / "www"
             / "volter-plan-card.js").read_text(encoding="utf-8")

    assert f"const WERSJA = '{_wersja()}';" in karta


# ── rejestracja jako zasób Lovelace ──────────────────────────────────────────


def test_decyzja_gdy_zasobu_jeszcze_nie_ma():
    """Karta musi trafić do zasobów Lovelace, a nie tylko do `extra_module_url`.

    Na żywej instalacji Michała osiem działających kart własnych (apexcharts,
    mushroom, power-flow-card-plus…) jest zarejestrowanych jako zasoby. Nasza była
    jedyną wstrzykiwaną przez `extra_module_url` — i jedyną, która nie działała.
    """
    from custom_components.volter import decyzja_o_zasobie

    assert decyzja_o_zasobie([], "/volter_static/volter-plan-card.js?v=2.6.1") == (
        "utworz", None)


def test_decyzja_gdy_zasob_jest_z_inna_wersja():
    from custom_components.volter import decyzja_o_zasobie

    istniejace = [{"id": "abc", "url": "/volter_static/volter-plan-card.js?v=2.5.0"}]

    assert decyzja_o_zasobie(istniejace, "/volter_static/volter-plan-card.js?v=2.6.1") == (
        "zaktualizuj", "abc")


def test_decyzja_gdy_zasob_jest_aktualny():
    """Zapis przy każdym starcie HA przepisywałby `.storage` bez powodu."""
    from custom_components.volter import decyzja_o_zasobie

    istniejace = [{"id": "abc", "url": "/volter_static/volter-plan-card.js?v=2.6.1"}]

    assert decyzja_o_zasobie(istniejace, "/volter_static/volter-plan-card.js?v=2.6.1") == (
        "nic", "abc")


def test_decyzja_ignoruje_cudze_zasoby():
    from custom_components.volter import decyzja_o_zasobie

    istniejace = [
        {"id": "x", "url": "/hacsfiles/apexcharts-card/apexcharts-card.js?hacstag=1"},
        {"id": "y", "url": "/hacsfiles/lovelace-mushroom/mushroom.js"},
    ]

    assert decyzja_o_zasobie(istniejace, "/volter_static/volter-plan-card.js?v=2.6.1") == (
        "utworz", None)
