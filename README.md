# SPX GEX Live Dashboard

A real-time Gamma Exposure (GEX) dashboard for SPX options traders. Fetches live option chain data and streams real-time quotes via **Schwab** or **tastytrade** (DXLink WebSocket), computes dealer gamma positioning, and displays key levels with a butterfly spread signal overlay.

---

## What It Does

The dashboard runs a local web server and auto-refreshes every 60 seconds during market hours (8:00 AM – 4:15 PM ET). It shows:

- **Net GEX** — total dealer gamma exposure in dollar terms, and whether the market is in a positive (volatility-suppressing) or negative (volatility-amplifying) regime
- **Call Wall** — the strike with the highest call-side gamma; acts as resistance
- **Put Wall** — the strike with the highest put-side gamma; acts as support
- **Zero Gamma** — the price level where dealer gamma flips from stabilizing to amplifying
- **ATM IV and expected move** — implied volatility and 1-sigma daily and 5-day ranges
- **GEX by strike** — bar chart showing call and net GEX across strikes near the current price
- **Intraday net GEX** — line chart of GEX through the trading day, with positive/negative regime reference lines
- **Butterfly spread signal** — real-time trade setup guidance based on where price is relative to the GEX walls (see below)

Intraday history is saved to disk in the `data/` folder and survives restarts, so the line chart stays intact if you need to reboot mid-day.

---

## Butterfly Spread Signal

The signal section analyzes where SPX is relative to the GEX walls and tells you which type of spread to consider opening as Leg 1 of a legged butterfly. It covers five scenarios:

| Signal | Condition | Trade |
|---|---|---
| **Call Side** | Spot above Call Wall | Sell call credit spread, short strike at Call Wall |
| **Put Side** | Spot below Put Wall | Sell put credit spread, short strike at Put Wall |
| **Near Wall** | Within 50 pts of a wall | Pre-stage the spread; wait for confirmed break |
| **Between Walls** | Inside range, above Zero Gamma | No butterfly edge; iron condor shown instead |
| **Warning / Avoid** | Below Zero Gamma or negative regime | Do not enter; dealer hedging amplifies moves |

Each signal includes a zone-specific entry checklist (regime, Zero Gamma, wall strength, IV) and suggested strikes sized to a $5 wide spread.

---

## Requirements

- Python 3.11+
- For Schwab provider: a Schwab developer account with an app that has **Market Data Production** scope
- For tastytrade provider: a tastytrade brokerage account

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Setup

Credentials are stored securely in **Windows Credential Manager** — no `.env` files needed. On first run, the app prompts for credentials and saves them automatically.

To update a credential later:
```python
import keyring
keyring.set_password("GexDashboard/Schwab", "client_id", "new_value")
```
Or open **Windows Credential Manager** → Generic Credentials and edit `GexDashboard/Schwab` or `GexDashboard/TastyTrade`.

### Schwab provider (default)

#### 1. First run — enter credentials when prompted

Start the app and you will be prompted for:
- **Schwab client ID** and **client secret** — from [developer.schwab.com](https://developer.schwab.com), app with Market Data Production scope
- **Schwab redirect URI** — must match your app settings, e.g. `https://127.0.0.1:8182`
- **Schwab token file path** — where to save the OAuth token, e.g. `C:\Users\you\.schwab_token.json`

#### 2. Complete OAuth login (first time only)

```bash
python schwab_market.py --setup
```

This opens a browser window for Schwab login. After authorizing, the token is saved. The `schwab-py` library handles token refresh automatically.

#### 3. Run the dashboard

```bash
python gex_app.py
```

### tastytrade provider

#### 1. First run — enter credentials when prompted

Start the app with `--provider tastytrade` and you will be prompted for your tastytrade **username** and **password**. They are stored in Windows Credential Manager under `GexDashboard/TastyTrade`.

#### 2. Run the dashboard

```bash
python gex_app.py --provider tastytrade --symbol SPX
```

Note: Use `--symbol SPX` (not `$SPX`) with the tastytrade provider.

Then open your browser to: [http://127.0.0.1:5556](http://127.0.0.1:5556)

---

## Command Line Options

| Option | Default | Description |
|---|---|---|
| `--provider` | `schwab` | Data provider: `schwab` or `tastytrade` |
| `--symbol` | `$SPX` | Option chain symbol (`$SPX` for Schwab, `SPX` for tastytrade) |
| `--dte-max` | `30` | Maximum days to expiration to include |
| `--port` | `5556` | Port to serve the dashboard on |
| `--host` | `127.0.0.1` | Host to bind to |
| `--dry-run` | off | Use fake data (no API calls — useful for UI testing) |

Example with tastytrade:

```bash
python gex_app.py --provider tastytrade --symbol SPX --dte-max 10
```

Dry run for UI testing without any API connection:

```bash
python gex_app.py --dry-run
```

---

## File Structure

```
GexDashboard/
├── gex_app.py              # Flask app, dashboard HTML, butterfly signal logic
├── gex_calc.py             # GEX computation (provider-agnostic)
├── schwab_market.py        # Schwab API client and OAuth setup
├── tastytrade_market.py    # tastytrade + DXLink WebSocket provider
├── credentials.py          # Windows Credential Manager helpers
├── requirements.txt        # Python dependencies
├── .gitignore
├── data/
│   └── .gitkeep            # Folder kept in repo; history files written here at runtime
└── README.md
```

---

## Token Refresh (Schwab)

Schwab OAuth tokens expire periodically. If the dashboard stops fetching data, re-run the setup script to get a fresh token:

```bash
python schwab_market.py --setup
```

tastytrade sessions are managed automatically by the `tastytrade` SDK — no manual token refresh required.

---

## API Endpoint

The dashboard exposes a JSON endpoint if you want to consume the GEX data programmatically:

```
GET http://127.0.0.1:5556/api/gex
```

Returns the current GEX snapshot including spot price, all key levels, regime, and intraday history.

To trigger a manual refresh from outside the browser:

```
POST http://127.0.0.1:5556/api/refresh
```
