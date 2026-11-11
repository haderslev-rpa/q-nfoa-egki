"""Login til NetForvaltning Offentligt Arrangement."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from automation_server_client import (
    AutomationServer,
    Credential,
)
from playwright.async_api import Page
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from q_outlook_api.functionality.mail_api import (
    delete_mail,
    search_mails_filter,
)

from q_nfoa_egki.configuration import (
    NFOA_CREDENTIAL_NAME,
    NFOA_MAILBOX_FIELD,
    NFOA_PASSWORD_FIELD,
    NFOA_URL,
    PAGE_TIMEOUT_MS,
    TWO_FACTOR_MAIL_FOLDER,
    TWO_FACTOR_MAIL_LIMIT,
    TWO_FACTOR_MAIL_SENDER,
    TWO_FACTOR_MAIL_SUBJECT,
    TWO_FACTOR_MAIL_TIMEOUT_SECONDS,
    TWO_FACTOR_MAIL_TIME_TOLERANCE_SECONDS,
    TWO_FACTOR_POLL_INTERVAL_SECONDS,
)
from q_nfoa_egki.selectors import NfoaSelectors


logger = logging.getLogger(__name__)


TWO_FACTOR_CODE_PATTERN = re.compile(
    r"Anvend\s+engangskoden\s+(\d{6})\s+"
    r"for\s+at\s+bekræfte\s+din\s+identitet",
    flags=re.IGNORECASE,
)


def _get_login_information() -> tuple[str, str]:
    """
    Henter loginoplysninger fra credentialen DIRXHP.

    Credentialens Data (JSON) skal indeholde:

        {
            "NetForvaltningOA_pass": "...",
            "mailbox": "dirxhp@haderslev.dk"
        }

    mailbox bruges både som loginmail til NFOA og som
    Outlook-postkasse til to-faktormailen.

    Returnerer:
        Tuple med mailbox og adgangskode.
    """
    try:
        AutomationServer.from_environment()
    except Exception as error:
        raise RuntimeError(
            "Forbindelsen til Automation Server "
            "kunne ikke initialiseres."
        ) from error

    try:
        credential = Credential.get_credential(
            NFOA_CREDENTIAL_NAME
        )
    except Exception as error:
        raise RuntimeError(
            "Credentialen kunne ikke hentes fra "
            "Automation Server. "
            f"Credential-navn: {NFOA_CREDENTIAL_NAME}."
        ) from error

    credential_data: Any = credential.data

    if not isinstance(credential_data, dict):
        raise RuntimeError(
            "Data (JSON) på credentialen "
            f"{NFOA_CREDENTIAL_NAME!r} skal være "
            "et JSON-objekt."
        )

    mailbox = str(
        credential_data.get(NFOA_MAILBOX_FIELD)
        or ""
    ).strip()

    password_value = credential_data.get(
        NFOA_PASSWORD_FIELD
    )

    if not mailbox:
        raise RuntimeError(
            "Data (JSON) på credentialen "
            f"{NFOA_CREDENTIAL_NAME!r} mangler "
            f"feltet {NFOA_MAILBOX_FIELD!r}."
        )

    if not isinstance(password_value, str):
        raise RuntimeError(
            "Data (JSON) på credentialen "
            f"{NFOA_CREDENTIAL_NAME!r} mangler "
            f"feltet {NFOA_PASSWORD_FIELD!r}."
        )

    if not password_value:
        raise RuntimeError(
            f"Feltet {NFOA_PASSWORD_FIELD!r} på "
            f"credentialen {NFOA_CREDENTIAL_NAME!r} "
            "må ikke være tomt."
        )

    logger.info(
        "NFOA-loginoplysninger blev hentet fra "
        "credentialen %s.",
        NFOA_CREDENTIAL_NAME,
    )

    return mailbox, password_value


def _parse_received_datetime(
    value: Any,
) -> datetime | None:
    """
    Konverterer mailens modtagelsestidspunkt.

    Returnerer:
        UTC datetime eller None, hvis værdien ikke
        kan læses.
    """
    if not value:
        return None

    normalized_value = str(value).strip()

    if normalized_value.endswith(("Z", "z")):
        normalized_value = (
            normalized_value[:-1] + "+00:00"
        )

    try:
        received_at = datetime.fromisoformat(
            normalized_value
        )
    except ValueError:
        logger.warning(
            "Kunne ikke læse mailens tidspunkt: %s",
            value,
        )
        return None

    if received_at.tzinfo is None:
        received_at = received_at.replace(
            tzinfo=timezone.utc
        )

    return received_at.astimezone(timezone.utc)


def _extract_two_factor_code(
    mail_body: Any,
) -> str | None:
    """
    Finder den sekscifrede kode i NFOA-mailen.

    Returnerer:
        Den sekscifrede kode eller None.
    """
    body_text = str(mail_body or "")

    match = TWO_FACTOR_CODE_PATTERN.search(
        body_text
    )

    if match is None:
        return None

    return match.group(1)


def _find_two_factor_mail(
    mailbox: str,
    login_started_at: datetime,
) -> tuple[str, str] | None:
    """
    Søger efter den nyeste relevante NFOA-mail.

    Returnerer:
        Tuple med engangskode og message_id, hvis
        mailen findes. Ellers None.
    """
    date_from = (
        login_started_at - timedelta(minutes=1)
    ).strftime("%d-%m-%Y")

    mails = search_mails_filter(
        user_mail=mailbox,
        subject=TWO_FACTOR_MAIL_SUBJECT,
        from_email=TWO_FACTOR_MAIL_SENDER,
        date_from=date_from,
        folder=TWO_FACTOR_MAIL_FOLDER,
        limit=TWO_FACTOR_MAIL_LIMIT,
        include_attachments=False,
        prefer_plain_text=True,
    )

    if not isinstance(mails, list):
        raise RuntimeError(
            "Mailopslaget returnerede ikke en liste."
        )

    earliest_allowed = (
        login_started_at
        - timedelta(
            seconds=(
                TWO_FACTOR_MAIL_TIME_TOLERANCE_SECONDS
            )
        )
    )

    for mail in mails:
        if not isinstance(mail, dict):
            continue

        subject = str(
            mail.get("subject") or ""
        ).strip()

        sender_address = str(
            mail.get("sender_address") or ""
        ).strip()

        if (
            subject.casefold()
            != TWO_FACTOR_MAIL_SUBJECT.casefold()
        ):
            continue

        if (
            sender_address.casefold()
            != TWO_FACTOR_MAIL_SENDER.casefold()
        ):
            continue

        received_at = _parse_received_datetime(
            mail.get("received_datetime_utc")
        )

        if received_at is None:
            continue

        if received_at < earliest_allowed:
            continue

        two_factor_code = _extract_two_factor_code(
            mail.get("body")
        )

        if not two_factor_code:
            logger.warning(
                "NFOA-mailen blev fundet, men koden "
                "kunne ikke udlæses."
            )
            continue

        message_id = str(
            mail.get("message_id")
            or mail.get("id")
            or ""
        ).strip()

        if not message_id:
            logger.warning(
                "NFOA-mailen blev fundet, men mailen "
                "mangler message_id."
            )
            continue

        return two_factor_code, message_id

    return None


async def _wait_for_two_factor_mail(
    mailbox: str,
    login_started_at: datetime,
) -> tuple[str, str]:
    """
    Venter på NFOA's to-faktormail.

    Mailopslaget køres i en separat tråd, fordi
    q-outlook-api bruger synkrone HTTP-kald.

    Returnerer:
        Tuple med engangskode og message_id.
    """
    loop = asyncio.get_running_loop()

    deadline = (
        loop.time()
        + TWO_FACTOR_MAIL_TIMEOUT_SECONDS
    )

    logger.info(
        "Venter på NFOA's to-faktormail i %s.",
        mailbox,
    )

    while loop.time() < deadline:
        try:
            result = await asyncio.to_thread(
                _find_two_factor_mail,
                mailbox,
                login_started_at,
            )
        except Exception:
            logger.exception(
                "Mailopslaget fejlede. "
                "Der forsøges igen."
            )
            result = None

        if result is not None:
            logger.info(
                "NFOA's to-faktormail blev fundet."
            )
            return result

        logger.info(
            "To-faktormailen er endnu ikke modtaget. "
            "Prøver igen om %s sekunder.",
            TWO_FACTOR_POLL_INTERVAL_SECONDS,
        )

        await asyncio.sleep(
            TWO_FACTOR_POLL_INTERVAL_SECONDS
        )

    raise TimeoutError(
        "NFOA's to-faktormail blev ikke fundet "
        f"inden for {TWO_FACTOR_MAIL_TIMEOUT_SECONDS} "
        "sekunder."
    )


async def _delete_two_factor_mail(
    mailbox: str,
    message_id: str,
) -> None:
    """
    Flytter den anvendte to-faktormail til Slettet post.

    permanent_delete=False betyder, at mailen ikke
    slettes permanent. Mailen kan derfor normalt
    gendannes fra Outlook-mappen Slettet post.

    Returnerer:
        Ingen værdi.
    """
    logger.info(
        "Flytter den anvendte to-faktormail "
        "til Slettet post."
    )

    result = await asyncio.to_thread(
        delete_mail,
        user_mail=mailbox,
        message_id=message_id,
        permanent_delete=False,
    )

    if not isinstance(result, dict):
        raise RuntimeError(
            "Sletning af to-faktormailen returnerede "
            "et ugyldigt resultat."
        )

    if result.get("success") is not True:
        raise RuntimeError(
            "To-faktormailen kunne ikke flyttes "
            "til Slettet post."
        )

    logger.info(
        "To-faktormailen blev flyttet til "
        "Slettet post."
    )


async def login_nfoa(page: Page) -> None:
    """
    Logger ind i NetForvaltning Offentligt Arrangement.

    Funktionen:

    1. Henter DIRXHP fra Automation Server.
    2. Læser mailbox og NetForvaltningOA_pass.
    3. Åbner NFOA.
    4. Indtaster mailbox og adgangskode.
    5. Trykker Log ind.
    6. Venter på to-faktormailen.
    7. Udlæser den sekscifrede kode og message_id.
    8. Indtaster koden.
    9. Trykker Bekræft.
    10. Venter på, at login er gennemført.
    11. Flytter den anvendte mail til Slettet post.

    Returnerer:
        Ingen værdi. Ved succes står page på den
        indloggede side, og to-faktormailen ligger
        i Outlook-mappen Slettet post.
    """
    if page.is_closed():
        raise RuntimeError(
            "Playwright-siden er lukket."
        )

    mailbox, password = _get_login_information()

    login_started_at = datetime.now(
        timezone.utc
    )

    page.set_default_timeout(
        PAGE_TIMEOUT_MS
    )

    page.set_default_navigation_timeout(
        PAGE_TIMEOUT_MS
    )

    try:
        logger.info(
            "Åbner NetForvaltning Offentligt "
            "Arrangement."
        )

        await page.goto(
            NFOA_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        email_input = page.locator(
            NfoaSelectors.EMAIL_INPUT
        )

        password_input = page.locator(
            NfoaSelectors.PASSWORD_INPUT
        )

        login_button = page.locator(
            NfoaSelectors.LOGIN_BUTTON
        )

        await email_input.wait_for(
            state="visible",
            timeout=PAGE_TIMEOUT_MS,
        )

        await password_input.wait_for(
            state="visible",
            timeout=PAGE_TIMEOUT_MS,
        )

        await login_button.wait_for(
            state="visible",
            timeout=PAGE_TIMEOUT_MS,
        )

        logger.info(
            "Udfylder NFOA-loginformularen."
        )

        await email_input.fill(mailbox)

        await password_input.fill(password)

        logger.info(
            "Trykker på Log ind."
        )

        await login_button.click(
            timeout=PAGE_TIMEOUT_MS
        )

        two_factor_input = page.locator(
            NfoaSelectors.TWO_FACTOR_INPUT
        )

        await two_factor_input.wait_for(
            state="visible",
            timeout=PAGE_TIMEOUT_MS,
        )

        logger.info(
            "NFOA's to-faktorside er synlig."
        )

        (
            two_factor_code,
            two_factor_message_id,
        ) = await _wait_for_two_factor_mail(
            mailbox=mailbox,
            login_started_at=login_started_at,
        )

        await two_factor_input.fill(
            two_factor_code
        )

        confirm_button = page.locator(
            NfoaSelectors.TWO_FACTOR_CONFIRM_BUTTON
        )

        await confirm_button.wait_for(
            state="visible",
            timeout=PAGE_TIMEOUT_MS,
        )

        logger.info(
            "Trykker på Bekræft."
        )

        await confirm_button.click(
            timeout=PAGE_TIMEOUT_MS
        )

        await two_factor_input.wait_for(
            state="hidden",
            timeout=PAGE_TIMEOUT_MS,
        )

        logger.info(
            "Login til NFOA blev gennemført."
        )

        await _delete_two_factor_mail(
            mailbox=mailbox,
            message_id=two_factor_message_id,
        )

    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "NFOA-loginflowet nåede ikke den "
            "forventede tilstand. "
            f"Aktuel URL: {page.url}"
        ) from error