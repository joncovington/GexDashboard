# SPX GEX Live Dashboard

A real-time Gamma Exposure (GEX) dashboard for SPX options traders. Fetches live option chain data from the Schwab API, computes dealer gamma positioning, and displays key levels with a butterfly spread signal overlay.

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
- A Schwab developer account with an app that has **Market Data Production** scope
- The `schwab-py` library and supporting packages (see `requirements.txt`)

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Setup

### 1. Configure credentials

Copy the example env file and fill in your Schwab app credentials:

```bash
cp .env.example .env
```

Edit `.env` with your values:

```
SCHWAB_CLIENT_ID=your_client_id_here
SCHWAB_CLIENT_SECRET=your_client_secret_here
SCHWAB_REDIRECT_URI=https://127.0.0.1:8182
SCHWAB_TOKEN_PATH=/path/to/your/.schwab_token.json
```

You can get your client ID and secret from [developer.schwab.com](https://developer.schwab.com). The redirect URI must match exactly what is registered in your Schwab app settings.

### 2. Complete OAuth login (first time only)

Run the setup script once to authenticate and save your token:

```bash
python schwab_market.py --setup
```

This opens a browser window for Schwab login. After authorizing, the token is saved to the path you set in `SCHWAB_TOKEN_PATH`. The `schwab-py` library handles token refresh automatically after that.

### 3. Run the dashboard

```bash
python gex_app.py
```

Then open your browser to: [http://127.0.0.1:5556](http://127.0.0.1:5556)

---

## Command Line Options

| Option | Default | Description |
|---|---|---|
| `--symbol` | `$SPX` | Option chain symbol to fetch |
| `--dte-max` | `30` | Maximum days to expiration to include |
| `--port` | `5556` | Port to serve the dashboard on |
| `--host` | `127.0.0.1` | Host to bind to |
| `--dry-run` | off | Use fake data instead of live Schwab API (useful for UI testing) |

Example with options:

```bash
python gex_app.py --dte-max 10 --port 8080
```

Dry run for UI testing without a Schwab connection:

```bash
python gex_app.py --dry-run
```

---

## File Structure

```
gex_dashboard/
├── gex_app.py          # Flask app, dashboard HTML, butterfly signal logic
├── gex_calc.py         # GEX computation from Schwab option chain data
├── schwab_market.py    # Schwab API client and OAuth setup
├── requirements.txt    # Python dependencies
├── .env.example        # Credential template (copy to .env)
├── .gitignore
├── data/
│   └── .gitkeep        # Folder kept in repo; history files written here at runtime
└── README.md
```

---

## Token Refresh

Schwab OAuth tokens expire periodically. If the dashboard stops fetching data, re-run the setup script to get a fresh token:

```bash
python schwab_market.py --setup
```

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
