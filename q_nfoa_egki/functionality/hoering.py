"""Konfiguration og igangsættelse af høring i NFOA."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from playwright.async_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from q_nfoa_egki.configuration import PAGE_TIMEOUT_MS


logger = logging.getLogger(__name__)


HOERING_TAB_LINK_SELECTOR = "a[href='#Main_Main_caseView_tab6']"
HOERING_TAB_FALLBACK_SELECTOR = "#tabs a.ui-tabs-anchor"
HOERING_PANEL_SELECTOR = "#Main_Main_caseView_tab6"

HOERING_STATUS_SELECTOR = (
    "#Main_Main_caseView_CaseHearing_lblStatus"
)
HOERING_CREATED_DATE_SELECTOR = (
    "#Main_Main_caseView_CaseHearing_lblCreatedDate"
)

AVAILABLE_PARTIES_SELECTOR = (
    "#Main_Main_caseView_CaseHearing_listboxActorsAvailable"
)
SELECTED_PARTIES_SELECTOR = (
    "#Main_Main_caseView_CaseHearing_listboxActorsSelected"
)
ADD_PARTY_BUTTON_SELECTOR = (
    "#Main_Main_caseView_CaseHearing_btnTemplateAdd"
)
REMOVE_PARTY_BUTTON_SELECTOR = (
    "#Main_Main_caseView_CaseHearing_btnTemplateRemove"
)

DEADLINE_INPUT_SELECTOR = (
    "#Main_Main_caseView_CaseHearing_PartResponseAnswer_txtDate"
)
REMINDER_YES_SELECTOR = "#CheckboxYes"
REMINDER_NO_SELECTOR = "#CheckboxNo"
START_HEARING_BUTTON_SELECTOR = "#btnCreateHearing"


@dataclass(frozen=True, slots=True)
class Hoeringspart:
    """En høringspart fra NFOA."""

    value: str
    name: str

    def to_dict(self) -> dict[str, str]:
        """Konverterer høringsparten til en dictionary."""
        return {
            "value": self.value,
            "name": self.name,
        }


@dataclass(frozen=True, slots=True)
class Hoeringsresultat:
    """Resultat af konfigurering eller start af en høring."""

    started: bool
    status: str
    created_date: str
    deadline: str
    reminder_enabled: bool
    selected_parties: list[Hoeringspart]

    def to_dict(self) -> dict[str, Any]:
        """Konverterer resultatet til en JSON-klar dictionary."""
        return {
            "started": self.started,
            "status": self.status,
            "created_date": self.created_date,
            "deadline": self.deadline,
            "reminder_enabled": self.reminder_enabled,
            "selected_parties": [
                party.to_dict()
                for party in self.selected_parties
            ],
        }


def _normalize_text(value: object) -> str:
    """Normaliserer tekst."""
    return " ".join(
        str(value or "")
        .replace("\u00a0", " ")
        .split()
    ).strip()


def _normalize_party_name(value: object) -> str:
    """Normaliserer et partnavn til sammenligning."""
    return _normalize_text(value).casefold()


def _format_deadline(value: date | datetime | str) -> str:
    """Konverterer svarfristen til DD-MM-YYYY."""
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y")

    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")

    text = _normalize_text(value)

    if not text:
        raise ValueError("Svarfristen må ikke være tom.")

    for date_format in (
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ):
        try:
            parsed_date = datetime.strptime(
                text,
                date_format,
            )
            return parsed_date.strftime("%d-%m-%Y")
        except ValueError:
            continue

    raise ValueError(
        "Svarfristen skal være en gyldig dato. "
        "Brug eksempelvis 30-11-2026."
    )


async def _read_text(locator: Locator) -> str:
    """Læser normaliseret tekst fra et element."""
    if await locator.count() == 0:
        return ""

    return _normalize_text(
        await locator.first.inner_text()
    )


async def _open_hoering_tab(page: Page) -> None:
    """Klikker på fanen Høring og venter på panelet."""
    logger.info("Åbner fanen Høring.")

    hoering_tab = page.locator(
        HOERING_TAB_LINK_SELECTOR
    )

    if await hoering_tab.count() == 0:
        hoering_tab = page.locator(
            HOERING_TAB_FALLBACK_SELECTOR
        ).filter(has_text="Høring")

    if await hoering_tab.count() == 0:
        raise RuntimeError(
            "Fanen Høring blev ikke fundet."
        )

    hoering_tab = hoering_tab.first

    await hoering_tab.wait_for(
        state="visible",
        timeout=PAGE_TIMEOUT_MS,
    )
    await hoering_tab.scroll_into_view_if_needed()

    try:
        await hoering_tab.click(
            timeout=PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Der kunne ikke klikkes på fanen Høring."
        ) from error

    hoering_panel = page.locator(
        HOERING_PANEL_SELECTOR
    )

    try:
        await hoering_panel.wait_for(
            state="visible",
            timeout=PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Høringsfanen blev klikket, men "
            "høringspanelet blev ikke synligt."
        ) from error

    logger.info("Fanen Høring er aktiv.")


async def _read_parties(
    page: Page,
    select_selector: str,
) -> list[Hoeringspart]:
    """Læser alle options fra en høringsliste."""
    select = page.locator(select_selector)

    if await select.count() == 0:
        raise RuntimeError(
            "Listen med høringsparter blev ikke fundet. "
            f"Selector: {select_selector}"
        )

    options = select.locator("option")
    parties: list[Hoeringspart] = []

    for index in range(await options.count()):
        option = options.nth(index)
        value = _normalize_text(
            await option.get_attribute("value")
        )
        name = _normalize_text(
            await option.inner_text()
        )

        if value and name:
            parties.append(
                Hoeringspart(
                    value=value,
                    name=name,
                )
            )

    return parties


async def _find_party(
    page: Page,
    select_selector: str,
    requested_party: str,
) -> Hoeringspart | None:
    """Finder en part ud fra det fulde viste navn."""
    requested_name = _normalize_party_name(
        requested_party
    )

    parties = await _read_parties(
        page=page,
        select_selector=select_selector,
    )

    for party in parties:
        if _normalize_party_name(party.name) == requested_name:
            return party

    return None


async def _wait_for_party_in_list(
    page: Page,
    select_selector: str,
    party_value: str,
) -> None:
    """Venter på, at parten findes i den angivne liste."""
    await page.wait_for_function(
        """
        ({ selector, partyValue }) => {
            const select = document.querySelector(selector);
            if (!select) return false;
            return Array.from(select.options).some(
                option => option.value === partyValue
            );
        }
        """,
        arg={
            "selector": select_selector,
            "partyValue": party_value,
        },
        timeout=PAGE_TIMEOUT_MS,
    )


async def _wait_for_party_not_in_list(
    page: Page,
    select_selector: str,
    party_value: str,
) -> None:
    """Venter på, at parten er fjernet fra listen."""
    await page.wait_for_function(
        """
        ({ selector, partyValue }) => {
            const select = document.querySelector(selector);
            if (!select) return false;
            return !Array.from(select.options).some(
                option => option.value === partyValue
            );
        }
        """,
        arg={
            "selector": select_selector,
            "partyValue": party_value,
        },
        timeout=PAGE_TIMEOUT_MS,
    )


async def _get_selected_values(
    select: Locator,
) -> list[str]:
    """Returnerer value for alle markerede options."""
    values = await select.locator(
        "option:checked"
    ).evaluate_all(
        """
        options => options.map(
            option => option.value
        )
        """
    )

    return [str(value) for value in values]


async def _move_party(
    page: Page,
    *,
    party: Hoeringspart,
    source_selector: str,
    destination_selector: str,
    button_selector: str,
) -> None:
    """Markérer en part og flytter parten med pileknappen."""
    source_list = page.locator(source_selector)

    await source_list.wait_for(
        state="visible",
        timeout=PAGE_TIMEOUT_MS,
    )

    source_option = source_list.locator(
        f"option[value='{party.value}']"
    )

    if await source_option.count() == 0:
        raise RuntimeError(
            "Høringsparten findes ikke i den forventede "
            "kildeliste. "
            f"Part: {party.name!r}. "
            f"Value: {party.value!r}."
        )

    logger.info(
        "Markerer høringsparten %s med value=%s.",
        party.name,
        party.value,
    )

    await source_list.select_option(
        value=party.value,
    )

    selected_values = await _get_selected_values(
        source_list
    )

    if party.value not in selected_values:
        raise RuntimeError(
            "Høringsparten blev ikke markeret. "
            f"Part: {party.name!r}. "
            f"Value: {party.value!r}. "
            f"Markerede værdier: {selected_values!r}."
        )

    logger.info(
        "Høringsparten er markeret. "
        "Trykker på pileknappen."
    )

    move_button = page.locator(button_selector)

    await move_button.wait_for(
        state="visible",
        timeout=PAGE_TIMEOUT_MS,
    )

    if not await move_button.is_enabled():
        raise RuntimeError(
            "Pileknappen er deaktiveret. "
            f"Selector: {button_selector}"
        )

    try:
        await move_button.click(
            timeout=PAGE_TIMEOUT_MS,
        )

        await _wait_for_party_in_list(
            page=page,
            select_selector=destination_selector,
            party_value=party.value,
        )
        await _wait_for_party_not_in_list(
            page=page,
            select_selector=source_selector,
            party_value=party.value,
        )
    except PlaywrightTimeoutError as error:
        available_parties = await _read_parties(
            page,
            AVAILABLE_PARTIES_SELECTOR,
        )
        selected_parties = await _read_parties(
            page,
            SELECTED_PARTIES_SELECTOR,
        )

        raise RuntimeError(
            "NFOA flyttede ikke høringsparten efter "
            "klik på pileknappen. "
            f"Part: {party.name!r}. "
            f"Value: {party.value!r}. "
            "Tilgængelige: "
            f"{[item.name for item in available_parties]!r}. "
            "Valgte: "
            f"{[item.name for item in selected_parties]!r}."
        ) from error

    logger.info(
        "Høringsparten blev flyttet: %s.",
        party.name,
    )


async def _add_party(
    page: Page,
    requested_party: str,
) -> None:
    """Flytter en part fra Tilgængelige til Valgte parter."""
    selected_party = await _find_party(
        page=page,
        select_selector=SELECTED_PARTIES_SELECTOR,
        requested_party=requested_party,
    )

    if selected_party is not None:
        logger.info(
            "Høringsparten er allerede valgt: %s",
            selected_party.name,
        )
        return

    available_party = await _find_party(
        page=page,
        select_selector=AVAILABLE_PARTIES_SELECTOR,
        requested_party=requested_party,
    )

    if available_party is None:
        available = await _read_parties(
            page,
            AVAILABLE_PARTIES_SELECTOR,
        )
        selected = await _read_parties(
            page,
            SELECTED_PARTIES_SELECTOR,
        )
        known_names = [
            party.name
            for party in available + selected
        ]

        raise ValueError(
            "Høringsparten blev ikke fundet: "
            f"{requested_party!r}. "
            "Mulige parter: "
            + ", ".join(known_names)
        )

    await _move_party(
        page=page,
        party=available_party,
        source_selector=AVAILABLE_PARTIES_SELECTOR,
        destination_selector=SELECTED_PARTIES_SELECTOR,
        button_selector=ADD_PARTY_BUTTON_SELECTOR,
    )


async def _remove_party(
    page: Page,
    requested_party: str,
) -> None:
    """Flytter en part fra Valgte til Tilgængelige parter."""
    selected_party = await _find_party(
        page=page,
        select_selector=SELECTED_PARTIES_SELECTOR,
        requested_party=requested_party,
    )

    if selected_party is None:
        logger.info(
            "Høringsparten er ikke valgt og skal "
            "derfor ikke fjernes: %s",
            requested_party,
        )
        return

    await _move_party(
        page=page,
        party=selected_party,
        source_selector=SELECTED_PARTIES_SELECTOR,
        destination_selector=AVAILABLE_PARTIES_SELECTOR,
        button_selector=REMOVE_PARTY_BUTTON_SELECTOR,
    )


async def _set_exact_parties(
    page: Page,
    requested_parties: list[str],
) -> None:
    """Sikrer, at højre liste indeholder præcis de ønskede parter."""
    requested_names = {
        _normalize_party_name(party)
        for party in requested_parties
        if _normalize_text(party)
    }

    if not requested_names:
        raise ValueError(
            "Der skal angives mindst én høringspart."
        )

    selected_parties = await _read_parties(
        page,
        SELECTED_PARTIES_SELECTOR,
    )

    for selected_party in selected_parties:
        if (
            _normalize_party_name(selected_party.name)
            not in requested_names
        ):
            await _remove_party(
                page,
                selected_party.name,
            )

    for requested_party in requested_parties:
        cleaned_name = _normalize_text(
            requested_party
        )

        if cleaned_name:
            await _add_party(
                page,
                cleaned_name,
            )


async def _set_deadline(
    page: Page,
    deadline: str,
) -> None:
    """Udfylder svarfristen."""
    deadline_input = page.locator(
        DEADLINE_INPUT_SELECTOR
    )

    await deadline_input.wait_for(
        state="visible",
        timeout=PAGE_TIMEOUT_MS,
    )
    await deadline_input.fill(deadline)
    await deadline_input.press("Tab")

    actual_value = _normalize_text(
        await deadline_input.input_value()
    )

    if actual_value != deadline:
        raise RuntimeError(
            "NFOA beholdt ikke svarfristen. "
            f"Forventet: {deadline}. "
            f"Faktisk: {actual_value}."
        )

    logger.info("Svarfrist angivet: %s", deadline)


async def _set_reminder(
    page: Page,
    enabled: bool,
) -> None:
    """Vælger Ja eller Nej til mailpåmindelse."""
    selector = (
        REMINDER_YES_SELECTOR
        if enabled
        else REMINDER_NO_SELECTOR
    )
    radio_button = page.locator(selector)

    await radio_button.wait_for(
        state="visible",
        timeout=PAGE_TIMEOUT_MS,
    )
    await radio_button.check(
        timeout=PAGE_TIMEOUT_MS,
    )

    if not await radio_button.is_checked():
        raise RuntimeError(
            "Indstillingen for påmindelse "
            "kunne ikke vælges."
        )

    logger.info(
        "Påmindelse via mail: %s.",
        "Ja" if enabled else "Nej",
    )


async def _start_hoering(
    page: Page,
    previous_status: str,
) -> None:
    """Trykker på Igangsæt høring og venter på statusændring."""
    start_button = page.locator(
        START_HEARING_BUTTON_SELECTOR
    )

    if await start_button.count() == 0:
        raise RuntimeError(
            "Knappen Igangsæt høring blev ikke fundet."
        )

    start_button = start_button.first
    disabled_attribute = await start_button.get_attribute(
        "disabled"
    )
    css_classes = (
        await start_button.get_attribute("class")
        or ""
    )

    if (
        disabled_attribute is not None
        or "aspNetDisabled" in css_classes
        or not await start_button.is_enabled()
    ):
        title = _normalize_text(
            await start_button.get_attribute("title")
        )
        reason = (
            title
            or (
                "Knappen er deaktiveret. "
                "Kontrollér, at sagen er visiteret "
                "til en sagsbehandler."
            )
        )
        raise RuntimeError(
            "Høringen kan ikke igangsættes: "
            f"{reason}"
        )

    logger.info("Igangsætter høringen.")

    await start_button.click(
        timeout=PAGE_TIMEOUT_MS,
    )

    try:
        await page.wait_for_function(
            """
            ({ selector, previousStatus }) => {
                const element = document.querySelector(selector);
                if (!element) return false;

                const currentStatus = (
                    element.innerText
                    || element.textContent
                    || ""
                ).replace(/\s+/g, " ").trim();

                return (
                    currentStatus
                    && currentStatus !== previousStatus
                    && !currentStatus
                        .toLowerCase()
                        .includes("afventer igangsættelse")
                );
            }
            """,
            arg={
                "selector": HOERING_STATUS_SELECTOR,
                "previousStatus": previous_status,
            },
            timeout=PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        current_status = await _read_text(
            page.locator(HOERING_STATUS_SELECTOR)
        )
        raise RuntimeError(
            "Der blev klikket på Igangsæt høring, "
            "men status blev ikke ændret. "
            f"Aktuel status: {current_status!r}."
        ) from error

    logger.info("Høringen blev igangsat.")


async def konfigurer_hoering(
    page: Page,
    *,
    parties: list[str],
    deadline: date | datetime | str,
    reminder_enabled: bool = False,
    exact_parties: bool = True,
    start_hearing: bool = False,
) -> Hoeringsresultat:
    """Konfigurerer og kan igangsætte en NFOA-høring."""
    if page.is_closed():
        raise RuntimeError(
            "Playwright-siden er lukket."
        )

    if "CaseView.aspx" not in page.url:
        raise RuntimeError(
            "Playwright-siden står ikke på en NFOA-sag. "
            f"Aktuel URL: {page.url}"
        )

    cleaned_parties = [
        _normalize_text(party)
        for party in parties
        if _normalize_text(party)
    ]

    if not cleaned_parties:
        raise ValueError(
            "Der skal angives mindst én høringspart."
        )

    formatted_deadline = _format_deadline(deadline)

    await _open_hoering_tab(page)

    initial_status = await _read_text(
        page.locator(HOERING_STATUS_SELECTOR)
    )

    if (
        initial_status
        and "afventer igangsættelse"
        not in initial_status.casefold()
    ):
        raise RuntimeError(
            "Høringen kan ikke konfigureres, fordi "
            "status ikke er 'Afventer igangsættelse'. "
            f"Aktuel status: {initial_status!r}."
        )

    if exact_parties:
        await _set_exact_parties(
            page=page,
            requested_parties=cleaned_parties,
        )
    else:
        for party in cleaned_parties:
            await _add_party(page, party)

    selected_parties = await _read_parties(
        page,
        SELECTED_PARTIES_SELECTOR,
    )
    selected_names = {
        _normalize_party_name(party.name)
        for party in selected_parties
    }
    missing_parties = [
        party
        for party in cleaned_parties
        if _normalize_party_name(party)
        not in selected_names
    ]

    if missing_parties:
        raise RuntimeError(
            "Følgende høringsparter blev ikke valgt: "
            + ", ".join(missing_parties)
        )

    await _set_deadline(page, formatted_deadline)
    await _set_reminder(page, reminder_enabled)

    started = False

    if start_hearing:
        await _start_hoering(
            page=page,
            previous_status=initial_status,
        )
        started = True

    final_status = await _read_text(
        page.locator(HOERING_STATUS_SELECTOR)
    )
    created_date = await _read_text(
        page.locator(HOERING_CREATED_DATE_SELECTOR)
    )

    return Hoeringsresultat(
        started=started,
        status=final_status,
        created_date=created_date,
        deadline=formatted_deadline,
        reminder_enabled=reminder_enabled,
        selected_parties=selected_parties,
    )


configure_hearing = konfigurer_hoering
