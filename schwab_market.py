"""
schwab_market.py — Lightweight Schwab market-data-only client for the GEX dashboard.

Uses a separate Schwab app registered with "Market Data Production" scope only.
No account hash, no trading permissions.

Credentials are stored in Windows Credential Manager under GexDashboard/Schwab.
On first run, the app prompts for credentials and saves them automatically.
To update credentials, use:
  import keyring; keyring.set_password("GexDashboard/Schwab", "client_id", "new_value")
Or use the Windows Credential Manager UI.

First-time OAuth setup — run once after entering credentials:
    python schwab_market.py --setup
"""

import sys
import logging
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import schwab
    from schwab import auth, client as schwab_client_lib
except ImportError:
    raise ImportError("schwab-py is not installed. Run: pip install schwab-py")

from credentials import get_schwab_credentials

_creds = get_schwab_credentials()
CLIENT_ID     = _creds["client_id"]
CLIENT_SECRET = _creds["client_secret"]
REDIRECT_URI  = _creds["redirect_uri"]
TOKEN_PATH    = _creds["token_path"]


def get_client() -> schwab_client_lib.Client:
    """
    Return an authenticated Schwab client using the saved market-data token.
    Run  python schwab_market.py --setup  once to complete the first OAuth login.
    """
    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError(
            "Schwab credentials not found in Windows Credential Manager.\n"
            "Delete the GexDashboard/Schwab entries and restart to re-enter them,\n"
            "or set them directly:\n"
            "  import keyring\n"
            "  keyring.set_password('GexDashboard/Schwab', 'client_id', 'your_id')\n"
            "Then run:  python schwab_market.py --setup"
        )

    if not os.path.exists(TOKEN_PATH):
        raise FileNotFoundError(
            f"Token file not found: {TOKEN_PATH}\n"
            f"Run  python schwab_market.py --setup  to complete first-time OAuth login."
        )

    c = auth.client_from_token_file(
        token_path=TOKEN_PATH,
        api_key=CLIENT_ID,
        app_secret=CLIENT_SECRET,
    )
    logger.info("[schwab_market] Client ready (token: %s)", TOKEN_PATH)
    return c


def fetch_option_chain(
    c: schwab_client_lib.Client,
    symbol: str,
    dte_min: int = 0,
    dte_max: int = 30,
    strike_count: int = 200,
) -> dict:
    """
    Fetch option chain for a symbol. Returns raw Schwab JSON.
    Use '$SPX' for SPX index options.
    """
    from_date = date.today() + timedelta(days=dte_min)
    to_date   = date.today() + timedelta(days=dte_max)

    resp = c.get_option_chain(
        symbol,
        contract_type=c.Options.ContractType.ALL,
        include_underlying_quote=True,
        strategy=c.Options.Strategy.SINGLE,
        from_date=from_date,
        to_date=to_date,
        strike_count=strike_count,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# First-time OAuth setup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    parser = argparse.ArgumentParser(description="Schwab Market Data — OAuth setup")
    parser.add_argument("--setup", action="store_true", help="Run first-time OAuth login")
    args = parser.parse_args()

    if args.setup:
        if not CLIENT_ID:
            print("ERROR: Schwab client_id not found in Windows Credential Manager.")
            print("       Restart the script to be prompted for credentials.")
            sys.exit(1)

        token_dir = Path(TOKEN_PATH).parent
        token_dir.mkdir(parents=True, exist_ok=True)

        print(f"\nStarting OAuth login for Market Data app...")
        print(f"Token will be saved to: {TOKEN_PATH}\n")

        c = auth.client_from_login_flow(
            api_key=CLIENT_ID,
            app_secret=CLIENT_SECRET,
            callback_url=REDIRECT_URI,
            token_path=TOKEN_PATH,
        )
        print("\nSetup complete! Token saved.")
        print("You can now run:  python gex_app.py")
    else:
        parser.print_help()
