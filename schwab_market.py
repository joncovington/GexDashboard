"""
schwab_market.py — Lightweight Schwab market-data-only client for the GEX dashboard.

Uses a separate Schwab app registered with "Market Data Production" scope only.
No account hash, no trading permissions.

Credentials are loaded from gex_dashboard/.env:
  SCHWAB_CLIENT_ID      — new read-only app key
  SCHWAB_CLIENT_SECRET  — new read-only app secret
  SCHWAB_REDIRECT_URI   — https://127.0.0.1:8182
  SCHWAB_TOKEN_PATH     — path to THIS app's token file (separate from AutoTrading)

First-time setup — run once to complete OAuth login:
    python schwab_market.py --setup
"""

import os
import sys
import logging
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Load THIS folder's .env (not the options_tracker one)
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

try:
    import schwab
    from schwab import auth, client as schwab_client_lib
except ImportError:
    raise ImportError("schwab-py is not installed. Run: pip install schwab-py")

CLIENT_ID     = os.getenv("SCHWAB_CLIENT_ID")
CLIENT_SECRET = os.getenv("SCHWAB_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1:8182")
TOKEN_PATH    = os.getenv(
    "SCHWAB_TOKEN_PATH",
    str(Path(__file__).parent / "data" / "schwab_token.json"),
)


def get_client() -> schwab_client_lib.Client:
    """
    Return an authenticated Schwab client using the saved market-data token.
    Run  python schwab_market.py --setup  once to complete the first OAuth login.
    """
    if not CLIENT_ID or not CLIENT_SECRET or CLIENT_ID == "YOUR_NEW_CLIENT_ID":
        raise ValueError(
            "GEX dashboard credentials not configured.\n"
            "Edit gex_dashboard/.env and fill in SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET\n"
            "from your new Market Data app at developer.schwab.com.\n"
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
        if not CLIENT_ID or CLIENT_ID == "YOUR_NEW_CLIENT_ID":
            print("ERROR: Fill in SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET in gex_dashboard/.env first.")
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
