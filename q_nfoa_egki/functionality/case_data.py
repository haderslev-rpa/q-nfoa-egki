"""Udlæs feltdata fra en konkret NFOA-sag."""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page

from q_nfoa_egki.configuration import PAGE_TIMEOUT_MS
from q_nfoa_egki.models import NfoaArrangementCase, NfoaCaseLink

logger = logging.getLogger(__name__)

FIELD_ALIASES = {
    "kle": ("KLE", "KLE-nr", "KLE nr"),
    "virksomheds_foreningsnavn": (
        "Virksomheds/foreningsnavn",
        "Virksomhedsnavn",
        "Foreningsnavn",
    ),
    "cvr_nr": ("CVR-nr", "CVR nr", "CVR"),
    "cpr_nr": ("CPR-nr", "CPR nr", "CPR"),
    "p_nr": ("P-nr", "P nr"),
    "adresse": ("Adresse", "Ansøgers adresse"),
    "arrangement_adresse": (
        "Arrangement Adresse",
        "Arrangement adresse",
        "Placering",
    ),
    "longitude": ("Longitude", "Længdegrad"),
    "latitude": ("Latitude", "Breddegrad"),
    "fornavn": ("Fornavn",),
    "mellemnavn": ("Mellemnavn",),
    "efternavn": ("Efternavn",),
    "telefonnummer": ("Telefonnummer", "Telefon", "Mobil"),
    "email": ("E-mail", "Email", "E-mailadresse"),
    "arrangement_navn": (
        "Arrangement navn",
        "Arrangementets navn",
        "Navn på arrangement",
    ),
    "kontaktperson_under_arrangement": (
        "Kontaktperson - Under Arrangement",
        "Kontaktperson under arrangement",
    ),
    "kontaktperson_efter_arrangement": (
        "Kontaktperson - Efter Arrangement",
        "Kontaktperson efter arrangement",
    ),
    "afsluttet": ("Afsluttet",),
    "start_tidspunkt": ("Start tidspunkt", "Starttidspunkt"),
    "slut_tidspunkt": ("Slut tidspunkt", "Sluttidspunkt"),
    "start_dato": ("Start dato", "Startdato"),
    "slut_dato": ("Slut dato", "Slutdato"),
    "ansoegningsdato": ("Ansøgningsdato", "Ansøgt dato"),
}

BLANKET_TYPE_ALIASES = (
    "Blanket",
    "Blankettype",
    "Blanket type",
    "Ansøgningstype",
    "Ansøgningstype/blanket",
)


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").replace(" ", " ").split()).strip()


def _normalize_label(value: object) -> str:
    text = _normalize_text(value).rstrip(":")
    return text.casefold()


def _extract_cid(case_url: str) -> str:
    values = parse_qs(urlparse(case_url).query).get("cid")
    if not values or not str(values[0]).strip():
        raise RuntimeError(f"NFOA-sags-URL mangler cid: {case_url}")
    return str(values[0]).strip()


async def _ensure_logged_in(page: Page) -> None:
    login_input = page.locator("input[type='email'], input[name*='mail' i]")
    if await login_input.count() and await login_input.first.is_visible():
        raise RuntimeError("NFOA-sessionen er udløbet eller ikke logget ind.")


async def _read_labelled_controls(page: Page) -> dict[str, str]:
    """Læs værdier fra label/input, label/select og label/textarea."""
    result = await page.evaluate(
        """
        () => {
            const clean = value => (value ?? '')
                .toString()
                .replace(/\u00a0/g, ' ')
                .replace(/\s+/g, ' ')
                .trim();
            const output = {};
            const controls = document.querySelectorAll('input, textarea, select');
            for (const control of controls) {
                if (control.type === 'hidden' || control.type === 'submit' || control.type === 'button') continue;
                let label = '';
                if (control.id) {
                    const explicit = document.querySelector(`label[for="${CSS.escape(control.id)}"]`);
                    if (explicit) label = clean(explicit.innerText || explicit.textContent);
                }
                if (!label) {
                    const wrapping = control.closest('label');
                    if (wrapping) label = clean(wrapping.innerText || wrapping.textContent);
                }
                if (!label) {
                    const container = control.closest('td, div, li, p');
                    const localLabel = container?.querySelector('label, .label, .field-label, dt, th');
                    if (localLabel) label = clean(localLabel.innerText || localLabel.textContent);
                }
                if (!label) continue;
                let value = '';
                if (control.tagName === 'SELECT') {
                    value = clean(control.selectedOptions?.[0]?.textContent || control.value);
                } else if (control.type === 'checkbox' || control.type === 'radio') {
                    if (!control.checked) continue;
                    value = clean(control.value || 'Ja');
                } else {
                    value = clean(control.value);
                }
                if (value) output[label] = value;
            }
            return output;
        }
        """
    )
    return {_normalize_text(k): _normalize_text(v) for k, v in result.items() if _normalize_text(k)}


async def _read_table_and_definition_rows(page: Page) -> dict[str, str]:
    """Læs visningsfelter fra tabelrækker og dt/dd-par."""
    result = await page.evaluate(
        """
        () => {
            const clean = value => (value ?? '')
                .toString()
                .replace(/\u00a0/g, ' ')
                .replace(/\s+/g, ' ')
                .trim();
            const output = {};
            for (const row of document.querySelectorAll('tr')) {
                const cells = [...row.querySelectorAll(':scope > th, :scope > td')];
                if (cells.length < 2) continue;
                const label = clean(cells[0].innerText || cells[0].textContent).replace(/:$/, '');
                const value = clean(cells.slice(1).map(cell => cell.innerText || cell.textContent).join(' '));
                if (label && value && label !== value) output[label] = value;
            }
            for (const term of document.querySelectorAll('dt')) {
                const description = term.nextElementSibling;
                if (!description || description.tagName !== 'DD') continue;
                const label = clean(term.innerText || term.textContent).replace(/:$/, '');
                const value = clean(description.innerText || description.textContent);
                if (label && value) output[label] = value;
            }
            return output;
        }
        """
    )
    return {_normalize_text(k): _normalize_text(v) for k, v in result.items() if _normalize_text(k)}


def _find_value(raw_fields: dict[str, str], aliases: tuple[str, ...]) -> str:
    normalized = {_normalize_label(k): _normalize_text(v) for k, v in raw_fields.items()}
    for alias in aliases:
        value = normalized.get(_normalize_label(alias))
        if value:
            return value
    for alias in aliases:
        wanted = _normalize_label(alias)
        for label, value in normalized.items():
            if wanted in label or label in wanted:
                if value:
                    return value
    return ""


async def get_case_data(
    page: Page,
    case: NfoaCaseLink | str,
) -> NfoaArrangementCase:
    """Åbn én kendt NFOA-URL og returnér normaliserede blanketdata."""
    case_url = case.url if isinstance(case, NfoaCaseLink) else str(case)
    cid = case.cid if isinstance(case, NfoaCaseLink) else _extract_cid(case_url)

    logger.info("Åbner NFOA-sag cid=%s for feltudlæsning.", cid)
    response = await page.goto(
        case_url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )
    if response is not None and not response.ok:
        raise RuntimeError(
            f"NFOA returnerede HTTP {response.status} for cid={cid}."
        )
    await _ensure_logged_in(page)
    await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)

    raw_fields = await _read_table_and_definition_rows(page)
    raw_fields.update(await _read_labelled_controls(page))

    blanket_type = _find_value(raw_fields, BLANKET_TYPE_ALIASES)
    if not blanket_type:
        body_text = _normalize_text(await page.locator("body").inner_text())
        expected = "Ansøgning om tilladelse til offentligt arrangement"
        if expected.casefold() in body_text.casefold():
            blanket_type = expected

    fields = {
        key: _find_value(raw_fields, aliases)
        for key, aliases in FIELD_ALIASES.items()
    }

    logger.info(
        "Udlæste NFOA-sag cid=%s med blanket-type %r.",
        cid,
        blanket_type,
    )
    return NfoaArrangementCase(
        cid=cid,
        url=case_url,
        blanket_type=blanket_type,
        fields=fields,
    )


async def get_cases_data(
    page: Page,
    cases: list[NfoaCaseLink],
) -> list[NfoaArrangementCase]:
    """Andet kald: gennemgå de allerede kendte URL'er og læs sagsdata."""
    return [await get_case_data(page, case) for case in cases]
