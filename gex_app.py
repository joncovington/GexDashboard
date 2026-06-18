"""
gex_app.py — SPX GEX Live Dashboard

Serves a dark-themed trading dashboard at http://127.0.0.1:5556

Features:
  - Auto-refreshes GEX every 60 seconds between 8:00 AM – 4:15 PM ET
  - GEX bar chart by strike (Chart.js)
  - Intraday GEX line chart (net GEX over time)
  - Key level cards: Call Wall, Put Wall, Zero Gamma, 1-sigma range
  - Regime badge: POSITIVE / NEGATIVE / NEUTRAL
  - Manual refresh button (always available)
  - /api/gex endpoint for JSON data

Run:
    python gex_app.py

Optional args:
    --symbol SPXW   (default: SPX)
    --dte-max 10    (default: 30)
    --port 5556     (default: 5556)
    --dry-run       (skip Schwab API, use fake data for UI testing)
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template_string

# Ensure gex_dashboard modules are importable
sys.path.insert(0, str(Path(__file__).parent))

from gex_calc import compute_gex_snapshot, _empty_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)),
        logging.FileHandler(Path(__file__).parent / "gex.log", encoding="utf-8"),
    ],
)
# Suppress verbose httpx/httpcore request logs from schwab-py
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_OPEN_H  = 8     # 8:00 AM ET
MARKET_CLOSE_H = 16    # 4:00 PM ET
MARKET_CLOSE_M = 15    # 4:15 PM ET

app = Flask(__name__)

# ---------------------------------------------------------------------------
# History persistence
# ---------------------------------------------------------------------------
DATA_DIR     = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

def _history_file() -> Path:
    """Returns path to today's history file, e.g. data/gex_history_2026-05-04.json"""
    return DATA_DIR / f"gex_history_{date.today().isoformat()}.json"


def _load_history() -> list[dict]:
    """Load today's intraday history from disk, or return empty list."""
    path = _history_file()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            logger.info("[app] Loaded %d history points from %s", len(data), path.name)
            return data
        except Exception as e:
            logger.warning("[app] Could not load history file: %s", e)
    return []


def _save_history(history: list[dict]):
    """Persist today's intraday history to disk."""
    try:
        _history_file().write_text(
            json.dumps(history, indent=None), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("[app] Could not save history file: %s", e)


def _prune_old_history():
    """Delete history files older than 7 days to avoid accumulation."""
    try:
        today = date.today()
        for f in DATA_DIR.glob("gex_history_*.json"):
            try:
                file_date = date.fromisoformat(f.stem.replace("gex_history_", ""))
                if (today - file_date).days > 7:
                    f.unlink()
                    logger.info("[app] Pruned old history file: %s", f.name)
            except ValueError:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_current_snap: dict = {}
_history: list[dict] = _load_history()   # load today's history on startup
_args = None                              # parsed CLI args, set in main()


def _is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:        # Saturday / Sunday
        return False
    start = now.replace(hour=MARKET_OPEN_H, minute=0, second=0, microsecond=0)
    end   = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)
    return start <= now <= end


def _fetch_and_update():
    global _current_snap, _history
    try:
        dte_max  = _args.dte_max  if _args else 30
        symbol   = _args.symbol   if _args else "$SPX"
        provider = _args.provider if _args else "schwab"

        if _args and _args.dry_run:
            snap = _fake_snapshot()
        elif provider == "tastytrade":
            from tastytrade_market import get_session, compute_gex_snapshot_tastytrade
            session = get_session()
            snap = compute_gex_snapshot_tastytrade(session, symbol=symbol, dte_max=dte_max)
        else:
            from schwab_market import get_client
            c = get_client()
            snap = compute_gex_snapshot(c, symbol=symbol, dte_max=dte_max)

        with _state_lock:
            _current_snap = snap
            if snap.get("total_net_gex_b") is not None:
                _history.append({
                    "timestamp": snap["timestamp"],
                    "net_gex_b": snap["total_net_gex_b"],
                    "spot":      snap.get("spot", 0),
                })
                # Keep only today's data
                today = date.today().isoformat()
                _history = [h for h in _history if h["timestamp"].startswith(today)]
                _save_history(_history)

        logger.info("[app] Updated GEX: spot=%.2f  gex=%.3fB  regime=%s",
                    snap.get("spot", 0),
                    snap.get("total_net_gex_b", 0),
                    snap.get("gex_regime", "?"))
    except Exception as e:
        logger.exception("[app] Fetch failed: %s", e)


def _background_loop():
    """Fetch GEX every 60 seconds while market is open, else every 5 minutes."""
    # Initial fetch on startup regardless of market hours
    _fetch_and_update()

    while True:
        if _is_market_hours():
            sleep_secs = 60
        else:
            sleep_secs = 300    # 5 min outside market hours
        time.sleep(sleep_secs)
        _fetch_and_update()


# ---------------------------------------------------------------------------
# Fake snapshot for dry-run / UI dev
# ---------------------------------------------------------------------------

def _fake_snapshot() -> dict:
    import math, random
    from datetime import datetime
    spot = 5825.0 + random.uniform(-20, 20)
    strikes = list(range(5600, 6050, 5))
    by_strike = []
    for k in strikes:
        dist = (k - spot) / spot
        call_gex     = max(0, (1 - abs(dist + 0.01) * 20) * 5e8 + random.uniform(-5e7, 5e7))
        put_gex      = max(0, (1 - abs(dist - 0.01) * 20) * 5e8 + random.uniform(-5e7, 5e7))
        call_vol_gex = call_gex * random.uniform(0.3, 0.7)
        put_vol_gex  = put_gex  * random.uniform(0.3, 0.7)
        by_strike.append({
            "strike":       k,
            "call_gex":     call_gex,
            "put_gex":      put_gex,
            "net_gex":      call_gex - put_gex,
            "call_vol_gex": call_vol_gex,
            "put_vol_gex":  put_vol_gex,
            "net_vol_gex":  call_vol_gex - put_vol_gex,
        })
    total     = sum(s["net_gex"]     for s in by_strike)
    total_vol = sum(s["net_vol_gex"] for s in by_strike)
    return {
        "symbol":               "SPX",
        "spot":                  round(spot, 2),
        "total_net_gex":         round(total, 2),
        "total_net_gex_b":       round(total / 1e9, 3),
        "total_net_vol_gex":     round(total_vol, 2),
        "total_net_vol_gex_b":   round(total_vol / 1e9, 3),
        "by_strike":             by_strike,
        "call_wall":             spot + 50,
        "put_wall":              spot - 50,
        "zero_gamma":            spot + 10,
        "gex_regime":            "positive" if total > 0 else "negative",
        "atm_iv":                0.142,
        "exp_move_1d":           round(spot * 0.142 * math.sqrt(1/365), 1),
        "exp_move_5d":           round(spot * 0.142 * math.sqrt(5/365), 1),
        "timestamp":             datetime.now().isoformat(),
        "error":                 None,
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/gex")
def api_gex():
    with _state_lock:
        snap = dict(_current_snap)
        hist = list(_history)
    snap["history"] = hist
    return jsonify(snap)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=_fetch_and_update, daemon=True).start()
    return jsonify({"status": "refresh started"})


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SPX GEX Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3/dist/chartjs-plugin-annotation.min.js"></script>
<style>
  :root {
    --bg:       #0d0d0d;
    --surface:  #1a1a1a;
    --border:   #2a2a2a;
    --text:     #e0e0e0;
    --muted:    #888;
    --green:    #00c853;
    --red:      #ff1744;
    --yellow:   #ffd600;
    --blue:     #2196f3;
    --purple:   #9c27b0;
    --call:     #00c853;
    --put:      #ff5722;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Courier New', monospace; font-size: 13px; }

  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 20px; background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  header h1 { font-size: 18px; letter-spacing: 2px; color: var(--call); }
  .header-right { display: flex; align-items: center; gap: 16px; }

  #regime-badge {
    padding: 4px 12px; border-radius: 4px; font-size: 12px;
    font-weight: bold; letter-spacing: 1px;
  }
  .badge-positive { background: #003300; color: var(--green); border: 1px solid var(--green); }
  .badge-negative { background: #330000; color: var(--red);   border: 1px solid var(--red);   }
  .badge-neutral  { background: #222200; color: var(--yellow);border: 1px solid var(--yellow);}

  #status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  #status-dot.live { background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

  #last-update { color: var(--muted); font-size: 11px; }

  button#refresh-btn {
    background: var(--border); border: 1px solid #444; color: var(--text);
    padding: 5px 14px; border-radius: 4px; cursor: pointer; font-family: inherit;
    font-size: 12px;
  }
  button#refresh-btn:hover { background: #333; }

  .kpi-row {
    display: flex; gap: 12px; padding: 14px 20px;
    flex-wrap: wrap;
  }
  .kpi-card {
    flex: 1; min-width: 160px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 12px 16px;
  }
  .kpi-label { color: var(--muted); font-size: 13px; letter-spacing: 1px; margin-bottom: 6px; }
  .kpi-value { font-size: 22px; font-weight: bold; }
  .kpi-sub   { font-size: 11px; color: var(--muted); margin-top: 4px; }

  .charts-row {
    display: flex; gap: 12px; padding: 0 20px 14px;
    flex-wrap: wrap;
  }
  .chart-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 14px; flex: 1; min-width: 300px;
  }
  .chart-title { font-size: 11px; color: var(--muted); letter-spacing: 1px; margin-bottom: 12px; }
  .chart-wrap { position: relative; height: 280px; }

  .levels-row {
    display: flex; gap: 12px; padding: 0 20px 14px; flex-wrap: wrap;
  }
  .level-card {
    flex: 1; min-width: 150px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px;
  }
  .level-label { font-size: 13px; color: var(--muted); letter-spacing: 1px; margin-bottom: 4px; }
  .level-value { font-size: 18px; font-weight: bold; }

  .error-banner {
    background: #330000; border: 1px solid var(--red);
    color: var(--red); padding: 8px 20px; font-size: 12px; display: none;
  }

  /* ── Butterfly Signal Section ── */
  .signal-section { padding: 0 20px 14px; }
  .signal-section-title {
    font-size: 13px; color: var(--muted); letter-spacing: 2px;
    margin-bottom: 10px; padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }
  .signal-body { display: flex; gap: 12px; flex-wrap: wrap; }

  .signal-card {
    flex: 2; min-width: 280px; border-radius: 6px;
    padding: 14px 18px; background: var(--surface);
    border: 2px solid var(--border);
  }
  .signal-card.call-side  { border-color: var(--green); }
  .signal-card.put-side   { border-color: var(--put); }
  .signal-card.between    { border-color: var(--yellow); }
  .signal-card.avoid      { border-color: #444; }

  .signal-label { font-size: 13px; color: var(--muted); letter-spacing: 1px; margin-bottom: 6px; }
  .signal-name  { font-size: 20px; font-weight: bold; margin-bottom: 6px; }
  .signal-desc  { font-size: 12px; color: var(--muted); line-height: 1.5; }

  .checklist-card {
    flex: 1; min-width: 200px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 6px; padding: 14px 18px;
  }
  .checklist-title { font-size: 13px; color: var(--muted); letter-spacing: 1px; margin-bottom: 10px; }
  .check-item { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; font-size: 12px; }
  .check-dot  {
    width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
    background: var(--muted);
  }
  .check-dot.pass { background: var(--green); }
  .check-dot.fail { background: var(--red); }
  .check-dot.warn { background: var(--yellow); }

  .strikes-card {
    flex: 1; min-width: 200px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 6px; padding: 14px 18px;
  }
  .strikes-title { font-size: 13px; color: var(--muted); letter-spacing: 1px; margin-bottom: 10px; }
  .strike-row { display: flex; justify-content: space-between; margin-bottom: 7px; font-size: 12px; }
  .strike-row .slabel { color: var(--muted); }
  .strike-row .svalue { font-weight: bold; }

  .sig-history-card {
    flex: 1; min-width: 200px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 6px; padding: 14px 18px;
    max-height: 200px; overflow-y: auto;
  }
  .sig-history-title { font-size: 13px; color: var(--muted); letter-spacing: 1px; margin-bottom: 10px; }
  .sig-history-item  { display: flex; gap: 10px; margin-bottom: 5px; font-size: 11px; }
  .sig-history-time  { color: var(--muted); flex-shrink: 0; }
  .sig-history-name  { font-weight: bold; }

  footer {
    text-align: center; color: var(--muted); font-size: 10px;
    padding: 12px; border-top: 1px solid var(--border);
  }

  .ladder-toggle {
    background: var(--border); border: 1px solid #444; color: var(--muted);
    padding: 3px 10px; border-radius: 3px; cursor: pointer; font-family: inherit;
    font-size: 11px; margin-left: 4px;
  }
  .ladder-toggle.active { background: #1a2e1a; color: var(--green); border-color: var(--green); }
</style>
</head>
<body>

<header>
  <h1>&#9650; SPX GEX</h1>
  <div class="header-right">
    <div id="status-dot"></div>
    <span id="last-update">--</span>
    <div id="regime-badge" class="badge-neutral">NEUTRAL</div>
    <button id="refresh-btn" onclick="manualRefresh()">&#8635; Refresh</button>
  </div>
</header>

<div id="error-banner" class="error-banner"></div>

<!-- KPI row -->
<div class="kpi-row">
  <div class="kpi-card">
    <div class="kpi-label">SPX SPOT</div>
    <div class="kpi-value" id="kpi-spot">--</div>
    <div class="kpi-sub">current price</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">NET GEX</div>
    <div class="kpi-value" id="kpi-gex">--</div>
    <div class="kpi-sub">dealer gamma exposure</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">ATM IV</div>
    <div class="kpi-value" id="kpi-iv">--</div>
    <div class="kpi-sub">implied volatility</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">1-SIGMA 1D</div>
    <div class="kpi-value" id="kpi-move1d">--</div>
    <div class="kpi-sub">expected daily range</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">1-SIGMA 5D</div>
    <div class="kpi-value" id="kpi-move5d">--</div>
    <div class="kpi-sub">5-day expected range</div>
  </div>
</div>

<!-- Key levels -->
<div class="levels-row">
  <div class="level-card" style="border-color:#005577">
    <div class="level-label">CALL WALL</div>
    <div class="level-value" id="lvl-call-wall" style="color:var(--green)">--</div>
  </div>
  <div class="level-card" style="border-color:#552200">
    <div class="level-label">PUT WALL</div>
    <div class="level-value" id="lvl-put-wall" style="color:var(--put)">--</div>
  </div>
  <div class="level-card" style="border-color:#333">
    <div class="level-label">ZERO GAMMA</div>
    <div class="level-value" id="lvl-zero-gamma" style="color:var(--yellow)">--</div>
  </div>
  <div class="level-card" style="border-color:#333">
    <div class="level-label">RANGE HIGH (1d)</div>
    <div class="level-value" id="lvl-range-high" style="color:var(--green)">--</div>
  </div>
  <div class="level-card" style="border-color:#333">
    <div class="level-label">RANGE LOW (1d)</div>
    <div class="level-value" id="lvl-range-low" style="color:var(--red)">--</div>
  </div>
</div>

<!-- Charts -->
<div class="charts-row">
  <div class="chart-card" style="flex:1; min-width:280px;">
    <div class="chart-title">INTRADAY SPOT PRICE</div>
    <div class="chart-wrap"><canvas id="spot-line-chart"></canvas></div>
  </div>
  <div class="chart-card" style="flex:1; min-width:280px;">
    <div class="chart-title" style="display:flex; justify-content:space-between; align-items:center;">
      <span>GEX LADDER  (call ▶ right, put ◀ left)</span>
      <span>
        <button id="ladder-oi-btn"  class="ladder-toggle active">OI</button>
        <button id="ladder-vol-btn" class="ladder-toggle">VOL</button>
      </span>
    </div>
    <div class="chart-wrap"><canvas id="gex-ladder-chart"></canvas></div>
  </div>
</div>

<!-- Butterfly Signal Overlay -->
<div class="signal-section">
  <div class="signal-section-title">BUTTERFLY SPREAD SIGNAL</div>
  <div class="signal-body">

    <!-- Setup signal -->
    <div class="signal-card" id="sig-card">
      <div class="signal-label">CURRENT SETUP</div>
      <div class="signal-name" id="sig-name">--</div>
      <div class="signal-desc" id="sig-desc">Waiting for data...</div>
    </div>

    <!-- Suggested strikes -->
    <div class="strikes-card">
      <div class="strikes-title" id="strikes-title">SUGGESTED STRIKES</div>
      <div id="strikes-items"></div>
    </div>

    <!-- Entry checklist -->
    <div class="checklist-card">
      <div class="checklist-title" id="checklist-title">ENTRY CHECKLIST</div>
      <div id="checklist-items"></div>
    </div>

    <!-- Signal history -->
    <div class="sig-history-card">
      <div class="sig-history-title">SIGNAL HISTORY</div>
      <div id="sig-history-list"></div>
    </div>

  </div>
</div>

<footer>
  SPX GEX Dashboard &bull; Updates every 60s during market hours
</footer>

<script>
const FMT_NUM  = v => v == null ? '--' : v.toLocaleString('en-US', {maximumFractionDigits: 0});
const FMT_FRAC = (v,d=2) => v == null ? '--' : v.toFixed(d);
const FMT_PCT  = v => v == null ? '--' : (v*100).toFixed(1)+'%';
const FMT_GEX  = v => {
  if (v == null) return '--';
  const b = v;
  if (Math.abs(b) >= 1) return (b >= 0 ? '+' : '') + b.toFixed(2) + 'B';
  return (b >= 0 ? '+' : '') + (b * 1000).toFixed(1) + 'M';
};

let spotChart   = null;
let ladderChart = null;
let autoTimer   = null;
let ladderMode  = 'oi';   // 'oi' | 'vol'
let lastSnap    = null;

const FMT_GEX_AXIS = v => {
  if (v == null) return '';
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v/1e9).toFixed(1) + 'B';
  if (abs >= 1e6) return (v/1e6).toFixed(0) + 'M';
  return (v/1e3).toFixed(0) + 'K';
};

function _buildLevelAnnotations(snap) {
  if (!snap) return {};
  const mkLine = (y, color, dash) => ({
    type: 'line', yMin: y, yMax: y,
    borderColor: color, borderWidth: dash ? 1 : 1.5,
    borderDash: dash || [],
  });
  return {
    spotLine:      mkLine(snap.spot,       'rgba(255,255,255,0.85)', null),
    callWallLine:  mkLine(snap.call_wall,  'rgba(0,200,83,0.75)',    [5,4]),
    putWallLine:   mkLine(snap.put_wall,   'rgba(255,87,34,0.75)',   [5,4]),
    zeroGammaLine: mkLine(snap.zero_gamma, 'rgba(255,214,0,0.75)',   [5,4]),
  };
}

function initCharts() {
  // ── Intraday spot price ──────────────────────────────────────────────────
  spotChart = new Chart(
    document.getElementById('spot-line-chart').getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [{
      label: 'Spot', data: [],
      borderColor: 'rgba(0,200,255,0.9)', borderWidth: 1.5,
      pointRadius: 0, fill: false,
    }]},
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#555', maxTicksLimit: 6, font: { size: 9 } }, grid: { color: '#1a1a1a' } },
        y: { position: 'right',
             ticks: { color: '#666', font: { size: 9 },
               callback: v => v.toLocaleString('en-US', {maximumFractionDigits: 0}) },
             grid: { color: '#1a1a1a' } },
      }
    }
  });

  // ── GEX ladder histogram ─────────────────────────────────────────────────
  ladderChart = new Chart(
    document.getElementById('gex-ladder-chart').getContext('2d'), {
    type: 'bar',
    data: { labels: [], datasets: [
      { label: 'Call GEX', data: [], backgroundColor: 'rgba(0,200,83,0.55)',
        borderColor: 'rgba(0,200,83,0.8)', borderWidth: 0.5 },
      { label: 'Put GEX',  data: [], backgroundColor: 'rgba(255,87,34,0.55)',
        borderColor: 'rgba(255,87,34,0.8)', borderWidth: 0.5 },
    ]},
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        annotation: { annotations: {} },
      },
      scales: {
        x: {
          stacked: false,
          ticks: { color: '#555', font: { size: 9 }, callback: FMT_GEX_AXIS },
          grid:  { color: '#222' },
        },
        y: {
          stacked: false,
          ticks: { color: '#666', font: { size: 9 } },
          grid:  { color: '#1a1a1a' },
        },
      }
    }
  });

  // Toggle buttons
  document.getElementById('ladder-oi-btn').onclick  = () => setLadderMode('oi');
  document.getElementById('ladder-vol-btn').onclick = () => setLadderMode('vol');
}

function setLadderMode(mode) {
  ladderMode = mode;
  document.getElementById('ladder-oi-btn').classList.toggle('active',  mode === 'oi');
  document.getElementById('ladder-vol-btn').classList.toggle('active', mode === 'vol');
  if (lastSnap) updateCharts(lastSnap);
}

function updateCharts(snap) {
  if (!snap || !snap.by_strike) return;

  // ── Spot price history ───────────────────────────────────────────────────
  const hist = snap.history || [];
  spotChart.data.labels            = hist.map(h => h.timestamp.substring(11, 16));
  spotChart.data.datasets[0].data  = hist.map(h => h.spot);
  spotChart.update('none');

  // ── GEX ladder — ±150 pts of spot, sorted descending (high strike at top) ─
  const spot = snap.spot || 0;
  const strikes = snap.by_strike
    .filter(s => Math.abs(s.strike - spot) <= 150)
    .sort((a, b) => b.strike - a.strike);

  const callKey = ladderMode === 'vol' ? 'call_vol_gex' : 'call_gex';
  const putKey  = ladderMode === 'vol' ? 'put_vol_gex'  : 'put_gex';

  ladderChart.data.labels            = strikes.map(s => s.strike);
  ladderChart.data.datasets[0].data  = strikes.map(s =>  s[callKey]);   // positive → right
  ladderChart.data.datasets[1].data  = strikes.map(s => -s[putKey]);    // negative → left

  ladderChart.options.plugins.annotation.annotations = _buildLevelAnnotations(snap);
  ladderChart.update('none');
}

function updateKpis(snap) {
  if (!snap) return;

  document.getElementById('kpi-spot').textContent   = FMT_NUM(snap.spot);
  document.getElementById('kpi-gex').textContent    = FMT_GEX(snap.total_net_gex_b);
  document.getElementById('kpi-iv').textContent     = FMT_PCT(snap.atm_iv);
  document.getElementById('kpi-move1d').textContent = snap.exp_move_1d ? '+/-'+FMT_NUM(snap.exp_move_1d) : '--';
  document.getElementById('kpi-move5d').textContent = snap.exp_move_5d ? '+/-'+FMT_NUM(snap.exp_move_5d) : '--';

  document.getElementById('lvl-call-wall').textContent  = FMT_NUM(snap.call_wall);
  document.getElementById('lvl-put-wall').textContent   = FMT_NUM(snap.put_wall);
  document.getElementById('lvl-zero-gamma').textContent = FMT_NUM(snap.zero_gamma);

  if (snap.spot && snap.exp_move_1d) {
    document.getElementById('lvl-range-high').textContent = FMT_NUM(snap.spot + snap.exp_move_1d);
    document.getElementById('lvl-range-low').textContent  = FMT_NUM(snap.spot - snap.exp_move_1d);
  }

  // Regime badge
  const badge = document.getElementById('regime-badge');
  badge.className = 'badge-' + (snap.gex_regime || 'neutral');
  badge.textContent = (snap.gex_regime || 'neutral').toUpperCase();

  // Color the net GEX value
  const gexEl = document.getElementById('kpi-gex');
  gexEl.style.color = snap.total_net_gex_b >= 0 ? 'var(--green)' : 'var(--red)';

  // Last update
  if (snap.timestamp) {
    const t = snap.timestamp.substring(11, 19);
    document.getElementById('last-update').textContent = t + ' ET';
  }

  // Error banner
  const banner = document.getElementById('error-banner');
  if (snap.error) {
    banner.textContent = 'Error: ' + snap.error;
    banner.style.display = 'block';
  } else {
    banner.style.display = 'none';
  }
}

// ---------------------------------------------------------------------------
// Butterfly signal logic
// ---------------------------------------------------------------------------
let _lastSignalName = null;
const _signalHistory = [];

function computeSignal(snap) {
  const spot    = snap.spot            || 0;
  const callWall = snap.call_wall      || 0;
  const putWall  = snap.put_wall       || 0;
  const zeroGamma = snap.zero_gamma    || 0;
  const regime   = snap.gex_regime     || 'neutral';
  const iv       = snap.atm_iv         || 0;
  const netGex   = snap.total_net_gex_b || 0;
  const W        = 5;  // spread width in points

  const toCall       = Math.abs(spot - callWall);
  const toPut        = Math.abs(spot - putWall);
  const nearWall     = Math.min(toCall, toPut) < 50;
  const strongPos    = netGex > 0.1;

  let name, desc, cssClass, checks, strikes;

  // ── Zone 5: Negative gamma — hard avoid / consider directional ────────────
  if (regime === 'negative') {
    name     = 'AVOID — NEGATIVE GAMMA';
    desc     = 'Net GEX is negative (' + FMT_GEX(netGex) + '). Dealers are short gamma and amplifying every move — they buy rallies and sell declines, making trends persist. Butterfly and credit spread strategies carry high risk here. If trading, consider a directional debit spread in the direction of the prevailing trend instead.';
    cssClass = 'avoid';
    checks   = [
      { label: 'GEX regime positive',                                      pass: false },
      { label: 'Spot above Zero Gamma (' + FMT_NUM(zeroGamma) + ')',       pass: spot > zeroGamma },
      { label: 'Net GEX > +0.1B — walls sticky (' + FMT_GEX(netGex) + ')', pass: false },
      { label: 'ATM IV elevated — good for debit buys (' + FMT_PCT(iv) + ')', pass: iv > 0.10 },
      { label: 'Trend direction clear for directional spread',              pass: null },
    ];
    strikes = null;

  // ── Zone 4: Transitional — below Zero Gamma, walls may lose stickiness ───
  } else if (spot < zeroGamma) {
    const distToReclaim = Math.round(zeroGamma - spot);
    name     = 'WARNING — BELOW ZERO GAMMA';
    desc     = 'SPX (' + FMT_NUM(spot) + ') has crossed below Zero Gamma (' + FMT_NUM(zeroGamma) + '). Dealer hedging is shifting from stabilizing to amplifying. The walls become less reliable as pin targets. Hold off on new entries and monitor for a reclaim of ' + FMT_NUM(zeroGamma) + ' (' + distToReclaim + ' pts away).';
    cssClass = 'avoid';
    checks   = [
      { label: 'Spot above Zero Gamma — BREACHED (' + FMT_NUM(spot) + ' vs ' + FMT_NUM(zeroGamma) + ')', pass: false },
      { label: 'Net GEX still positive (' + FMT_GEX(netGex) + ')',         pass: netGex > 0 },
      { label: 'Distance to reclaim Zero Gamma: ' + distToReclaim + ' pts', pass: null },
      { label: 'No new butterfly entries until Zero Gamma reclaimed',        pass: false },
      { label: 'Hedge or reduce existing positions',                          pass: null },
    ];
    strikes = null;

  // ── Zone 1: Price above Call Wall ────────────────────────────────────────
  } else if (spot > callWall) {
    name     = 'CALL SIDE';
    desc     = 'SPX (' + FMT_NUM(spot) + ') is above the Call Wall (' + FMT_NUM(callWall) + '). Dealers are selling into this rally to stay delta-neutral — the Call Wall is structural resistance working against price. Open Leg 1: sell a call credit spread with the short strike AT the Call Wall. Wait for price to pull back toward ' + FMT_NUM(callWall) + ' to complete the butterfly with a debit spread below.';
    cssClass = 'call-side';
    checks   = [
      { label: 'GEX regime positive',                                       pass: regime === 'positive' },
      { label: 'Spot above Zero Gamma (' + FMT_NUM(zeroGamma) + ')',        pass: true },
      { label: 'Spot confirmed above Call Wall (' + FMT_NUM(callWall) + ')', pass: true },
      { label: 'Net GEX > +0.1B — wall is sticky (' + FMT_GEX(netGex) + ')', pass: strongPos },
      { label: 'ATM IV > 10% — enough premium (' + FMT_PCT(iv) + ')',       pass: iv > 0.10 },
    ];
    strikes = {
      title: 'LEG 1 — CALL CREDIT SPREAD ($5 WIDE)',
      rows: [
        { label: 'Pin target (middle)',               value: FMT_NUM(callWall) },
        { label: 'Sell call at (short)',               value: FMT_NUM(callWall) },
        { label: 'Buy call at (long / protection)',    value: FMT_NUM(callWall + W) },
        { label: 'Spread width',                       value: W + ' pts' },
        { label: 'Add Leg 2 when price reaches',       value: FMT_NUM(callWall) },
      ]
    };

  // ── Zone 2: Price below Put Wall ─────────────────────────────────────────
  } else if (spot < putWall) {
    name     = 'PUT SIDE';
    desc     = 'SPX (' + FMT_NUM(spot) + ') is below the Put Wall (' + FMT_NUM(putWall) + '). Dealers are buying stock to stay delta-neutral — the Put Wall is structural support working in your favor. Open Leg 1: sell a put credit spread with the short strike AT the Put Wall. Wait for price to bounce back toward ' + FMT_NUM(putWall) + ' to complete the butterfly with a debit spread above.';
    cssClass = 'put-side';
    checks   = [
      { label: 'GEX regime positive',                                       pass: regime === 'positive' },
      { label: 'Spot above Zero Gamma (' + FMT_NUM(zeroGamma) + ')',        pass: true },
      { label: 'Spot confirmed below Put Wall (' + FMT_NUM(putWall) + ')',   pass: true },
      { label: 'Net GEX > +0.1B — wall is sticky (' + FMT_GEX(netGex) + ')', pass: strongPos },
      { label: 'ATM IV > 10% — enough premium (' + FMT_PCT(iv) + ')',       pass: iv > 0.10 },
    ];
    strikes = {
      title: 'LEG 1 — PUT CREDIT SPREAD ($5 WIDE)',
      rows: [
        { label: 'Pin target (middle)',               value: FMT_NUM(putWall) },
        { label: 'Sell put at (short)',                value: FMT_NUM(putWall) },
        { label: 'Buy put at (long / protection)',     value: FMT_NUM(putWall - W) },
        { label: 'Spread width',                       value: W + ' pts' },
        { label: 'Add Leg 2 when price reaches',       value: FMT_NUM(putWall) },
      ]
    };

  // ── Between walls / near wall: unified directional signal ───────────────
  // Closer to Call Wall → sell PUT credit spread, short at Put Wall, target = Call Wall
  // Closer to Put Wall → sell CALL credit spread, short at Call Wall, target = Put Wall
  } else {
    if (toCall <= toPut) {
      const nearNote = toCall < 50 ? ' — ' + Math.round(toCall) + ' PTS FROM WALL' : '';
      name     = 'PUT SPREAD — CALL WALL PIN' + nearNote;
      desc     = 'SPX (' + FMT_NUM(spot) + ') is between walls and closer to the Call Wall (' + FMT_NUM(callWall) + '). In positive gamma, price is magnetically drawn toward the Call Wall. Open Leg 1 now: sell a put credit spread with the short strike at the Put Wall (' + FMT_NUM(putWall) + ') — the structural GEX support floor that price is unlikely to break. Close for profit (or add Leg 2 call debit spread) when price reaches ' + FMT_NUM(callWall) + '.';
      cssClass = 'put-side';
      checks   = [
        { label: 'GEX regime positive',                                           pass: regime === 'positive' },
        { label: 'Spot above Zero Gamma (' + FMT_NUM(zeroGamma) + ')',            pass: true },
        { label: 'Call Wall (' + FMT_NUM(callWall) + ') is the pin target',       pass: null },
        { label: 'Net GEX > +0.1B — walls sticky (' + FMT_GEX(netGex) + ')',     pass: strongPos },
        { label: 'ATM IV > 10% — enough premium (' + FMT_PCT(iv) + ')',           pass: iv > 0.10 },
      ];
      strikes = {
        title: 'LEG 1 — PUT CREDIT SPREAD ($5 WIDE)',
        rows: [
          { label: 'Pin target / close trigger',     value: FMT_NUM(callWall) },
          { label: 'Sell put at (short)',             value: FMT_NUM(putWall) },
          { label: 'Buy put at (long / protection)', value: FMT_NUM(putWall - W) },
          { label: 'Spread width',                    value: W + ' pts' },
          { label: 'Add Leg 2 when price reaches',    value: FMT_NUM(callWall) },
        ]
      };
    } else {
      const nearNote = toPut < 50 ? ' — ' + Math.round(toPut) + ' PTS FROM WALL' : '';
      name     = 'CALL SPREAD — PUT WALL PIN' + nearNote;
      desc     = 'SPX (' + FMT_NUM(spot) + ') is between walls and closer to the Put Wall (' + FMT_NUM(putWall) + '). Dealer gamma is pulling price toward the Put Wall. Open Leg 1 now: sell a call credit spread with the short strike at the Call Wall (' + FMT_NUM(callWall) + ') — the structural GEX resistance ceiling that price is unlikely to reclaim. Close for profit (or add Leg 2 put debit spread) when price reaches ' + FMT_NUM(putWall) + '.';
      cssClass = 'call-side';
      checks   = [
        { label: 'GEX regime positive',                                           pass: regime === 'positive' },
        { label: 'Spot above Zero Gamma (' + FMT_NUM(zeroGamma) + ')',            pass: true },
        { label: 'Put Wall (' + FMT_NUM(putWall) + ') is the pin target',         pass: null },
        { label: 'Net GEX > +0.1B — walls sticky (' + FMT_GEX(netGex) + ')',     pass: strongPos },
        { label: 'ATM IV > 10% — enough premium (' + FMT_PCT(iv) + ')',           pass: iv > 0.10 },
      ];
      strikes = {
        title: 'LEG 1 — CALL CREDIT SPREAD ($5 WIDE)',
        rows: [
          { label: 'Pin target / close trigger',     value: FMT_NUM(putWall) },
          { label: 'Sell call at (short)',            value: FMT_NUM(callWall) },
          { label: 'Buy call at (long / protection)', value: FMT_NUM(callWall + W) },
          { label: 'Spread width',                    value: W + ' pts' },
          { label: 'Add Leg 2 when price reaches',    value: FMT_NUM(putWall) },
        ]
      };
    }
  }

  return { name, desc, cssClass, checks, strikes };
}

function updateSignal(snap) {
  const sig = computeSignal(snap);

  // Signal card
  const card = document.getElementById('sig-card');
  card.className = 'signal-card ' + sig.cssClass;
  document.getElementById('sig-name').textContent = sig.name;
  document.getElementById('sig-desc').textContent = sig.desc;

  const nameEl = document.getElementById('sig-name');
  if      (sig.cssClass === 'call-side') nameEl.style.color = 'var(--green)';
  else if (sig.cssClass === 'put-side')  nameEl.style.color = 'var(--put)';
  else if (sig.cssClass === 'between')   nameEl.style.color = 'var(--yellow)';
  else                                   nameEl.style.color = 'var(--muted)';

  // Checklist — rendered dynamically per zone
  // pass: true = green, false = red, null = yellow (informational)
  document.getElementById('checklist-items').innerHTML = sig.checks.map(c => {
    const cls = c.pass === true ? 'pass' : c.pass === false ? 'fail' : 'warn';
    return '<div class="check-item"><div class="check-dot ' + cls + '"></div><span>' + c.label + '</span></div>';
  }).join('');

  // Strikes card — rendered dynamically per zone
  if (sig.strikes) {
    document.getElementById('strikes-title').textContent = sig.strikes.title;
    document.getElementById('strikes-items').innerHTML = sig.strikes.rows.map(r =>
      '<div class="strike-row"><span class="slabel">' + r.label + '</span><span class="svalue">' + r.value + '</span></div>'
    ).join('');
  } else {
    document.getElementById('strikes-title').textContent = 'SUGGESTED STRIKES';
    document.getElementById('strikes-items').innerHTML =
      '<div style="color:var(--muted);font-size:12px;margin-top:8px;">No entry — conditions do not support a spread right now.</div>';
  }

  // Signal history — log when signal name changes
  if (sig.name !== _lastSignalName) {
    _lastSignalName = sig.name;
    const time = snap.timestamp ? snap.timestamp.substring(11, 16) : '--';
    _signalHistory.unshift({ time, name: sig.name, cssClass: sig.cssClass });
    if (_signalHistory.length > 20) _signalHistory.pop();

    const list = document.getElementById('sig-history-list');
    list.innerHTML = _signalHistory.map(h => {
      let color = '#888';
      if (h.cssClass === 'call-side') color = 'var(--green)';
      if (h.cssClass === 'put-side')  color = 'var(--put)';
      if (h.cssClass === 'between')   color = 'var(--yellow)';
      return '<div class="sig-history-item"><span class="sig-history-time">' + h.time + '</span><span class="sig-history-name" style="color:' + color + '">' + h.name + '</span></div>';
    }).join('');
  }
}

async function fetchGex() {
  try {
    const resp = await fetch('/api/gex');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const snap = await resp.json();
    lastSnap = snap;
    updateKpis(snap);
    updateCharts(snap);
    updateSignal(snap);
    document.getElementById('status-dot').className = 'live';
  } catch(e) {
    console.error('GEX fetch error:', e);
    document.getElementById('status-dot').className = '';
  }
}

async function manualRefresh() {
  const btn = document.getElementById('refresh-btn');
  btn.disabled = true;
  btn.textContent = '...';
  try {
    await fetch('/api/refresh', { method: 'POST' });
    await new Promise(r => setTimeout(r, 3000));
    await fetchGex();
  } finally {
    btn.disabled = false;
    btn.textContent = '⟳ Refresh';
  }
}

function startAutoRefresh() {
  if (autoTimer) clearInterval(autoTimer);
  autoTimer = setInterval(fetchGex, 60000);
}

// Init
initCharts();
fetchGex();
startAutoRefresh();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _args

    parser = argparse.ArgumentParser(description="SPX GEX Dashboard")
    parser.add_argument("--symbol",   default="$SPX", help="Option chain symbol (default: $SPX)")
    parser.add_argument("--dte-max",  default=30, type=int, help="Max DTE to include (default: 30)")
    parser.add_argument("--port",     default=5556, type=int, help="Dashboard port (default: 5556)")
    parser.add_argument("--host",     default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--dry-run",  action="store_true", help="Use fake data (no API calls)")
    parser.add_argument("--provider", default="schwab", choices=["schwab", "tastytrade"],
                        help="Market data provider (default: schwab)")
    _args = parser.parse_args()

    if _args.dry_run:
        logger.info("[app] DRY-RUN mode -- fake GEX data")
    else:
        logger.info("[app] Provider: %s", _args.provider)

    # Clean up old history files
    _prune_old_history()

    # Start background GEX updater
    bg = threading.Thread(target=_background_loop, daemon=True, name="gex-updater")
    bg.start()

    logger.info("[app] SPX GEX Dashboard starting at http://%s:%d", _args.host, _args.port)
    logger.info("[app] Provider: %s  Symbol: %s  DTE max: %d  Auto-refresh: 60s during market hours",
                _args.provider, _args.symbol, _args.dte_max)

    app.run(host=_args.host, port=_args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
