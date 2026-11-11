"""Playwright-selectors til NFOA."""


class NfoaSelectors:
    """Selectors til login, to-faktor og sagsoversigt."""

    # --------------------------------------------------------
    # LOGIN
    # --------------------------------------------------------

    EMAIL_INPUT = (
        "#Main_nfx_login_Login1_ctl00_txtEmail"
    )

    PASSWORD_INPUT = (
        "#Main_nfx_login_Login1_ctl00_txtPwd"
    )

    LOGIN_BUTTON = (
        "#Main_nfx_login_Login1_ctl00_btnLogin"
    )

    # --------------------------------------------------------
    # TO-FAKTOR
    # --------------------------------------------------------

    TWO_FACTOR_INPUT = (
        "#twoFactorAuthentication_txtTwoFactorCode"
    )

    TWO_FACTOR_CONFIRM_BUTTON = (
        "#twoFactorAuthentication_btnLogin"
    )

    # --------------------------------------------------------
    # NYE SAGER
    # --------------------------------------------------------

    CASE_TABLES = "table"

    CASE_TABLE_ROWS = "tbody tr"

    CASE_TABLE_FALLBACK_ROWS = "tr"

    CASE_HEADER_CELLS = "thead th"

    CASE_FALLBACK_HEADER_CELLS = (
        "tr:first-child th"
    )

    CASE_DATA_CELLS = "td"

    CASE_LINKS = "a[href]"

    # Elementerne bruger ASP.NET __doPostBack og indeholder
    # btnSelectEventName i event target.
    CASE_SELECT_LINKS = (
        "a[href*='btnSelectEventName'], "
        "a[onclick*='btnSelectEventName'], "
        "input[onclick*='btnSelectEventName']"
    )