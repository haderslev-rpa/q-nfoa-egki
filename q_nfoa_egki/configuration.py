"""Konfiguration til q-nfoa-egki."""


# ------------------------------------------------------------
# NFOA
# ------------------------------------------------------------

NFOA_URL = "https://nfoa.egki.dk/"

NFOA_NEW_CASES_URL = (
    "https://nfoa.egki.dk/"
    "CaseListOverview.aspx?CaseViewEnum=NewCase"
)


# ------------------------------------------------------------
# AUTOMATION SERVER
# ------------------------------------------------------------

NFOA_CREDENTIAL_NAME = "DIRXHP"

NFOA_PASSWORD_FIELD = "NetForvaltningOA_pass"

NFOA_MAILBOX_FIELD = "mailbox"


# ------------------------------------------------------------
# TO-FAKTOR-MAIL
# ------------------------------------------------------------

TWO_FACTOR_MAIL_SUBJECT = (
    "Engangskode til 2-faktor godkendelse"
)

TWO_FACTOR_MAIL_SENDER = "ki-noreply@eg.dk"

TWO_FACTOR_MAIL_FOLDER = "inbox"

TWO_FACTOR_MAIL_LIMIT = 10

TWO_FACTOR_MAIL_TIMEOUT_SECONDS = 180

TWO_FACTOR_POLL_INTERVAL_SECONDS = 5

TWO_FACTOR_MAIL_TIME_TOLERANCE_SECONDS = 10


# ------------------------------------------------------------
# PLAYWRIGHT
# ------------------------------------------------------------

PAGE_TIMEOUT_MS = 60_000