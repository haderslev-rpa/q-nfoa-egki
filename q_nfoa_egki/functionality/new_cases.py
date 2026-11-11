"""Hent URL'er til nye NFOA-sager."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

from playwright.async_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from q_nfoa_egki.configuration import (
    NFOA_NEW_CASES_URL,
    PAGE_TIMEOUT_MS,
)
from q_nfoa_egki.models import NfoaCaseLink
from q_nfoa_egki.selectors import NfoaSelectors


logger = logging.getLogger(__name__)


CASE_URL_PATTERN = re.compile(
    r"/CaseView\.aspx\?[^#]*\bcid=\d+",
    flags=re.IGNORECASE,
)


def _normalize_text(
    value: str | None,
) -> str:
    """Fjerner linjeskift og overflødige mellemrum."""
    return " ".join(
        str(value or "").split()
    )


def _extract_cid(
    case_url: str,
) -> str:
    """
    Henter cid fra en NFOA-sags-URL.

    Eksempel:
        https://nfoa.egki.dk/CaseView.aspx?cid=111111

    Returnerer:
        cid som tekst.

    Raises:
        RuntimeError, hvis URL'en ikke indeholder cid.
    """
    parsed_url = urlparse(case_url)

    query_values = parse_qs(
        parsed_url.query
    )

    cid_values = query_values.get("cid")

    if not cid_values:
        raise RuntimeError(
            "NFOA-sags-URL'en indeholder ikke cid. "
            f"URL: {case_url}"
        )

    cid = str(cid_values[0]).strip()

    if not cid:
        raise RuntimeError(
            "NFOA-sags-URL'en indeholder en tom cid."
        )

    return cid


async def _ensure_logged_in(
    page: Page,
) -> None:
    """
    Kontrollerer, at siden ikke er sendt tilbage til login.

    Raises:
        RuntimeError, hvis loginfeltet er synligt.
    """
    email_input = page.locator(
        NfoaSelectors.EMAIL_INPUT
    )

    if await email_input.count() == 0:
        return

    if await email_input.first.is_visible():
        raise RuntimeError(
            "NFOA-sessionen er ikke logget ind, "
            "eller sessionen er udløbet."
        )


async def _open_new_cases_overview(
    page: Page,
) -> None:
    """
    Åbner oversigten over nye NFOA-sager.

    Funktionen bevarer den eksisterende browsersession.
    """
    response = await page.goto(
        NFOA_NEW_CASES_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    if response is not None and not response.ok:
        raise RuntimeError(
            "NFOA returnerede en HTTP-fejl ved åbning "
            "af oversigten over nye sager. "
            f"HTTP-status: {response.status}."
        )

    await _ensure_logged_in(page)

    await page.wait_for_load_state(
        "networkidle",
        timeout=PAGE_TIMEOUT_MS,
    )


async def _get_case_selectors(
    page: Page,
):
    """
    Finder de ASP.NET-elementer, der åbner en sag.

    Elementerne identificeres via btnSelectEventName,
    som indgår i __doPostBack-eventet.

    Returnerer:
        En Playwright Locator.
    """
    case_selectors = page.locator(
        NfoaSelectors.CASE_SELECT_LINKS
    )

    await case_selectors.first.wait_for(
        state="visible",
        timeout=PAGE_TIMEOUT_MS,
    )

    return case_selectors


async def _get_row_text(
    page: Page,
    row_index: int,
) -> str:
    """
    Henter teksten fra den tabelrække, som indeholder
    det aktuelle postback-element.

    Returnerer:
        Den normaliserede rækketekst.
    """
    case_selectors = await _get_case_selectors(
        page
    )

    current_element = case_selectors.nth(
        row_index
    )

    table_row = current_element.locator(
        "xpath=ancestor::tr[1]"
    )

    if await table_row.count() == 0:
        return _normalize_text(
            await current_element.inner_text()
        )

    return _normalize_text(
        await table_row.inner_text()
    )


async def _open_case_and_get_url(
    page: Page,
    row_index: int,
) -> str:
    """
    Klikker på en sagsrække og returnerer den endelige URL.

    Elementet findes igen umiddelbart før klik, fordi DOM'en
    bliver genindlæst efter hvert ASP.NET-postback.

    Returnerer:
        URL i formatet CaseView.aspx?cid=...
    """
    case_selectors = await _get_case_selectors(
        page
    )

    element_count = await case_selectors.count()

    if row_index >= element_count:
        raise RuntimeError(
            "Den ønskede sagsrække findes ikke længere. "
            f"Rækkeindeks: {row_index}. "
            f"Antal elementer: {element_count}."
        )

    current_element = case_selectors.nth(
        row_index
    )

    await current_element.scroll_into_view_if_needed()

    logger.info(
        "Åbner NFOA-sag fra række %s.",
        row_index + 1,
    )

    try:
        async with page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        ):
            await current_element.click(
                timeout=PAGE_TIMEOUT_MS,
            )

    except PlaywrightTimeoutError:
        # Nogle ASP.NET-sider ændrer URL lidt efter
        # domcontentloaded. Derfor kontrolleres URL'en,
        # før fejlen sendes videre.
        if not CASE_URL_PATTERN.search(page.url):
            raise RuntimeError(
                "Klik på sagsrækken førte ikke til "
                "CaseView.aspx. "
                f"Aktuel URL: {page.url}"
            )

    try:
        await page.wait_for_url(
            re.compile(
                r".*/CaseView\.aspx\?.*\bcid=\d+",
                flags=re.IGNORECASE,
            ),
            timeout=PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "NFOA åbnede ikke den forventede sags-URL. "
            f"Aktuel URL: {page.url}"
        ) from error

    return page.url


async def _return_to_case_overview(
    page: Page,
) -> None:
    """
    Går tilbage til oversigten over nye sager.

    Hvis browserens tilbage-navigation ikke rammer den
    forventede oversigt, åbnes overview-URL'en direkte.
    """
    try:
        await page.go_back(
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        logger.warning(
            "Browserens tilbage-navigation tog for lang tid. "
            "Oversigten åbnes direkte."
        )

    if (
        "CaseListOverview.aspx"
        not in page.url
    ):
        await _open_new_cases_overview(page)
    else:
        await page.wait_for_load_state(
            "networkidle",
            timeout=PAGE_TIMEOUT_MS,
        )

        await _ensure_logged_in(page)

    await _get_case_selectors(page)


async def get_new_case_urls(
    page: Page,
) -> list[NfoaCaseLink]:
    """
    Henter URL'er til alle nye NFOA-sager.

    Forudsætning:
        page skal allerede være logget ind via login_nfoa().

    Fremgangsmåde:
        1. Åbner listen over nye sager.
        2. Finder alle ASP.NET __doPostBack-elementer med
           btnSelectEventName.
        3. Klikker på hver række.
        4. Gemmer CaseView.aspx?cid=...
        5. Går tilbage til oversigten.
        6. Finder rækkeelementerne igen.

    Returnerer:
        Liste med NfoaCaseLink-objekter.
    """
    if page.is_closed():
        raise RuntimeError(
            "Playwright-siden er lukket."
        )

    logger.info(
        "Åbner NFOA-oversigten over nye sager."
    )

    try:
        await _open_new_cases_overview(page)

        case_selectors = (
            await _get_case_selectors(page)
        )

        number_of_cases = (
            await case_selectors.count()
        )

    except PlaywrightTimeoutError:
        logger.info(
            "Der blev ikke fundet nogen nye NFOA-sager."
        )
        return []

    logger.info(
        "Fandt %s nye sagsrækker.",
        number_of_cases,
    )

    collected_cases: list[NfoaCaseLink] = []
    collected_cids: set[str] = set()

    for row_index in range(number_of_cases):
        row_text = await _get_row_text(
            page=page,
            row_index=row_index,
        )

        case_url = await _open_case_and_get_url(
            page=page,
            row_index=row_index,
        )

        cid = _extract_cid(case_url)

        if cid not in collected_cids:
            collected_cases.append(
                NfoaCaseLink(
                    cid=cid,
                    url=case_url,
                    row_index=row_index,
                    row_text=row_text,
                )
            )

            collected_cids.add(cid)

            logger.info(
                "Fandt sag cid=%s fra række %s.",
                cid,
                row_index + 1,
            )
        else:
            logger.warning(
                "Sag cid=%s blev fundet mere end én gang.",
                cid,
            )

        if (
            row_index
            < number_of_cases - 1
        ):
            await _return_to_case_overview(
                page
            )

    logger.info(
        "Hentede %s unikke NFOA-sags-URL'er.",
        len(collected_cases),
    )

    return collected_cases