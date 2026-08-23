"""Integracja Volter Energy — telemetria i sterowanie falownikiem via Cloud."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration

from .command_handler import VolterCommandHandler
from .const import CONF_API_KEY, CONF_DEVICE_ID, CONF_SUPABASE_ANON_KEY, CONF_SUPABASE_URL, DOMAIN
from .coordinator import VolterTelemetryCoordinator
from .device_token import DeviceTokenProvider, make_fetch
from .executor import VolterExecutor
from .fetcher import ScheduleFetcher
from .runtime import VolterRuntime

_LOGGER = logging.getLogger(__name__)

type VolterConfigEntry = ConfigEntry

SERVICE_DIAGNOSE = "diagnose"

#: Encje Voltera w HA. Bez nich integracja nie pokazywała NICZEGO — jedynym
#: wglądem był log, a jedynym hamulcem usunięcie mapowania encji.
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]

#: Karta Lovelace dostarczana razem z integracją. Rejestrujemy ją sami, żeby
#: użytkownik nie musiał nic wgrywać ręcznie ani dodawać zasobu w Lovelace.
CARD_URL = "/volter_static/volter-plan-card.js"


def url_karty(wersja: str | None) -> str:
    """Adres karty dla frontendu — z wersja integracji jako parametrem.

    Frontend HA cache'uje moduly takze wtedy, gdy serwer odpowiada `no-cache`.
    Po podmianie pliku pod tym samym adresem przegladarka potrafi trzymac STARY
    modul, a to jest blad niewidoczny z zadnej strony serwera: plik na dysku nowy,
    HTTP 200, testy zielone, a uzytkownik ma kod sprzed dwoch wydan. Wersja
    w adresie rozstrzyga to deterministycznie — nowe wydanie to nowy adres.

    Wersje podaje loader HA (`async_get_integration`), a nie odczyt `manifest.json`
    z dysku: ten drugi jest blokujacym wejsciem-wyjsciem w petli zdarzen.
    Brak wersji nie jest powodem do niewystawienia karty.
    """
    return f"{CARD_URL}?v={wersja}" if wersja else CARD_URL


def decyzja_o_zasobie(
    istniejace: list[dict], adres: str
) -> tuple[str, str | None]:
    """Co zrobic z wpisem karty w zasobach Lovelace: utworz / zaktualizuj / nic.

    Wydzielone jako czysta funkcja, bo cala reszta to I/O po kolekcji HA, ktorej
    w testach nie ma — a decyzja jest tym, co moze byc zle.

    Dopasowanie po SCIEZCE, nie po pelnym adresie: adres niesie wersje, wiec po
    kazdym wydaniu pelne porownanie nie znalazloby wpisu i dokladalo kolejny.
    """
    for wpis in istniejace:
        url = str(wpis.get("url") or "")
        if url.split("?", 1)[0] != CARD_URL:
            continue
        return ("nic" if url == adres else "zaktualizuj"), wpis.get("id")
    return "utworz", None


async def _async_zarejestruj_zasob(hass: HomeAssistant, adres: str) -> None:
    """Dopisz karte do zasobow Lovelace.

    `add_extra_js_url` wstrzykuje modul do powloki HTML i to jest droga zalecana,
    ale okazala sie niewystarczajaca: na instalacji, na ktorej to debugowalismy,
    osiem dzialajacych kart wlasnych (apexcharts, mushroom, power-flow-card-plus...)
    bylo zarejestrowanych jako ZASOBY, a nasza — jedyna szyta przez `extra_module_url`
    — pokazywala "configuration error". Zasob jest ladowany przez frontend razem
    z konfiguracja dashboardu, a nie z powloki, ktora potrafi przyjechac z cache'a.

    Robimy OBIE rzeczy. Zaden blad tutaj nie moze przewrocic integracji: karta jest
    dodatkiem do sterowania, a nie warunkiem jego dzialania.
    """
    try:
        lovelace = hass.data.get("lovelace")
        zasoby = getattr(lovelace, "resources", None)
        if zasoby is None:
            _LOGGER.debug("Lovelace w trybie YAML albo bez kolekcji zasobow — pomijam")
            return
        # Kolekcja jest leniwa; bez tego `async_items` zwroci pusta liste i przy
        # kazdym starcie dokladalibysmy duplikat.
        if hasattr(zasoby, "async_get_info"):
            await zasoby.async_get_info()

        akcja, ident = decyzja_o_zasobie(list(zasoby.async_items()), adres)
        if akcja == "utworz":
            await zasoby.async_create_item({"res_type": "module", "url": adres})
            _LOGGER.info("Karta Voltera dodana do zasobow Lovelace: %s", adres)
        elif akcja == "zaktualizuj" and ident:
            await zasoby.async_update_item(ident, {"res_type": "module", "url": adres})
            _LOGGER.info("Zasob karty Voltera zaktualizowany na %s", adres)
    except Exception:  # noqa: BLE001 — patrz docstring: karta nie moze wywrocic setupu
        _LOGGER.warning(
            "Nie udalo sie zarejestrowac karty w zasobach Lovelace. Karta moze wymagac "
            "recznego dodania (Ustawienia > Dashboardy > Zasoby): %s", adres, exc_info=True
        )


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Wystaw kartę Lovelace i dopisz ją do zasobów frontendu (raz na HA)."""
    if hass.data.get(f"{DOMAIN}_frontend"):
        return
    hass.data[f"{DOMAIN}_frontend"] = True
    katalog = str(Path(__file__).parent / "www")
    # Sciezka statyczna zostaje GOLA — parametr zapytania jest dla przegladarki,
    # nie dla routera; zarejestrowany razem z `?v=...` nie trafilby w handler.
    await hass.http.async_register_static_paths([
        StaticPathConfig(CARD_URL, f"{katalog}/volter-plan-card.js", cache_headers=False)
    ])
    integracja = await async_get_integration(hass, DOMAIN)
    adres = url_karty(str(integracja.version) if integracja.version else None)
    add_extra_js_url(hass, adres)
    await _async_zarejestruj_zasob(hass, adres)


async def _async_register_services(hass: HomeAssistant) -> None:
    """Zarejestruj serwisy domeny (raz na instancję HA, nie na config entry)."""
    if hass.services.has_service(DOMAIN, SERVICE_DIAGNOSE):
        return

    async def _handle_diagnose(_call: ServiceCall) -> dict:
        entries = hass.data.get(DOMAIN, {})
        return {
            entry_id: await bundle["executor"].async_diagnose()
            for entry_id, bundle in entries.items()
        }

    hass.services.async_register(
        DOMAIN, SERVICE_DIAGNOSE, _handle_diagnose,
        supports_response=SupportsResponse.ONLY,
    )


async def async_setup_entry(hass: HomeAssistant, entry: VolterConfigEntry) -> bool:
    """Konfiguracja integracji Volter z config entry."""
    api_key = entry.data[CONF_API_KEY]
    device_id = entry.data[CONF_DEVICE_ID]
    supabase_url = entry.data[CONF_SUPABASE_URL]
    anon_key = entry.data[CONF_SUPABASE_ANON_KEY]

    # N-6: prywatne kanały Realtime (JWT z device-token dla telemetrii i komend).
    # DOMYŚLNIE WYŁĄCZONE — bez tokenu HA nadaje/dołącza publicznie, tak jak przed N-6,
    # więc build aplikacji sprzed prywatnego kanału dalej widzi telemetrię. Włączenie:
    # ustaw `VOLTER_PRIVATE_REALTIME=1` w środowisku HA I zbuduj aplikację z
    # `private: true` (hooks/useHaTelemetry.ts) + zastosuj migrację 060. Kolejność:
    # nagłówek supabase/migrations/060_realtime_private_channels.sql (repo Apka1).
    token_provider = None
    if os.getenv("VOLTER_PRIVATE_REALTIME") == "1":
        token_provider = DeviceTokenProvider(
            make_fetch(lambda: async_get_clientsession(hass), supabase_url, api_key)
        )
        _LOGGER.info("N-6: prywatne kanały Realtime WŁĄCZONE (VOLTER_PRIVATE_REALTIME=1)")

    # Telemetry coordinator — zbiera stany encji i wysyła batche co 60s
    coordinator = VolterTelemetryCoordinator(
        hass=hass,
        entry=entry,
        api_key=api_key,
        device_id=device_id,
        supabase_url=supabase_url,
        token_provider=token_provider,
    )

    # Wyłącznik sterowania trzymany poza opcjami, żeby przełączenie encją nie
    # wymuszało `async_reload` (czyli resetu ochrony NVM — ustalenie S-7).
    runtime = VolterRuntime(hass, entry.entry_id, dict(entry.options))
    await runtime.async_load()

    # Executor — pętla wykonawcza harmonogramu + jedyna brama zapisu do falownika
    executor = VolterExecutor(hass=hass, entry=entry, runtime=runtime)

    # Task 16: pobieranie planu z chmury (get-schedule, pull co 5 min) —
    # brakujące ogniwo między planerem a executorem. Musi powstać PO executorze,
    # bo przekazuje mu pobrany plan (`executor.async_set_schedule`).
    fetcher = ScheduleFetcher(
        hass=hass,
        entry=entry,
        supabase_url=supabase_url,
        api_key=api_key,
        executor=executor,
    )

    # Command handler — subskrybuje kanał Realtime i deleguje do executora
    command_handler = VolterCommandHandler(
        hass=hass,
        entry=entry,
        device_id=device_id,
        supabase_url=supabase_url,
        anon_key=anon_key,
        api_key=api_key,
        executor=executor,
        token_provider=token_provider,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "command_handler": command_handler,
        "executor": executor,
        "fetcher": fetcher,
        "runtime": runtime,
    }

    await _async_register_services(hass)

    # Uruchom coordinator, executor, fetcher (po executorze — patrz wyżej) i command handler
    await coordinator.async_start()
    await executor.async_start()
    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await fetcher.async_start()
    await command_handler.async_start()

    # Reaguj na zmiany w Options Flow (przeładuj encje)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info(
        "Volter Energy integration started — device_id=%s, telemetry=60s, commands=realtime",
        device_id,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VolterConfigEntry) -> bool:
    """Wyładuj integrację Volter."""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data[DOMAIN].pop(entry.entry_id, {})

    coordinator: VolterTelemetryCoordinator | None = data.get("coordinator")
    command_handler: VolterCommandHandler | None = data.get("command_handler")
    executor: VolterExecutor | None = data.get("executor")
    fetcher: ScheduleFetcher | None = data.get("fetcher")

    if coordinator:
        await coordinator.async_stop()

    if command_handler:
        await command_handler.async_stop()

    # Task 16 / S-5: odsubskrybuj TIMER pobierania PRZED zatrzymaniem executora —
    # inaczej tick fetchera mógłby jeszcze odpalić się w oknie między odsubskrybowaniem
    # a `executor.async_stop()` i trafić na executor, który już odmawia zapisu (S-5b).
    if fetcher:
        await fetcher.async_stop()

    if executor:
        await executor.async_stop()

    _LOGGER.info("Volter Energy integration unloaded")
    return True


async def _async_update_listener(hass: HomeAssistant, entry: VolterConfigEntry) -> None:
    """Przeładuj integrację po zmianie opcji (entity mapping)."""
    _LOGGER.info("Options changed, reloading Volter integration")
    await hass.config_entries.async_reload(entry.entry_id)
