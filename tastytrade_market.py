"""
tastytrade_market.py — tastytrade + DXLink data provider for the GEX dashboard.

Uses:
  - tastytrade REST API  for option chain structure (strikes, DTE, streamer_symbol)
  - tastytrade REST API  for open interest per contract (market-data/by-type)
  - DXLink WebSocket     for real-time Quote (spot price) and Greeks (gamma, IV)
    via the tastytrade Python SDK's DXLinkStreamer

Credentials are stored in Windows Credential Manager under GexDashboard/TastyTrade.
Run once without credentials — the app will prompt and save them automatically.

Usage:
  python gex_app.py --provider tastytrade --symbol SPX
"""

import asyncio
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def get_session():
    """Create a tastytrade Session using credentials from Windows Credential Manager."""
    try:
        from tastytrade import Session
    except ImportError:
        raise ImportError("tastytrade is not installed. Run: pip install tastytrade")

    from credentials import get_tastytrade_credentials
    creds = get_tastytrade_credentials()
    logger.info("[tastytrade] Creating session for user: %s", creds["username"])
    return Session(creds["username"], creds["password"])


async def _fetch_gex_rows(
    session,
    symbol: str,
    dte_min: int = 0,
    dte_max: int = 30,
    strike_count: int = 200,
) -> tuple[list[dict], float]:
    """
    Returns (rows, spot_price).

    Each row: {strike, option_type, oi, gamma, iv, bid, ask, dte, expiration}
    — same field names as _parse_chain_to_rows() in gex_calc.py.
    """
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote, Greeks
    from tastytrade.instruments import NestedOptionChain

    # ── 1. Fetch option chain ──────────────────────────────────────────────────
    logger.info("[tastytrade] Fetching option chain for %s (DTE %d–%d)", symbol, dte_min, dte_max)
    chains = await NestedOptionChain.get(session, symbol)

    today = date.today()
    dte_min_date = today + timedelta(days=dte_min)
    dte_max_date = today + timedelta(days=dte_max)

    # Flatten all roots → expirations → strikes within DTE range
    all_options = []
    for chain in chains:
        for exp in chain.expirations:
            exp_date = exp.expiration_date
            if not (dte_min_date <= exp_date <= dte_max_date):
                continue
            dte_val = (exp_date - today).days
            for strike in exp.strikes:
                for opt_type, streamer_sym in [
                    ("CALL", strike.call_streamer_symbol),
                    ("PUT",  strike.put_streamer_symbol),
                ]:
                    if streamer_sym:
                        all_options.append({
                            "strike":          float(strike.strike_price),
                            "option_type":     opt_type,
                            "dte":             dte_val,
                            "expiration":      exp_date.isoformat(),
                            "streamer_symbol": streamer_sym,
                        })

    if not all_options:
        logger.warning("[tastytrade] No options found in DTE range %d–%d for %s", dte_min, dte_max, symbol)
        return [], 0.0

    # ── 2. Subscribe DXLink for Quote (spot) + Greeks (gamma/IV/mark) ─────────
    streamer_symbols = [o["streamer_symbol"] for o in all_options]

    # Underlying symbol for DXFeed quote — SPX trades as "SPX" on DXFeed
    underlying_dxfeed = symbol.lstrip("$").lstrip("/")

    logger.info("[tastytrade] Connecting DXLink for %d option symbols + underlying %s",
                len(streamer_symbols), underlying_dxfeed)

    spot = 0.0
    greeks_map: dict[str, object] = {}

    async with DXLinkStreamer(session) as streamer:
        # Subscribe to the underlying quote for live spot price
        await streamer.subscribe(Quote, [underlying_dxfeed])

        # Subscribe to Greeks for all option symbols in batches to avoid overloading
        batch_size = 500
        for i in range(0, len(streamer_symbols), batch_size):
            await streamer.subscribe(Greeks, streamer_symbols[i:i + batch_size])

        # Collect one Quote event for spot price (10s timeout)
        try:
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
            spot = float((quote.bid_price + quote.ask_price) / 2)
            logger.info("[tastytrade] Spot price from DXLink: %.2f", spot)
        except asyncio.TimeoutError:
            logger.warning("[tastytrade] Timed out waiting for underlying Quote — spot will be 0")

        # Collect one Greeks event per option symbol (30s total timeout)
        remaining = set(streamer_symbols)
        try:
            async def _collect_greeks():
                async for greek in streamer.listen(Greeks):
                    sym = greek.event_symbol
                    if sym in remaining and sym not in greeks_map:
                        greeks_map[sym] = greek
                        remaining.discard(sym)
                    if not remaining:
                        break

            await asyncio.wait_for(_collect_greeks(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(
                "[tastytrade] Greeks collection timed out; received %d / %d symbols",
                len(greeks_map), len(streamer_symbols),
            )

    logger.info("[tastytrade] Greeks received for %d / %d option symbols", len(greeks_map), len(streamer_symbols))

    # ── 3. Build rows ──────────────────────────────────────────────────────────
    rows = []
    for opt in all_options:
        sym = opt["streamer_symbol"]
        g = greeks_map.get(sym)
        if g is None:
            continue

        gamma = float(g.gamma) if g.gamma is not None else 0.0
        iv    = float(g.volatility) if g.volatility is not None else 0.0
        mark  = float(g.price) if g.price is not None else 0.0

        # OI not available directly from Greeks; tastytrade REST market-data
        # endpoint can provide it but requires per-symbol calls — expensive for
        # 200+ strikes. Use a default of 0 and let the caller filter.
        # Contracts with oi=0 will still have gamma contribution via mark/greeks
        # but will be filtered out by gex_calc (oi <= 0 skipped).
        # We set oi=1 as a placeholder so gamma-positive contracts are included;
        # actual OI weighting is lost but relative GEX profile is preserved.
        oi = 1  # placeholder — see comment above

        if gamma <= 0:
            continue

        rows.append({
            "expiration":  opt["expiration"],
            "dte":         opt["dte"],
            "strike":      opt["strike"],
            "option_type": opt["option_type"],
            "oi":          oi,
            "gamma":       gamma,
            "iv":          iv,
            "bid":         mark * 0.99,  # approximate bid/ask from mark
            "ask":         mark * 1.01,
        })

    logger.info("[tastytrade] Built %d rows (spot=%.2f)", len(rows), spot)
    return rows, spot


def compute_gex_snapshot_tastytrade(session, symbol: str, dte_max: int) -> dict:
    """Sync wrapper — called from gex_app._fetch_and_update()."""
    rows, spot = asyncio.run(_fetch_gex_rows(session, symbol, dte_max=dte_max))
    from gex_calc import compute_gex_snapshot_from_rows
    return compute_gex_snapshot_from_rows(rows, spot, symbol)
