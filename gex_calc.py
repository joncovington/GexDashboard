"""
gex_calc.py — SPX Gamma Exposure (GEX) calculator.

Uses the live Schwab option chain to compute:
  - Net GEX (total dealer gamma exposure in $ billions)
  - GEX by strike (bar chart data)
  - Call Wall  — strike with highest total call GEX
  - Put Wall   — strike with highest total put GEX
  - Zero Gamma — strike where net GEX flips sign (dealer neutral)
  - 1-sigma expected move (based on ATM IV and DTE)
  - GEX regime label: 'positive' | 'negative' | 'neutral'

GEX formula (per strike):
  call_gex[K] = call_OI[K] * call_gamma[K] * spot * 100
  put_gex[K]  = put_OI[K]  * put_gamma[K]  * spot * 100
  net_gex[K]  = call_gex[K] - put_gex[K]   (dealers long puts → puts are negative)

Total net GEX = sum(net_gex[K]) over all strikes
Positive GEX → dealers are net long gamma → suppress volatility (good for IC/credit spreads)
Negative GEX → dealers are net short gamma → amplify moves

Usage:
    from gex_calc import compute_gex_snapshot
    snap = compute_gex_snapshot(client, symbol="SPX", dte_max=5)
"""

import logging
import math
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chain parsing helpers
# ---------------------------------------------------------------------------

def _parse_chain_to_rows(chain: dict) -> list[dict]:
    """
    Flatten a Schwab option chain JSON into a list of per-strike dicts.
    Each row: { expiration, dte, strike, option_type, oi, gamma, iv, bid, ask }
    Only includes contracts with gamma > 0 and OI > 0.
    """
    rows = []
    today = date.today()

    for opt_type, date_map_key in [("CALL", "callExpDateMap"), ("PUT", "putExpDateMap")]:
        date_map = chain.get(date_map_key, {})
        for exp_key, strikes in date_map.items():
            # exp_key looks like "2025-07-18:10" (date:dte)
            try:
                exp_str = exp_key.split(":")[0]   # "2025-07-18"
                exp_date = date.fromisoformat(exp_str)
                dte_val  = (exp_date - today).days
            except Exception:
                continue

            for strike_str, contracts in strikes.items():
                try:
                    strike = float(strike_str)
                except ValueError:
                    continue

                for c in contracts:
                    oi     = c.get("openInterest", 0) or 0
                    gamma  = c.get("gamma", 0.0) or 0.0
                    iv     = c.get("volatility", 0.0) or 0.0
                    bid    = c.get("bid", 0.0) or 0.0
                    ask    = c.get("ask", 0.0) or 0.0

                    if oi <= 0 or gamma <= 0:
                        continue

                    rows.append({
                        "expiration":  exp_str,
                        "dte":         dte_val,
                        "strike":      strike,
                        "option_type": opt_type,
                        "oi":          oi,
                        "gamma":       gamma,
                        "iv":          iv / 100.0,   # Schwab returns as percent
                        "bid":         bid,
                        "ask":         ask,
                    })

    return rows


def _spot_price(chain: dict) -> float:
    """Extract spot price from Schwab chain response."""
    underlying = chain.get("underlying", {})
    price = underlying.get("mark") or underlying.get("last") or underlying.get("close") or 0.0
    return float(price)


def _atm_iv(rows: list[dict], spot: float) -> float:
    """
    Estimate ATM IV by averaging IVs of contracts within 1% of spot.
    Falls back to overall mean if nothing is close enough.
    """
    band = spot * 0.01
    near = [r["iv"] for r in rows if abs(r["strike"] - spot) <= band and r["iv"] > 0]
    if near:
        return sum(near) / len(near)
    all_iv = [r["iv"] for r in rows if r["iv"] > 0]
    return sum(all_iv) / len(all_iv) if all_iv else 0.0


# ---------------------------------------------------------------------------
# Core GEX computation
# ---------------------------------------------------------------------------

def compute_gex_from_rows(rows: list[dict], spot: float) -> dict:
    """
    Given parsed option rows and spot price, compute all GEX metrics.

    Returns a dict with:
      total_net_gex    — float, in raw dollar terms (divide by 1e9 for $B)
      by_strike        — list of { strike, call_gex, put_gex, net_gex }
      call_wall        — strike with highest call GEX
      put_wall         — strike with highest put GEX
      zero_gamma       — strike where net GEX is closest to zero (sign flip)
      gex_regime       — 'positive' | 'negative' | 'neutral'
      atm_iv           — estimated ATM IV (0-1 range)
      spot             — current spot price
    """
    if not rows or spot <= 0:
        return _empty_snapshot(spot)

    # Aggregate by strike
    strike_data: dict[float, dict] = {}
    for r in rows:
        k = r["strike"]
        if k not in strike_data:
            strike_data[k] = {"call_gex": 0.0, "put_gex": 0.0}

        gex_val = r["oi"] * r["gamma"] * spot * 100

        if r["option_type"] == "CALL":
            strike_data[k]["call_gex"] += gex_val
        else:
            strike_data[k]["put_gex"]  += gex_val

    # Build sorted list
    by_strike = []
    for k in sorted(strike_data.keys()):
        d = strike_data[k]
        by_strike.append({
            "strike":   k,
            "call_gex": round(d["call_gex"], 2),
            "put_gex":  round(d["put_gex"],  2),
            "net_gex":  round(d["call_gex"] - d["put_gex"], 2),
        })

    # Total net GEX
    total_net_gex = sum(s["net_gex"] for s in by_strike)

    # Call Wall — strike with max call GEX
    call_wall = max(by_strike, key=lambda s: s["call_gex"])["strike"] if by_strike else spot

    # Put Wall — strike with max put GEX
    put_wall  = max(by_strike, key=lambda s: s["put_gex"])["strike"] if by_strike else spot

    # Zero Gamma — strike closest to net GEX == 0 (sign flip point)
    # Find the strike pair where net_gex changes sign
    zero_gamma = _find_zero_gamma(by_strike, spot)

    # Regime
    if total_net_gex > 1e8:
        regime = "positive"
    elif total_net_gex < -1e8:
        regime = "negative"
    else:
        regime = "neutral"

    # ATM IV
    atm_iv_val = _atm_iv(rows, spot)

    return {
        "total_net_gex":  round(total_net_gex, 2),
        "total_net_gex_b": round(total_net_gex / 1e9, 3),  # in billions
        "by_strike":      by_strike,
        "call_wall":      call_wall,
        "put_wall":       put_wall,
        "zero_gamma":     zero_gamma,
        "gex_regime":     regime,
        "atm_iv":         round(atm_iv_val, 4),
        "spot":           spot,
    }


def _find_zero_gamma(by_strike: list[dict], spot: float) -> float:
    """
    Find the strike where net GEX is closest to zero, giving preference
    to strikes near spot (within ±5%).
    """
    if not by_strike:
        return spot

    band = spot * 0.05
    near = [s for s in by_strike if abs(s["strike"] - spot) <= band]
    candidates = near if near else by_strike

    # Find where sign changes
    for i in range(len(candidates) - 1):
        if candidates[i]["net_gex"] * candidates[i+1]["net_gex"] < 0:
            # Linear interpolation between the two strikes
            s1, s2 = candidates[i]["strike"],   candidates[i+1]["strike"]
            g1, g2 = candidates[i]["net_gex"],  candidates[i+1]["net_gex"]
            zg = s1 + (s2 - s1) * abs(g1) / (abs(g1) + abs(g2))
            return round(zg, 0)

    # No sign change — return strike with net_gex closest to 0
    return min(candidates, key=lambda s: abs(s["net_gex"]))["strike"]


def _empty_snapshot(spot: float) -> dict:
    return {
        "total_net_gex":   0.0,
        "total_net_gex_b": 0.0,
        "by_strike":       [],
        "call_wall":       spot,
        "put_wall":        spot,
        "zero_gamma":      spot,
        "gex_regime":      "neutral",
        "atm_iv":          0.0,
        "spot":            spot,
    }


# ---------------------------------------------------------------------------
# Expected move (1-sigma)
# ---------------------------------------------------------------------------

def expected_move(spot: float, iv: float, dte: int) -> float:
    """
    1-sigma expected move in points.
    Formula: spot * IV * sqrt(DTE / 365)
    """
    if iv <= 0 or dte < 0:
        return 0.0
    return round(spot * iv * math.sqrt(dte / 365.0), 2)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_gex_snapshot(
    c,
    symbol: str = "$SPX",
    dte_max: int = 30,
) -> dict:
    """
    Fetch live option chain from Schwab and compute full GEX snapshot.

    Returns the GEX dict plus:
      symbol       — e.g. "SPX"
      timestamp    — ISO-8601 of computation time
      exp_move_1d  — 1-sigma expected move for today (DTE=1)
      exp_move_5d  — 1-sigma expected move for 5 days (DTE=5)
      error        — None or error message string
    """
    import sys
    import os
    from datetime import datetime
    from pathlib import Path

    # Use the gex_dashboard's own market-data client
    sys.path.insert(0, str(Path(__file__).parent))
    from schwab_market import fetch_option_chain

    now_str = datetime.now().isoformat()

    try:
        chain = fetch_option_chain(c, symbol, dte_min=0, dte_max=dte_max)
        spot  = _spot_price(chain)

        if spot <= 0:
            logger.warning("[gex] Could not determine spot price for %s", symbol)
            snap = _empty_snapshot(0.0)
            snap.update({"symbol": symbol, "timestamp": now_str, "error": "no spot price"})
            return snap

        rows = _parse_chain_to_rows(chain)
        logger.info("[gex] %s  spot=%.2f  rows=%d", symbol, spot, len(rows))

        snap = compute_gex_from_rows(rows, spot)

        # Add expected moves
        snap["exp_move_1d"] = expected_move(spot, snap["atm_iv"], 1)
        snap["exp_move_5d"] = expected_move(spot, snap["atm_iv"], 5)
        snap["symbol"]      = symbol
        snap["timestamp"]   = now_str
        snap["error"]       = None

        logger.info(
            "[gex] %s  net_gex=%.2fB  regime=%s  call_wall=%.0f  put_wall=%.0f  zero_gamma=%.0f",
            symbol,
            snap["total_net_gex_b"],
            snap["gex_regime"],
            snap["call_wall"],
            snap["put_wall"],
            snap["zero_gamma"],
        )
        return snap

    except Exception as e:
        logger.exception("[gex] Failed to compute GEX for %s: %s", symbol, e)
        snap = _empty_snapshot(0.0)
        snap.update({"symbol": symbol, "timestamp": now_str, "error": str(e)})
        return snap


# ---------------------------------------------------------------------------
# CLI quick-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from schwab_market import get_client

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    c = get_client()
    snap = compute_gex_snapshot(c, symbol="$SPX", dte_max=10)

    print(f"\nSPX GEX Snapshot @ {snap['timestamp']}")
    print(f"  Spot:        {snap['spot']:.2f}")
    print(f"  Net GEX:     {snap['total_net_gex_b']:.3f}B")
    print(f"  Regime:      {snap['gex_regime']}")
    print(f"  Call Wall:   {snap['call_wall']:.0f}")
    print(f"  Put Wall:    {snap['put_wall']:.0f}")
    print(f"  Zero Gamma:  {snap['zero_gamma']:.0f}")
    print(f"  ATM IV:      {snap['atm_iv']*100:.1f}%")
    print(f"  1-sigma 1d:  ±{snap['exp_move_1d']:.1f} pts")
    print(f"  1-sigma 5d:  ±{snap['exp_move_5d']:.1f} pts")
    print(f"\n  Top 5 strikes by |net_gex|:")
    top = sorted(snap["by_strike"], key=lambda s: abs(s["net_gex"]), reverse=True)[:5]
    for s in top:
        print(f"    {s['strike']:>7.0f}  net={s['net_gex']/1e6:+8.1f}M  "
              f"call={s['call_gex']/1e6:.1f}M  put={s['put_gex']/1e6:.1f}M")
