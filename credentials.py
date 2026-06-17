"""
credentials.py — Windows Credential Manager storage for GEX Dashboard.

Credentials are stored in Windows Credential Manager under:
  GexDashboard/Schwab    — Schwab API credentials
  GexDashboard/TastyTrade — tastytrade credentials

On first run, missing credentials are prompted interactively and stored.
To update a stored credential:
  import keyring
  keyring.set_password("GexDashboard/Schwab", "client_secret", "new_value")
Or use the Windows Credential Manager UI (search "Credential Manager" in Start).
"""

import getpass
import keyring


def _get_or_prompt(service: str, key: str, prompt: str, secret: bool = True) -> str:
    val = keyring.get_password(service, key)
    if not val:
        val = getpass.getpass(prompt) if secret else input(prompt)
        keyring.set_password(service, key, val)
    return val


def get_schwab_credentials() -> dict:
    svc = "GexDashboard/Schwab"
    return {
        "client_id":     _get_or_prompt(svc, "client_id",     "Schwab client ID: ",            secret=False),
        "client_secret": _get_or_prompt(svc, "client_secret", "Schwab client secret: "),
        "redirect_uri":  _get_or_prompt(svc, "redirect_uri",  "Schwab redirect URI: ",          secret=False),
        "token_path":    _get_or_prompt(svc, "token_path",    "Schwab token file path: ",       secret=False),
    }


def get_tastytrade_credentials() -> dict:
    svc = "GexDashboard/TastyTrade"
    return {
        "username": _get_or_prompt(svc, "username", "TastyTrade username: ", secret=False),
        "password": _get_or_prompt(svc, "password", "TastyTrade password: "),
    }
