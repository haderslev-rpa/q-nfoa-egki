"""Hent sagsoplysninger og bilag fra en konkret NFOA-sag."""

from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.async_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from q_nfoa_egki.configuration import PAGE_TIMEOUT_MS


logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# URL
# ------------------------------------------------------------

NFOA_CASE_URL = (
    "https://nfoa.egki.dk/CaseView.aspx"
)

CASE_VIEW_PATTERN = re.compile(
    r"/CaseView\.aspx(?:\?|$)",
    flags=re.IGNORECASE,
)


# ------------------------------------------------------------
# LOGIN
# ------------------------------------------------------------

LOGIN_EMAIL_INPUT_SELECTOR = (
    "#Main_nfx_login_Login1_ctl00_txtEmail"
)


# ------------------------------------------------------------
# SAGSSIDE
# ------------------------------------------------------------

TABS_SELECTOR = "#tabs"

IDENTIFIER_SELECTOR = "#Identifier"

CASE_SUMMARY_SELECTOR = (
    "#Main_Main_caseView_case_id"
)


# ------------------------------------------------------------
# BILAG
# ------------------------------------------------------------

BILAG_TAB_LINK_SELECTOR = (
    "a[href='#Main_Main_caseView_tab4']"
)

BILAG_TAB_FALLBACK_SELECTOR = (
    "#ui-id-4"
)

BILAG_PANEL_SELECTOR = (
    "#Main_Main_caseView_tab4"
)

ATTACHMENT_LINK_SELECTOR = (
    "a[id*='CaseAttachments_"
    "repAttachments_"
    "lbtnAttachmentName']"
)


# ------------------------------------------------------------
# MEMORYGRÆNSER
# ------------------------------------------------------------

MAX_ATTACHMENT_SIZE_BYTES = (
    50 * 1024 * 1024
)

MAX_CASE_ATTACHMENTS_SIZE_BYTES = (
    200 * 1024 * 1024
)


# ------------------------------------------------------------
# DATAMODELLER
# ------------------------------------------------------------

@dataclass(slots=True)
class NfoaAttachment:
    """
    Metadata og binært indhold for ét NFOA-bilag.

    content indeholder filen som bytes, når bilaget er
    downloadet.

    Hvis der kun er hentet metadata, er content b"".
    """

    name: str

    file_name: str

    attachment_type: str

    created_date: str

    created_by: str

    content_type: str = ""

    content: bytes = b""

    @property
    def size(self) -> int:
        """
        Returnerer filens størrelse i bytes.

        Returnerer:
            Antal bytes i content.
        """
        return len(self.content)

    @property
    def has_content(self) -> bool:
        """
        Fortæller, om bilaget er hentet til memory.

        Returnerer:
            True, hvis content indeholder data.
        """
        return bool(self.content)

    def clear_content(self) -> None:
        """
        Frigiver objektets reference til filindholdet.

        bytes er immutable. Python garanterer derfor ikke,
        at den tidligere hukommelse straks overskrives.

        Referencen fjernes, så Python kan genbruge memory.
        """
        self.content = b""

    def to_dict(
        self,
        *,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """
        Konverterer bilaget til en dictionary.

        Args:
            include_content:
                Hvis True medtages content som bytes.
                Bytes kan ikke gemmes direkte som JSON.

        Returnerer:
            Dictionary med bilagsmetadata.
        """
        result: dict[str, Any] = {
            "name": self.name,
            "file_name": self.file_name,
            "attachment_type": self.attachment_type,
            "created_date": self.created_date,
            "created_by": self.created_by,
            "content_type": self.content_type,
            "size": self.size,
            "has_content": self.has_content,
        }

        if include_content:
            result["content"] = self.content

        return result


@dataclass(slots=True)
class NfoaCaseData:
    """
    Samlet resultat for én NFOA-sag.

    attachments kan indeholde bilag som bytes.
    """

    cid: str

    url: str

    identifier: str

    case_summary: str

    tabs: dict[str, Any]

    attachments: list[NfoaAttachment] = field(
        default_factory=list
    )

    @property
    def total_attachment_size(self) -> int:
        """
        Returnerer bilagenes samlede størrelse.

        Returnerer:
            Samlet antal bytes i memory.
        """
        return sum(
            attachment.size
            for attachment in self.attachments
        )

    def clear_attachment_content(self) -> None:
        """
        Frigiver alle bilagsreferencer for sagen.

        Funktionen bør kaldes i processens finally-blok
        efter behandlingen af hvert work item.
        """
        for attachment in self.attachments:
            attachment.clear_content()

        self.attachments.clear()

    def to_dict(
        self,
        *,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """
        Konverterer sagsdata til en dictionary.

        Args:
            include_content:
                Hvis True medtages bilagenes bytes.

                Dette bør normalt være False, da bytes ikke
                kan gemmes direkte som JSON eller i
                item.data["box"].

        Returnerer:
            Dictionary med sagsdata og bilagsmetadata.
        """
        return {
            "cid": self.cid,
            "url": self.url,
            "identifier": self.identifier,
            "case_summary": self.case_summary,
            "tabs": self.tabs,
            "total_attachment_size": (
                self.total_attachment_size
            ),
            "attachments": [
                attachment.to_dict(
                    include_content=include_content
                )
                for attachment in self.attachments
            ],
        }


# ------------------------------------------------------------
# HJÆLPEFUNKTIONER
# ------------------------------------------------------------

def _normalize_text(
    value: object,
) -> str:
    """
    Fjerner linjeskift, NBSP og overflødige mellemrum.

    Returnerer:
        Normaliseret tekst.
    """
    return " ".join(
        str(value or "")
        .replace("\u00a0", " ")
        .split()
    ).strip()


def _validate_cid(
    cid: str | int,
) -> str:
    """
    Kontrollerer, at cid kun indeholder cifre.

    Returnerer:
        cid som tekst.
    """
    value = str(cid).strip()

    if not value:
        raise ValueError(
            "cid må ikke være tom."
        )

    if not value.isdigit():
        raise ValueError(
            "cid må kun indeholde cifre."
        )

    return value


def _extract_cid(
    case: str | int,
) -> str:
    """
    Udleder cid fra et tal eller en komplet NFOA-URL.

    Eksempler:
        524493

        https://nfoa.egki.dk/CaseView.aspx?cid=524493

    Returnerer:
        cid som tekst.
    """
    value = str(case).strip()

    if value.isdigit():
        return _validate_cid(value)

    parsed_url = urlparse(value)

    cid_values = parse_qs(
        parsed_url.query
    ).get("cid")

    if not cid_values:
        raise ValueError(
            "Sagsreferencen indeholder ikke cid. "
            f"Værdi: {value}"
        )

    return _validate_cid(
        cid_values[0]
    )


def _build_case_url(
    cid: str,
) -> str:
    """
    Bygger NFOA-URL'en til en konkret sag.

    Returnerer:
        Komplet CaseView-URL.
    """
    query_string = urlencode(
        {
            "cid": cid,
        }
    )

    return (
        f"{NFOA_CASE_URL}"
        f"?{query_string}"
    )


def _safe_file_name(
    value: str,
    fallback: str,
) -> str:
    """
    Laver et sikkert filnavn.

    Filnavnet bruges kun som metadata. Funktionen skriver
    ikke filen til disk.

    Returnerer:
        Filnavn uden mapper og ugyldige tegn.
    """
    file_name = (
        Path(value).name.strip()
        or fallback
    )

    file_name = re.sub(
        r'[\\/:*?"<>|\x00-\x1f]',
        "_",
        file_name,
    )

    return (
        file_name[:240]
        or fallback
    )


def _guess_content_type(
    file_name: str,
) -> str:
    """
    Finder MIME-type ud fra filnavnet.

    Eksempler:
        .pdf  -> application/pdf
        .jpg  -> image/jpeg
        .xml  -> application/xml

    Returnerer:
        MIME-type eller application/octet-stream.
    """
    content_type, _ = mimetypes.guess_type(
        file_name
    )

    return (
        content_type
        or "application/octet-stream"
    )


# ------------------------------------------------------------
# SIDENAVIGATION
# ------------------------------------------------------------

async def _ensure_logged_in(
    page: Page,
) -> None:
    """
    Kontrollerer, at NFOA-sessionen stadig er logget ind.

    Raises:
        RuntimeError, hvis loginformularen er synlig.
    """
    login_input = page.locator(
        LOGIN_EMAIL_INPUT_SELECTOR
    )

    if await login_input.count() == 0:
        return

    if await login_input.first.is_visible():
        raise RuntimeError(
            "NFOA-sessionen er udløbet eller "
            "ikke logget ind."
        )


async def _open_case(
    page: Page,
    cid: str,
) -> str:
    """
    Åbner sagen i den eksisterende Playwright-session.

    Returnerer:
        Den endelige URL til sagen.
    """
    case_url = _build_case_url(
        cid
    )

    logger.info(
        "Åbner NFOA-sag cid=%s.",
        cid,
    )

    try:
        response = await page.goto(
            case_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            f"NFOA-sagen cid={cid} blev ikke "
            "indlæst inden for tidsgrænsen."
        ) from error

    if (
        response is not None
        and not response.ok
    ):
        raise RuntimeError(
            "NFOA returnerede en HTTP-fejl. "
            f"Status: {response.status}. "
            f"cid: {cid}."
        )

    await _ensure_logged_in(
        page
    )

    if not CASE_VIEW_PATTERN.search(
        page.url
    ):
        raise RuntimeError(
            "NFOA åbnede ikke CaseView.aspx. "
            f"Aktuel URL: {page.url}"
        )

    await page.locator(
        TABS_SELECTOR
    ).wait_for(
        state="attached",
        timeout=PAGE_TIMEOUT_MS,
    )

    return page.url


# ------------------------------------------------------------
# SAGSOPLYSNINGER
# ------------------------------------------------------------

async def _read_case_tabs(
    page: Page,
) -> dict[str, Any]:
    """
    Læser data fra alle fanepaneler på sagssiden.

    Fanerne findes i DOM'en, selv om kun én fane er synlig
    ad gangen.

    Returnerer:
        Dictionary opdelt efter fanenavn.
    """
    result = await page.evaluate(
        """
        () => {
            const clean = value => (
                value ?? ""
            )
                .toString()
                .replace(/\\u00a0/g, " ")
                .replace(/\\s+/g, " ")
                .trim();

            const controlValue = control => {
                if (!control) {
                    return "";
                }

                if (control.tagName === "SELECT") {
                    return clean(
                        control.selectedOptions?.[0]
                            ?.textContent
                        || control.value
                    );
                }

                if (
                    control.type === "checkbox"
                    || control.type === "radio"
                ) {
                    if (!control.checked) {
                        return "";
                    }

                    return clean(
                        control.value || "Ja"
                    );
                }

                return clean(control.value);
            };

            const directText = element => {
                if (!element) {
                    return "";
                }

                const clone = element.cloneNode(true);

                clone.querySelectorAll(
                    "input[type='submit'], "
                    + "input[type='button'], "
                    + "button, script, style"
                ).forEach(
                    item => item.remove()
 )
                );

                return clean(
                    clone.innerText
                    || clone.textContent
                );
            };

            const readTable = table => {
                const heading = clean(
                    table.querySelector(
                        ".headline                   )?.innerText
                    || table.querySelector(
                        ".headline"
                    )?.textContent
                    || table.querySelector(
                        "caption"
                    )?.innerText
                    || ""
                );

                const fields = [];
                const rows = [];

                const tableRows = (
                    table.querySelectorAll(
                        ":scope > tbody > tr, "
                        + ":scope > tr"
                    )
                );

                for (const row of tableRows) {
                    const cells = [
                        ...row.querySelectorAll(
                            ":scope > th, "
                            + ":scope > td"
                        )
                    ];

                    if (!cells.length) {
                        continue;
                    }

                    const values = cells.map(
                        cell => {
                            const controls = [
                                ...cell.querySelectorAll(
                                    "input, "
                                    + "textarea, "
                                    + "select"
                                )
                            ].filter(
                                control => ![
                                    "hidden",
                                    "submit",
                                    "button",
                                    "file"
                                ].includes(
                                    control.type
                                )
                            );

                            const controlValues = (
                                controls
                                    .map(controlValue)
                                    .filter(Boolean)
                            );

                            if (
                                controlValues.length
                            ) {
                                return (
                                    controlValues.join(
                                        " | "
                                    )
                                );
                            }

                            return directText(cell);
                        }
                    );

                    if (values.some(Boolean)) {
                        rows.push(values);
                    }

                    if (values.length >= 2) {
                        const label = (
                            values[0]
                                .replace(/:$/, "")
                                .trim()
                        );

                        const value = (
                            values
                                .slice(1)
                                .filter(Boolean)
                                .join(" | ")
                        );

                        if (
                            label
                            && value
                            && label !== value
                            && !label.includes(
                                "Rediger"
                            )
                        ) {
                            fields.push(
                                {
                                    label,
                                    value
                                }
                            );
                        }
                    }
                }

                return {
                    heading,
                    fields,
                    rows
                };
            };

            const tabs = {};

            const tabItems = (
                document.querySelectorAll(
                    "#tabs > ul > li"
                )
            );

            for (const tabItem of tabItems) {
                const link = (
                    tabItem.querySelector(
                        "a[href^='#']"
                    )
                );

                if (!link) {
                    continue;
                }

                const tabName = clean(
                    link.innerText
                    || link.textContent
                );

                const panelSelector = (
                    link.getAttribute("href")
                );

                if (!panelSelector) {
                    continue;
                }

                const panel = (
                    document.querySelector(
                        panelSelector
                    )
                );

                if (!tabName || !panel) {
                    continue;
                }

                const tables = [
                    ...panel.querySelectorAll(
                        "table"
                    )
                ]
                    .map(readTable)
                    .filter(
                        table => (
                            table.heading
                            || table.fields.length
                            || table.rows.length
                        )
                    );

                const questions = [
                    ...panel.querySelectorAll(
                        ".optionQuestionTD"
                    )
                ]
                    .map(
                        container => ({
                            question: clean(
                                container
                                    .querySelector(
                                        ".optionQuestionText"
                                    )
                                    ?.innerText
                            ),
                            answer: clean(
                                container
                                    .querySelector(
                                        ".optionAnswer"
                                    )
                                    ?.innerText
                            )
                        })
                    )
                    .filter(
                        item => (
                            item.question
                            || item.answer
                        )
                    );

                const groups = [
                    ...panel.querySelectorAll(
                        ".optionGroupheadline"
                    )
                ]
                    .map(
                        group => {
                            const content = (
                                group.nextElementSibling
                            );

                            const groupQuestions = (
                                content
                                ? [
                                    ...content
                                        .querySelectorAll(
                                            ".optionQuestionTD"
                                        )
                                ]
                                    .map(
                                        container => ({
                                            question:
                                                clean(
                                                    container
                                                        .querySelector(
                                                            ".optionQuestionText"
                                                        )
                                                        ?.innerText
                                                ),
                                            answer:
                                                clean(
                                                    container
                                                        .querySelector(
                                                            ".optionAnswer"
                                                        )
                                                        ?.innerText
                                                )
                                        })
                                    )
                                    .filter(
                                        item => (
                                            item.question
                                            || item.answer
                                        )
                                    )
                                : []
                            );

                            return {
                                name: clean(
                                    group.innerText
                                    || group.textContent
                                ),
                                questions:
                                    groupQuestions
                            };
                        }
                    )
                    .filter(
                        group => (
                            group.name
                            || group.questions.length
                        )
                    );

                const textareas = [
                    ...panel.querySelectorAll(
                        "textarea"
                    )
                ]
                    .map(
                        control => ({
                            id: control.id || "",
                            name:
                                control.name || "",
                            value:
                                controlValue(control)
                        })
                    )
                    .filter(
                        item => item.value
                    );

                tabs[tabName] = {
                    tables,
                    groups,
                    questions,
                    textareas,
                    visible_text:
                        directText(panel)
                };
            }

            return tabs;
        }
        """
    )

    if not isinstance(result, dict):
        return {}

    return result


# ------------------------------------------------------------
# BILAGSFANE
# ------------------------------------------------------------

async def _open_attachments_tab(
    page: Page,
) -> None:
    """
    Klikker på fanen Bilag.

    NFOA initialiserer eller viser bilagsindholdet, når fanen
    aktiveres. Derfor udføres et rigtigt Playwright-klik.

    Returnerer:
        Ingen værdi. Ved succes er bilagspanelet synligt.
    """
    logger.info(
        "Åbner fanen Bilag."
    )

    attachment_tab = page.locator(
        BILAG_TAB_LINK_SELECTOR
    )

    if await attachment_tab.count() == 0:
        attachment_tab = page.locator(
            BILAG_TAB_FALLBACK_SELECTOR
        )

    if await attachment_tab.count() == 0:
        raise RuntimeError(
            "Fanen Bilag blev ikke fundet "
            "på sagssiden."
        )

    attachment_tab = (
        attachment_tab.first
    )

    await attachment_tab.wait_for(
        state="visible",
        timeout=PAGE_TIMEOUT_MS,
    )

    await attachment_tab.scroll_into_view_if_needed()

    try:
        await attachment_tab.click(
            timeout=PAGE_TIMEOUT_MS,
        )

    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Der kunne ikke klikkes på "
            "fanen Bilag."
        ) from error

    attachment_panel = page.locator(
        BILAG_PANEL_SELECTOR
    )

    try:
        await attachment_panel.wait_for(
            state="visible",
            timeout=PAGE_TIMEOUT_MS,
        )

    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Bilag-fanen blev klikket, men "
            "bilagspanelet blev ikke synligt."
        ) from error

    await page.wait_for_function(
        """
        selector => {
            const panel = (
                document.querySelector(selector)
            );

            if (!panel) {
                return false;
            }

            const style = (
                window.getComputedStyle(panel)
            );

            return (
                style.display !== "none"
                && style.visibility !== "hidden"
            );
        }
        """,
        arg=BILAG_PANEL_SELECTOR,
        timeout=PAGE_TIMEOUT_MS,
    )

    logger.info(
        "Fanen Bilag er aktiv."
    )


async def _read_attachment_metadata(
    page: Page,
) -> list[NfoaAttachment]:
    """
    Læser bilagsmetadata fra den aktive Bilag-fane.

    Returnerer:
        Liste med bilagsmetadata. content er endnu b"".
    """
    attachment_panel = page.locator(
        BILAG_PANEL_SELECTOR
    )

    await attachment_panel.wait_for(
        state="visible",
        timeout=PAGE_TIMEOUT_MS,
    )

    attachment_links = (
        attachment_panel.locator(
            ATTACHMENT_LINK_SELECTOR
        )
    )

    link_count = (
        await attachment_links.count()
    )

    if link_count == 0:
        logger.info(
            "Sagen indeholder ingen bilag."
        )
        return []

    rows = await page.evaluate(
        """
        ({ panelSelector, linkSelector }) => {
            const clean = value => (
                value ?? ""
            )
                .toString()
                .replace(/\\u00a0/g, " ")
                .replace(/\\s+/g, " ")
                .trim();

            const panel = (
                document.querySelector(
                    panelSelector
                )
            );

            if (!panel) {
                return [];
            }

            return [
                ...panel.querySelectorAll(
                    linkSelector
                )
            ].map(
                link => {
                    const row = (
                        link.closest("tr")
                    );

                    const cells = (
                        row
                        ? [
                            ...row.querySelectorAll(
                                ":scope > td"
                            )
                        ]
                        : []
                    );

                    const displayName = clean(
                        link.innerText
                        || link.textContent
                    );

                    const fileNameMatch = (
                        displayName.match(
                            /\\(([^()]*)\\)\\s*$/
                        )
                    );

                    return {
                        name: displayName,
                        file_name: clean(
                            fileNameMatch?.[1]
                            || displayName
                        ),
                        attachment_type: clean(
                            cells[1]?.innerText
                        ),
                        created_date: clean(
                            cells[2]?.innerText
                        ),
                        created_by: clean(
                            cells[3]?.innerText
                        )
                    };
                }
            );
        }
        """,
        {
            "panelSelector":
                BILAG_PANEL_SELECTOR,
            "linkSelector":
                ATTACHMENT_LINK_SELECTOR,
        },
    )

    attachments: list[
        NfoaAttachment
    ] = []

    for item in rows:
        if not isinstance(item, dict):
            continue

        file_name = _normalize_text(
            item.get("file_name")
        )

        attachments.append(
            NfoaAttachment(
                name=_normalize_text(
                    item.get("name")
                ),
                file_name=file_name,
                attachment_type=(
                    _normalize_text(
                        item.get(
                            "attachment_type"
                        )
                    )
                ),
                created_date=(
                    _normalize_text(
                        item.get(
                            "created_date"
                        )
                    )
                ),
                created_by=(
                    _normalize_text(
                        item.get(
                            "created_by"
                        )
                    )
                ),
                content_type=(
                    _guess_content_type(
                        file_name
                    )
                ),
                content=b"",
            )
        )

    logger.info(
        "Fandt %s bilag på sagen.",
        len(attachments),
    )

    return attachments


# ------------------------------------------------------------
# DOWNLOAD TIL MEMORY
# ------------------------------------------------------------

async def _download_attachments_to_memory(
    page: Page,
    attachments: list[NfoaAttachment],
) -> list[NfoaAttachment]:
    """
    Downloader alle bilag og læser dem ind som bytes.

    Playwright gemmer først downloadfilen i browserens
    midlertidige downloadområde. Filen læses derefter som
    bytes.

    Der oprettes ikke permanente outputfiler.

    Returnerer:
        Nye NfoaAttachment-objekter med udfyldt content.
    """
    downloaded_attachments: list[
        NfoaAttachment
    ] = []

    total_size = 0

    try:
        for index, attachment in enumerate(
            attachments,
            start=1,
        ):
            # NFOA kræver, at Bilag-fanen er aktiveret.
            await _open_attachments_tab(
                page
            )

            attachment_panel = page.locator(
                BILAG_PANEL_SELECTOR
            )

            attachment_links = (
                attachment_panel.locator(
                    ATTACHMENT_LINK_SELECTOR
                )
            )

            link_count = (
                await attachment_links.count()
            )

            link_index = index - 1

            if link_index >= link_count:
                raise RuntimeError(
                    "Bilagslinket forsvandt fra "
                    "siden under download. "
                    f"Bilag: {attachment.name!r}."
                )

            attachment_link = (
                attachment_links.nth(
                    link_index
                )
            )

            await attachment_link.scroll_into_view_if_needed()

            logger.info(
                "Henter bilag %s/%s til memory: %s",
                index,
                len(attachments),
                attachment.file_name,
            )

            try:
                async with page.expect_download(
                    timeout=PAGE_TIMEOUT_MS,
                ) as download_info:
                    await attachment_link.click(
                        timeout=PAGE_TIMEOUT_MS,
                    )

                download = (
                    await download_info.value
                )

            except PlaywrightTimeoutError as error:
                raise RuntimeError(
                    "NFOA startede ikke download "
                    "af bilaget "
                    f"{attachment.name!r}."
                ) from error

            failure = await download.failure()

            if failure:
                raise RuntimeError(
                    "Download af bilaget "
                    f"{attachment.name!r} "
                    f"fejlede: {failure}"
                )

            temporary_path = (
                await download.path()
            )

            if temporary_path is None:
                raise RuntimeError(
                    "Playwright returnerede ingen "
                    "midlertidig filsti for bilaget "
                    f"{attachment.name!r}."
                )

            temporary_file_path = Path(
                temporary_path
            )

            try:
                file_size = (
                    temporary_file_path.stat().st_size
                )

            except OSError as error:
                raise RuntimeError(
                    "Filstørrelsen kunne ikke læses "
                    "for bilaget "
                    f"{attachment.name!r}."
                ) from error

            if file_size <= 0:
                raise RuntimeError(
                    "Det downloadede bilag var tomt: "
                    f"{attachment.name!r}."
                )

            if (
                file_size
                > MAX_ATTACHMENT_SIZE_BYTES
            ):
                raise RuntimeError(
                    "Bilaget er større end den "
                    "tilladte memorygrænse. "
                    f"Fil: {attachment.file_name}. "
                    f"Størrelse: {file_size} bytes. "
                    f"Grænse: "
                    f"{MAX_ATTACHMENT_SIZE_BYTES} bytes."
                )

            projected_total = (
                total_size + file_size
            )

            if (
                projected_total
                > MAX_CASE_ATTACHMENTS_SIZE_BYTES
            ):
                raise RuntimeError(
                    "Bilagene på sagen overskrider "
                    "den samlede memorygrænse. "
                    f"Forventet størrelse: "
                    f"{projected_total} bytes. "
                    f"Grænse: "
                    f"{MAX_CASE_ATTACHMENTS_SIZE_BYTES} "
                    "bytes."
                )

            try:
                file_content = (
                    temporary_file_path.read_bytes()
                )

            except OSError as error:
                raise RuntimeError(
                    "Det downloadede bilag kunne "
                    "ikke læses ind i memory: "
                    f"{attachment.name!r}."
                ) from error

            if len(file_content) != file_size:
                raise RuntimeError(
                    "Det indlæste bilag har ikke "
                    "den forventede størrelse. "
                    f"Fil: {attachment.file_name}. "
                    f"Forventet: {file_size} bytes. "
                    f"Læst: {len(file_content)} bytes."
                )

            suggested_file_name = (
                _safe_file_name(
                    download.suggested_filename,
                    _safe_file_name(
                        attachment.file_name,
                        f"bilag_{index}",
                    ),
                )
            )

            content_type = (
                _guess_content_type(
                    suggested_file_name
                )
            )

            downloaded_attachment = (
                NfoaAttachment(
                    name=attachment.name,
                    file_name=(
                        suggested_file_name
                    ),
                    attachment_type=(
                        attachment.attachment_type
                    ),
                    created_date=(
                        attachment.created_date
                    ),
                    created_by=(
                        attachment.created_by
                    ),
                    content_type=content_type,
                    content=file_content,
                )
            )

            downloaded_attachments.append(
                downloaded_attachment
            )

            total_size = projected_total

            logger.info(
                "Bilag hentet til memory: "
                "%s, %s bytes.",
                suggested_file_name,
                file_size,
            )

        return downloaded_attachments

    except Exception:
        # Frigiv allerede hentede bilag, hvis et senere
        # download fejler.
        for downloaded_attachment in (
            downloaded_attachments
        ):
            downloaded_attachment.clear_content()

        downloaded_attachments.clear()

        raise


# ------------------------------------------------------------
# OFFENTLIG FUNKTION
# ------------------------------------------------------------

async def get_case_data(
    page: Page,
    case: str | int,
    *,
    download_attachments: bool = True,
) -> NfoaCaseData:
    """
    Henter sagsoplysninger og bilag fra én NFOA-sag.

    Args:
        page:
            En Playwright-side, som allerede er logget ind
            via login_nfoa().

        case:
            Et numerisk cid eller en komplet URL i formatet
            CaseView.aspx?cid=...

        download_attachments:
            Hvis True hentes bilagene som bytes.

            Hvis False hentes kun bilagsmetadata, og
            attachment.content forbliver b"".

    Returnerer:
        NfoaCaseData med oplysninger fra alle faner og
        bilag som bytes, når download_attachments er True.

    Oprydning:
        Kald case_data.clear_attachment_content() i en
        finally-blok efter hvert work item.
    """
    if page.is_closed():
        raise RuntimeError(
            "Playwright-siden er lukket."
        )

    cid = _extract_cid(
        case
    )

    case_url = await _open_case(
        page=page,
        cid=cid,
    )

    identifier_locator = page.locator(
        IDENTIFIER_SELECTOR
    )

    identifier = ""

    if await identifier_locator.count() > 0:
        identifier = _normalize_text(
            await identifier_locator.first.get_attribute(
                "value"
            )
        )

    summary_locator = page.locator(
        CASE_SUMMARY_SELECTOR
    )

    case_summary = ""

    if await summary_locator.count() > 0:
        case_summary = _normalize_text(
            await summary_locator.first.inner_text()
        )

    logger.info(
        "Læser oplysninger fra alle sagsfaner."
    )

    tabs = await _read_case_tabs(
        page
    )

    # Bilag-fanen skal aktiveres med et rigtigt klik.
    await _open_attachments_tab(
        page
    )

    attachments = (
        await _read_attachment_metadata(
            page
        )
    )

    if (
        download_attachments
        and attachments
    ):
        attachments = (
            await _download_attachments_to_memory(
                page=page,
                attachments=attachments,
            )
        )

    case_data = NfoaCaseData(
        cid=cid,
        url=case_url,
        identifier=identifier,
        case_summary=case_summary,
        tabs=tabs,
        attachments=attachments,
    )

    logger.info(
        "Hentede sag cid=%s med %s faner, "
        "%s bilag og %s bytes i memory.",
        cid,
        len(tabs),
        len(attachments),
        case_data.total_attachment_size,
    )

    return case_data