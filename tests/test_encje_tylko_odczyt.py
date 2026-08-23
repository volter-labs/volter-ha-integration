"""Krok „Sterowanie" nie może przyjąć encji, która w danym układzie nic nie zrobi.

`number.goodwe_eco_mode_soc` i `number.goodwe_eco_mode_power` mają w integracji
mletenay `setter=None`, ale to NIE znaczy, że są martwe. `async_set_native_value`
mimo braku settera zapisuje stan encji, a `select.py` nasłuchuje tej zmiany
(`update_eco_mode_power` / `update_eco_mode_soc`) i przekłada ją na
`set_operation_mode(tryb, moc, soc)`.

Jest jednak warunek: listener działa TYLKO wtedy, gdy bieżąca opcja selecta trybu
pracy to `eco_charge` albo `eco_discharge`. To jest `select.goodwe_operation_mode`,
a nie `select.goodwe_ems_mode` — inna encja, inny zestaw trybów.

Stąd reguła: encje eco są prawidłowym adresatem, gdy tryb prowadzimy przez select
trybu pracy, i są martwym adresatem, gdy prowadzimy go przez select EMS. Wtedy
wskazanie `eco_mode_soc` jako dolnego progu SoC to ciche wyłączenie rezerwy
backupu (I-1): plan liczy rezerwę, guardy ją przepuszczają, komenda wychodzi,
a falownik nigdy jej nie zobaczy.
"""

from __future__ import annotations

import pytest

from custom_components.volter.const import (
    OPT_ENTITY_CHARGE_LIMIT,
    OPT_ENTITY_DISCHARGE_LIMIT,
    OPT_ENTITY_ECO_MODE_SOC,
    OPT_ENTITY_EMS_MODE,
    waliduj_encje_sterujace,
)


def _flow(fake_entry):
    from custom_components.volter.config_flow import VolterOptionsFlow

    flow = VolterOptionsFlow(fake_entry)
    flow.async_create_entry = lambda *, data: {"type": "create_entry", "data": data}
    # Harness stubuje moduly `homeassistant`, wiec metody bazowego FlowHandlera
    # trzeba podstawic — interesuje nas WYNIK kroku, nie renderowanie formularza.
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    # Krok „Sterowanie" konczy sie przejsciem do strategii, ktora renderuje wlasny
    # formularz przez selektory niedostepne w harnessie. Interesuje nas, CZY krok
    # przeszedl dalej — nie jak wyglada nastepny ekran.
    flow.async_step_strategy = lambda *a, **k: _dalej()
    return flow


async def _dalej():
    return {"type": "przeszlo_dalej"}


def test_walidator_wskazuje_pole_i_powod():
    bledy = waliduj_encje_sterujace({
        OPT_ENTITY_EMS_MODE: "select.goodwe_ems_mode",
        OPT_ENTITY_ECO_MODE_SOC: "number.goodwe_eco_mode_soc",
    })

    assert bledy == {OPT_ENTITY_ECO_MODE_SOC: "encja_tylko_do_odczytu"}


def test_encje_eco_sa_prawidlowe_przy_selekcie_trybu_pracy():
    """Regresja z v2.6.0: blokada byla bezwarunkowa i wycinala dzialajacy kanal.

    Przy `select.goodwe_operation_mode` zapis do `eco_mode_power`/`eco_mode_soc`
    DOCIERA do falownika — przez listener w `goodwe/select.py`, nie przez setter
    encji. Odrzucanie tego mapowania odbieralo uzytkownikowi sterowanie moca
    w trybach eco.
    """
    assert waliduj_encje_sterujace({
        OPT_ENTITY_EMS_MODE: "select.goodwe_operation_mode",
        OPT_ENTITY_ECO_MODE_SOC: "number.goodwe_eco_mode_soc",
    }) == {}


def test_walidator_przepuszcza_encje_zapisywalne():
    assert waliduj_encje_sterujace(
        {
            OPT_ENTITY_ECO_MODE_SOC: "number.goodwe_depth_of_discharge_on_grid",
            OPT_ENTITY_CHARGE_LIMIT: "number.goodwe_ems_power_limit",
            OPT_ENTITY_EMS_MODE: "select.goodwe_ems_mode",
        }
    ) == {}


def test_walidator_nie_myli_sie_o_podobna_nazwe():
    """Blokada idzie po CALEJ nazwie obiektu, nie po fragmencie — inaczej encja
    `number.goodwe_eco_mode_soc_target` z innej integracji zostalaby odrzucona
    bez powodu."""
    assert waliduj_encje_sterujace(
        {OPT_ENTITY_ECO_MODE_SOC: "number.inny_producent_eco_mode_soc_target"}
    ) == {}


@pytest.mark.asyncio
async def test_odrzuca_encje_eco_gdy_tryb_idzie_przez_select_ems(fake_entry):
    flow = _flow(fake_entry)

    wynik = await flow.async_step_control(
        {
            OPT_ENTITY_EMS_MODE: "select.goodwe_ems_mode",
            OPT_ENTITY_ECO_MODE_SOC: "number.goodwe_eco_mode_soc",
        }
    )

    assert wynik["type"] == "form", "formularz musi wrócić, a nie przejść dalej"
    assert wynik["errors"][OPT_ENTITY_ECO_MODE_SOC] == "encja_tylko_do_odczytu"


@pytest.mark.asyncio
async def test_odrzuca_eco_mode_power_jako_nastawe_mocy(fake_entry):
    """`eco_mode_power` ma `setter=None` z tego samego powodu — nastawa mocy
    idzie do `ems_power_limit`, nie tutaj."""
    flow = _flow(fake_entry)

    wynik = await flow.async_step_control(
        {
            OPT_ENTITY_EMS_MODE: "select.goodwe_ems_mode",
            OPT_ENTITY_CHARGE_LIMIT: "number.goodwe_eco_mode_power",
        }
    )

    assert wynik["errors"][OPT_ENTITY_CHARGE_LIMIT] == "encja_tylko_do_odczytu"


@pytest.mark.asyncio
async def test_encja_zapisywalna_przechodzi(fake_entry):
    flow = _flow(fake_entry)

    wynik = await flow.async_step_control(
        {
            OPT_ENTITY_ECO_MODE_SOC: "number.goodwe_depth_of_discharge_on_grid",
            OPT_ENTITY_CHARGE_LIMIT: "number.goodwe_ems_power_limit",
        }
    )

    assert wynik["type"] == "przeszlo_dalej"


@pytest.mark.asyncio
async def test_puste_pole_kasuje_wczesniejsze_mapowanie(fake_entry):
    """Odznaczenie encji w formularzu MUSI ją usunąć.

    Filtr `if v` przy zapisie sprawiał, że puste pole było ignorowane — raz
    wskazanej encji nie dało się już odpiąć inaczej niż przez ręczną edycję
    `.storage`. Przy encjach sterujących to nie jest kosmetyka: użytkownik,
    który chce odciąć jeden kanał zapisu, musi móc to zrobić z interfejsu.
    """
    flow = _flow(fake_entry)
    flow._options[OPT_ENTITY_DISCHARGE_LIMIT] = "number.stara_encja"

    await flow.async_step_control({OPT_ENTITY_DISCHARGE_LIMIT: ""})

    assert OPT_ENTITY_DISCHARGE_LIMIT not in flow._options


# ── tor zapisu ───────────────────────────────────────────────────────────────


def test_executor_traktuje_encje_tylko_do_odczytu_jak_niezmapowana():
    """Walidacja formularza działa dopiero przy NASTĘPNYM zapisie opcji.

    Instalacja, która ma już złe mapowanie w `.storage`, musi być widoczna od razu:
    `would_write` w `volter.diagnose` ma pokazywać to, co NAPRAWDĘ trafi na falownik.
    Encja z `setter=None` nie przyjmie nic, więc udawanie, że parametr ma adresata,
    kłamałoby w jedynym narzędziu, którym użytkownik to sprawdza przed włączeniem
    sterowania.
    """
    from custom_components.volter.executor import _mapped_entity

    ems = {OPT_ENTITY_EMS_MODE: "select.goodwe_ems_mode"}
    eco = {OPT_ENTITY_EMS_MODE: "select.goodwe_operation_mode"}

    assert _mapped_entity("eco_soc", ems | {OPT_ENTITY_ECO_MODE_SOC: "number.goodwe_eco_mode_soc"}) is False
    assert _mapped_entity("eco_soc", eco | {OPT_ENTITY_ECO_MODE_SOC: "number.goodwe_eco_mode_soc"}) is True
    assert _mapped_entity(
        "eco_soc", ems | {OPT_ENTITY_ECO_MODE_SOC: "number.goodwe_depth_of_discharge_on_grid"}
    ) is True
