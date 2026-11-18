"""BTC Oracle Pro — public tracking API (paper only, never live trades).

Tracks the live Kalshi 15-minute BTC, ETH, gold, SOL, XRP, DOGE, BNB
and silver windows
and serves them as public JSON: current window, spot price, computed
target, sigma, signals, and the win rate counted from settled signals.
Also watches the daily KXGOLD market (live spot + the open daily market,
no gold signal in v1). Gold 15-minute (KXGOLD15M) is tracked like BTC/ETH
— window, target, sigma, live book, key levels — but with NO signal, NO
track-record writes and NO ledger: a gold signal needs its own strategy
and validation run. SOL, XRP and DOGE 15-minute (KXSOL15M, KXXRP15M,
KXDOGE15M, added 2026-11-17) are feed-only the same way — no signal, no
track-record writes, no ledger. BNB and silver 15-minute (KXBNB15M,
KXSILVER15M, added 2026-11-17) are feed-only the same way — no signal,
no track-record writes, no ledger. GET /top-markets ranks the most-traded
Kalshi markets by public 24h volume (display only, no signal).

Data reality (verified Nov 16, 2026; corrected Nov 18, 2026):
- Kalshi's public REST API returns the live window (ticker, close time).
  The per-market orderbook endpoint DOES return live bids in `orderbook_fp`
  (yes_dollars/no_dollars bid ladders; YES ask = 1 - NO bid). The
  earlier "orderbooks are empty" finding read the legacy `yes`/`no`
  keys Kalshi removed on 2026-03-12.
- CORRECTION (verified keyless 2026-11-18, single spaced calls): the
  /markets rows are NOT null snapshots — the live KXBTC15M row carries
  volume_24h_fp, volume_fp, open_interest_fp, last_price_dollars, YES/NO
  bid+ask dollars and sizes, and floor_strike (the market's own strike).
  The public tape endpoint GET /markets/trades?ticker=... ALSO works
  keyless (HTTP 200). Settled rows add result, expiration_value (the
  BRTI settlement average) and settlement_ts. previous_price_dollars
  exists but read '0.0000' on every sample. See parse_kalshi_market_extras
  / record_kalshi_window_extras: display/diagnostics only, never signal.
- Target price = the 60-second average of spot in the minute before the
  window opens. That is how the market's strike is defined. If the
  service starts mid-window and missed that minute, the target is marked
  provisional (built from the earliest samples in the window) and flagged.
- Resolution is judged from the final-minute spot average vs the target.
  That is an approximation of the real settlement rule, which uses the
  CF Benchmarks BRTI average — the response says so.
- Signals are v1 and UNVALIDATED. Nothing here is a validated strategy
  until it passes 100+ paper trades plus out-of-sample validation.
  No money ledger lives here: without live Kalshi prices there is no
  executable entry price, so this API tracks signal correctness
  (direction vs resolution), not dollars.
- Gold (v1): live spot from a public feed + the open KXGOLD market
  when one exists. No gold signal — the market's own YES price is the
  market's view.
- Gold 15-minute (KXGOLD15M, added 2026-11-17): tracked like BTC/ETH
  (window, target, sigma, book, levels) but with NO signal and NO
  track-record writes. The daily KXGOLD feed is unchanged; the two
  gold series coexist.
- SOL / XRP / DOGE 15-minute (KXSOL15M, KXXRP15M, KXDOGE15M, added
  2026-11-17): feed-only, tracked exactly like gold 15-minute — live
  window, 60s pre-open target, 1-minute sigma, live book (polled
  ~60s), key levels scaled to each coin (SOL ~$74, XRP ~$1, DOGE
  ~$0.06 at time of writing). NO signal, NO track-record writes, NO
  ledger — a signal for any of them needs its own strategy and
  validation run. Dollar figures are rounded to decimals that fit the
  price scale (6 for sub-$1 coins) so sigma and spot-vs-target never
  read 0.0.
- BNB / silver 15-minute (added 2026-11-17): feed-only, tracked
  exactly like gold 15-minute — live window, 60s pre-open target,
  1-minute sigma, live book (polled ~60s), key levels scaled to each
  market (BNB ~$592, silver ~$47 at time of writing). NO signal, NO
  track-record writes, NO ledger — a signal for either needs its own
  strategy and validation run. Spot feeds are free and public:
  Coinbase BNB-USD for BNB, api.gold-api.com/price/XAG for silver.
  NEAR, HYPE, ZEC, ADA, BCH and TON 15-minute markets exist on Kalshi
  but are NOT tracked: their books are too thin (HYPE showed $0
  volume, NEAR $659 in the owner's screenshots) and each new tab adds
  validation debt. A market earns its own tab when its volume + a
  validation run justify it; the rest stay visible on Top Markets.
- Top Markets (GET /top-markets, added 2026-11-17): the most-traded
  Kalshi markets by public 24h volume (volume_24h_fp in /markets —
  verified Nov 17, 2026), ranked across the tracked liquid series,
  plus biggest movers since the last refresh. Display only, no signal,
  no advice. The unfiltered /markets list cannot be ranked in one call:
  it is flooded with 40,000+ zero-volume combo markets, so the ranking
  covers the named series pool in the response, not the whole exchange.
- Extra spot sources (added 2026-11-18, piece 2 — display/diagnostic
  only, never an input to the signal or the track record): Binance.US
  BTCUSD spot (api.binance.us/api/v3/ticker/price?symbol=BTCUSD,
  public, no key, polled every 60s, stored in
  binanceus_spot_samples) and the Kraken BTC mid-price, derived from
  the Kraken depth feed the viewer already polls every 15s
  (api.kraken.com/0/public/Depth) — the server never fetches Kraken,
  so no new Kraken request exists anywhere. A cross-exchange
  consensus check compares Coinbase spot vs Kraken mid vs Binance.US
  spot and flags a >0.20% spread as a diagnostic only.
- Durable history (added 2026-11-17): the SQLite history (windows,
  signals, spot samples) lives in a Turso free database when
  TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are set, and in the local
  oracle.db file when they are not. Render free Postgres was
  deliberately not used — free Postgres databases are deleted after
  ~90 days. See README.md for the env vars and the row budget.
- Accounts + VIP gate (added 2026-11-17): email + password accounts
  (auth.py, stdlib scrypt) and a VIP flag gated by the 100-window
  validation bar. Free tier: BTC + ETH live windows, signals,
  watchlist, sparklines, daily results note. VIP additionally: timely
  callouts + the full served markets. Stripe Subscriptions is the
  payment rail (billing.py webhook only — no product, no price, no
  checkout in this build). The web viewer is served at /app from this
  same service. The news-veto pop-up was dropped, not built.

Run:  uvicorn tracker:app --host 0.0.0.0 --port 8000
Endpoints: /live, /live/all, /signals, /track-record, /entries, /windows, /top-markets, /health
"""

import json
import math
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import auth  # accounts + VIP gate (added 2026-11-17); auth never imports tracker
import billing  # Stripe Subscriptions webhook (added 2026-11-17)

DB_PATH = "oracle.db"
# Durable history (added 2026-11-17): by default the tracker still uses
# the local SQLite file above, exactly as before. When
# TURSO_DATABASE_URL is set, history lives in a Turso (libSQL) database
# instead, so a free-tier restart no longer wipes the track record,
# entry log, spot samples and Key Levels history. TURSO_AUTH_TOKEN must
# be set with it; a URL without a token fails loudly at import — it must
# never silently fall back to the local file, or a restart would
# silently wipe history again. Render free Postgres was deliberately
# NOT used: free Postgres databases are deleted after ~90 days.
# The env vars are set in Render (dashboard -> service -> Environment);
# their values come from the Turso dashboard, never from this repo.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
if TURSO_DATABASE_URL and not TURSO_AUTH_TOKEN:
    raise RuntimeError(
        "TURSO_DATABASE_URL is set but TURSO_AUTH_TOKEN is missing; "
        "refusing to start rather than silently falling back to local SQLite"
    )
_USE_TURSO = bool(TURSO_DATABASE_URL)

# Key Levels refresh cadence (added 2026-11-17): levels are 6-hour
# support/resistance — a 10-minute refresh is more than fresh enough,
# and the old 60s cadence read ~4,320 spot rows per series per minute,
# ~1.5B rows/month across 8 series, 3x Turso's free 500M rows-read cap.
LEVELS_REFRESH_SECS = 600

# Prune bookkeeping (added 2026-11-17): per-tick DELETEs of 24h-old
# samples cost one HTTP round trip per series per tick on Turso. Pruning
# once an hour per table deletes the same rows — no query reads samples
# older than 6 hours — and drops ~108 DELETEs/minute to 8/hour.
_last_prune = {}

# Grade-the-first-call (added 2026-11-18): the track record grades the
# FIRST ENTER per window at minute <= 9 — the call you could act on —
# not the last signal. SIT_OUT_CONFIDENCE_FLOOR is the selectivity
# shadow threshold (first ENTER confidence below it => would_sit_out):
# logging only, never shown, never counted, until the fresh-data gate
# passes and the user-facing alert is approved separately.
SIT_OUT_CONFIDENCE_FLOOR = 70

# Level bounce tracking (added 2026-11-18): after price touches a key
# level, watch it for one 15-minute window, then judge HELD (price back
# on the level's own side: support >= level, resistance <= level) vs
# BROKE (crossed through). Pending touches older than this with no
# decision are marked 'stale', never counted in the hold rate.
LEVEL_BOUNCE_OBSERVE_SECS = 900
LEVEL_BOUNCE_STALE_SECS = 6 * 3600
# Bucket width for grouping a level's hold rate (matches each series'
# cluster radius in its CFG: BTC 25, ETH 10).
LEVEL_BOUNCE_BUCKETS = {"KXBTC15M": 25.0, "KXETH15M": 10.0}
KALSHI_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXBTC15M&limit=5&status=open"
)
KALSHI_ETH_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXETH15M&limit=5&status=open"
)
KALSHI_ORDERBOOK = "https://api.elections.kalshi.com/trade-api/v2/markets"
# Kalshi extras (piece 3, verified keyless 2026-11-18): the public tape
# is GET /markets/trades?ticker={ticker}&limit={n} — NOT
# /markets/{ticker}/trades. One page, newest first, no key, no signature.
KALSHI_TRADES = (
    "https://api.elections.kalshi.com/trade-api/v2/markets/trades"
)
KALSHI_EXTRAS_SERIES = "KXBTC15M"  # settle-time tape fetch: BTC only
KALSHI_GOLD_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXGOLD&limit=10&status=open"
)
KALSHI_GOLD15M_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXGOLD15M&limit=5&status=open"
)
GOLD_SPOT = "https://api.gold-api.com/price/XAU"
COINBASE_SPOT = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINBASE_ETH_SPOT = "https://api.coinbase.com/v2/prices/ETH-USD/spot"
COINBASE_SOL_SPOT = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
COINBASE_XRP_SPOT = "https://api.coinbase.com/v2/prices/XRP-USD/spot"
COINBASE_DOGE_SPOT = "https://api.coinbase.com/v2/prices/DOGE-USD/spot"
COINBASE_BNB_SPOT = "https://api.coinbase.com/v2/prices/BNB-USD/spot"
SILVER_SPOT = "https://api.gold-api.com/price/XAG"
# /candles sources (added 2026-11-17): Coinbase publishes the same free
# 1-minute candles for SOL/XRP/DOGE/BNB as for BTC/ETH. Gold/silver have
# no candle feed anywhere free — gold-api is price-only — so their tape
# is built from this service's own stored spot samples instead.
COINBASE_CANDLE_PRODUCTS = {
    "KXBTC15M": "BTC-USD",
    "KXETH15M": "ETH-USD",
    "KXSOL15M": "SOL-USD",
    "KXXRP15M": "XRP-USD",
    "KXDOGE15M": "DOGE-USD",
    "KXBNB15M": "BNB-USD",
}
SAMPLE_TAPE_TABLES = {
    "KXGOLD15M": "gold_spot_samples",
    "KXGOLD": "gold_spot_samples",
    "KXSILVER15M": "silver_spot_samples",
}
KALSHI_SOL15M_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXSOL15M&limit=5&status=open"
)
KALSHI_XRP15M_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXXRP15M&limit=5&status=open"
)
KALSHI_DOGE15M_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXDOGE15M&limit=5&status=open"
)
KALSHI_BNB15M_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXBNB15M&limit=5&status=open"
)
KALSHI_SILVER15M_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXSILVER15M&limit=5&status=open"
)
# Top Markets pool: the tracked liquid series, ranked by public 24h
# volume. The unfiltered /markets list is flooded with 40,000+
# zero-volume combo markets (verified Nov 17, 2026), so a full-exchange
# ranking is not feasible from one call — the pool is named in the
# /top-markets response.
TOP_MARKETS_SERIES = (
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M",
    "KXBNB15M", "KXGOLD15M", "KXSILVER15M", "KXGOLD", "KXBTCD",
    "KXNFLGAME", "KXNBAGAME", "KXNCAAMBGAME", "KXNHLGAME",
)
TOP_MARKETS_TTL = 300  # seconds between refreshes (14 series fetches)

app = FastAPI(title="BTC Oracle Pro — public tracking API")
# Public read-only API: allow any web page to read it (GET only) so the
# public viewer version of the dashboard can fetch /live straight from
# the browser. Nothing here writes anything, so this opens no door.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ------------------------------------------------------- series config
# One config per tracked 15-minute market. BTC keys are the bare names
# (the original state keys); ETH keys carry the eth_ prefix. The poll
# loop and the /live payload builder run the same code for both.
BTC_CFG = {
    "name": "BTC",
    "series_ticker": "KXBTC15M",
    "question": "BTC price up in next 15 mins?",
    "window_secs": 900,
    "spot_url": COINBASE_SPOT,
    "spot_table": "spot_samples",
    "markets_url": KALSHI_MARKETS,
    "prominence": 5, "cluster": 25, "round_step": 500,
    "keys": {
        "spot": "spot", "spot_ts": "spot_ts", "window": "window",
        "markets_ts": "markets_ts", "target": "target",
        "target_provisional": "target_provisional", "sigma": "sigma_1m",
        "last_signal": "last_signal", "levels": "levels",
        "levels_ts": "levels_ts", "book": "book", "book_ts": "book_ts",
    },
}
ETH_CFG = {
    "name": "ETH",
    "series_ticker": "KXETH15M",
    "question": "ETH price up in next 15 mins?",
    "window_secs": 900,
    "spot_url": COINBASE_ETH_SPOT,
    "spot_table": "eth_spot_samples",
    "markets_url": KALSHI_ETH_MARKETS,
    "prominence": 2, "cluster": 10, "round_step": 100,
    "keys": {
        "spot": "eth_spot", "spot_ts": "eth_spot_ts", "window": "eth_window",
        "markets_ts": "eth_markets_ts", "target": "eth_target",
        "target_provisional": "eth_target_provisional", "sigma": "eth_sigma_1m",
        "last_signal": "eth_last_signal", "levels": "eth_levels",
        "levels_ts": "eth_levels_ts", "book": "eth_book", "book_ts": "eth_book_ts",
    },
}
GOLD15_CFG = {
    # 15-minute gold, added 2026-11-17: Kalshi runs KXGOLD15M alongside
    # the daily KXGOLD series, and the daily feed alone left Pulse's
    # gold tab empty while a 15-minute gold market was live. Same
    # tracking shape as BTC/ETH, but NO signal and NO track-record
    # writes — a gold signal needs its own strategy and validation run.
    "name": "GOLD15",
    "series_ticker": "KXGOLD15M",
    "question": "Gold price up in next 15 mins?",
    "window_secs": 900,
    "spot_url": GOLD_SPOT,
    "spot_kind": "gold",          # gold-api feed, not Coinbase's shape
    "spot_table": "gold_spot_samples",
    "markets_url": KALSHI_GOLD15M_MARKETS,
    "prominence": 2, "cluster": 10, "round_step": 50,
    "signal": False,              # no gold signal
    "track_record": False,        # no windows/signals rows for gold
    "book_secs": 60,              # gentler book polling than BTC/ETH
    "keys": {
        "spot": "g15_spot", "spot_ts": "g15_spot_ts", "window": "g15_window",
        "markets_ts": "g15_markets_ts", "target": "g15_target",
        "target_provisional": "g15_target_provisional", "sigma": "g15_sigma_1m",
        "last_signal": "g15_last_signal", "levels": "g15_levels",
        "levels_ts": "g15_levels_ts", "book": "g15_book", "book_ts": "g15_book_ts",
    },
}
SOL15_CFG = {
    # 15-minute SOL, added 2026-11-17: feed-only, exactly like gold
    # 15-minute — NO signal, NO track-record writes. Spot ~$74 at time
    # of writing (Coinbase SOL-USD, verified live). Levels scaled to
    # the coin: swing prominence $0.10, cluster $0.50, round levels
    # every $5.
    "name": "SOL15",
    "series_ticker": "KXSOL15M",
    "question": "SOL price up in next 15 mins?",
    "window_secs": 900,
    "spot_url": COINBASE_SOL_SPOT,
    "spot_table": "sol_spot_samples",
    "markets_url": KALSHI_SOL15M_MARKETS,
    "prominence": 0.10, "cluster": 0.50, "round_step": 5,
    "signal": False,              # no SOL signal
    "track_record": False,        # no windows/signals rows for SOL
    "book_secs": 60,              # gentler book polling than BTC/ETH
    "keys": {
        "spot": "sol_spot", "spot_ts": "sol_spot_ts", "window": "sol_window",
        "markets_ts": "sol_markets_ts", "target": "sol_target",
        "target_provisional": "sol_target_provisional", "sigma": "sol_sigma_1m",
        "last_signal": "sol_last_signal", "levels": "sol_levels",
        "levels_ts": "sol_levels_ts", "book": "sol_book", "book_ts": "sol_book_ts",
    },
}
XRP15_CFG = {
    # 15-minute XRP, added 2026-11-17: feed-only, exactly like gold
    # 15-minute — NO signal, NO track-record writes. Spot ~$0.97 at
    # time of writing (Coinbase XRP-USD, verified live). Levels scaled
    # to the coin: swing prominence $0.002, cluster $0.01, round levels
    # every $0.10.
    "name": "XRP15",
    "series_ticker": "KXXRP15M",
    "question": "XRP price up in next 15 mins?",
    "window_secs": 900,
    "spot_url": COINBASE_XRP_SPOT,
    "spot_table": "xrp_spot_samples",
    "markets_url": KALSHI_XRP15M_MARKETS,
    "prominence": 0.002, "cluster": 0.01, "round_step": 0.10,
    "signal": False,              # no XRP signal
    "track_record": False,        # no windows/signals rows for XRP
    "book_secs": 60,              # gentler book polling than BTC/ETH
    "keys": {
        "spot": "xrp_spot", "spot_ts": "xrp_spot_ts", "window": "xrp_window",
        "markets_ts": "xrp_markets_ts", "target": "xrp_target",
        "target_provisional": "xrp_target_provisional", "sigma": "xrp_sigma_1m",
        "last_signal": "xrp_last_signal", "levels": "xrp_levels",
        "levels_ts": "xrp_levels_ts", "book": "xrp_book", "book_ts": "xrp_book_ts",
    },
}
DOGE15_CFG = {
    # 15-minute DOGE, added 2026-11-17: feed-only, exactly like gold
    # 15-minute — NO signal, NO track-record writes. Spot ~$0.059 at
    # time of writing (Coinbase DOGE-USD, verified live). Levels scaled
    # to the coin: swing prominence $0.0002, cluster $0.001, round
    # levels every $0.005.
    "name": "DOGE15",
    "series_ticker": "KXDOGE15M",
    "question": "DOGE price up in next 15 mins?",
    "window_secs": 900,
    "spot_url": COINBASE_DOGE_SPOT,
    "spot_table": "doge_spot_samples",
    "markets_url": KALSHI_DOGE15M_MARKETS,
    "prominence": 0.0002, "cluster": 0.001, "round_step": 0.005,
    "signal": False,              # no DOGE signal
    "track_record": False,        # no windows/signals rows for DOGE
    "book_secs": 60,              # gentler book polling than BTC/ETH
    "keys": {
        "spot": "doge_spot", "spot_ts": "doge_spot_ts", "window": "doge_window",
        "markets_ts": "doge_markets_ts", "target": "doge_target",
        "target_provisional": "doge_target_provisional", "sigma": "doge_sigma_1m",
        "last_signal": "doge_last_signal", "levels": "doge_levels",
        "levels_ts": "doge_levels_ts", "book": "doge_book", "book_ts": "doge_book_ts",
    },
}
BNB15_CFG = {
    # 15-minute BNB, added 2026-11-17: feed-only, exactly like gold
    # 15-minute — NO signal, NO track-record writes. Spot ~$592 at
    # time of writing (Coinbase BNB-USD, verified live). Levels scaled
    # to the coin: swing prominence $0.50, cluster $2.50, round levels
    # every $25.
    "name": "BNB15",
    "series_ticker": "KXBNB15M",
    "question": "BNB price up in next 15 mins?",
    "window_secs": 900,
    "spot_url": COINBASE_BNB_SPOT,
    "spot_table": "bnb_spot_samples",
    "markets_url": KALSHI_BNB15M_MARKETS,
    "prominence": 0.50, "cluster": 2.50, "round_step": 25,
    "signal": False,              # no BNB signal
    "track_record": False,        # no windows/signals rows for BNB
    "book_secs": 60,              # gentler book polling than BTC/ETH
    "keys": {
        "spot": "bnb_spot", "spot_ts": "bnb_spot_ts", "window": "bnb_window",
        "markets_ts": "bnb_markets_ts", "target": "bnb_target",
        "target_provisional": "bnb_target_provisional", "sigma": "bnb_sigma_1m",
        "last_signal": "bnb_last_signal", "levels": "bnb_levels",
        "levels_ts": "bnb_levels_ts", "book": "bnb_book", "book_ts": "bnb_book_ts",
    },
}
SILVER15_CFG = {
    # 15-minute silver, added 2026-11-17: feed-only, exactly like gold
    # 15-minute — NO signal, NO track-record writes. Spot ~$47.43 at
    # time of writing (api.gold-api.com/price/XAG, verified live — the
    # same feed family as the gold XAU feed). Levels scaled to the
    # metal: swing prominence $0.02, cluster $0.10, round levels
    # every $1.
    "name": "SILVER15",
    "series_ticker": "KXSILVER15M",
    "question": "Silver price up in next 15 mins?",
    "window_secs": 900,
    "spot_url": SILVER_SPOT,
    "spot_kind": "silver",        # gold-api feed, not Coinbase's shape
    "spot_table": "silver_spot_samples",
    "markets_url": KALSHI_SILVER15M_MARKETS,
    "prominence": 0.02, "cluster": 0.10, "round_step": 1,
    "signal": False,              # no silver signal
    "track_record": False,        # no windows/signals rows for silver
    "book_secs": 60,              # gentler book polling than BTC/ETH
    "keys": {
        "spot": "sil_spot", "spot_ts": "sil_spot_ts", "window": "sil_window",
        "markets_ts": "sil_markets_ts", "target": "sil_target",
        "target_provisional": "sil_target_provisional", "sigma": "sil_sigma_1m",
        "last_signal": "sil_last_signal", "levels": "sil_levels",
        "levels_ts": "sil_levels_ts", "book": "sil_book", "book_ts": "sil_book_ts",
    },
}


# ---------------------------------------------------------------- db
class _Row(tuple):
    """A row that answers both by name and by position.

    libsql (the Turso client) returns plain tuples; the tracker reads
    rows by column name in 8+ places (row["ts"], row["target"], ...) and
    by unpacking in candles_from_samples (for ts, price in rows). This
    subclass carries the column names from cursor.description so both
    styles keep working, whichever backend is in use — local SQLite rows
    are wrapped the same way, so behaviour is identical on both paths.
    """

    def __new__(cls, values, columns):
        obj = super().__new__(cls, tuple(values))
        obj._columns = tuple(columns)
        # Name lookup is case-insensitive: libsql reports the `action`
        # column as ACTION (it is a SQL keyword) while local SQLite
        # reports it as written. The tracker always asks lowercase.
        obj._by_name = {c.lower(): i for i, c in enumerate(columns)}
        return obj

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return super().__getitem__(self._by_name[key.lower()])
            except KeyError:
                raise KeyError(key) from None
        return super().__getitem__(key)

    def keys(self):
        return list(self._columns)


class _Cursor:
    """Wraps a DB-API cursor so fetchone()/fetchall() return _Row."""

    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return _Row(row, [d[0] for d in self._cursor.description])

    def fetchall(self):
        cols = [d[0] for d in self._cursor.description]
        return [_Row(row, cols) for row in self._cursor.fetchall()]


class _Conn:
    """Wraps a DB-API connection: same execute/commit/close surface.

    The Turso path needs no special casing except that PRAGMA
    journal_mode=WAL is a local-file setting — the remote server
    rejects it — so it is applied on the local path only (see _raw_db).
    Everything else passes through untouched.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return _Cursor(self._conn.execute(sql, params))

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()


def _raw_db():
    """Open a raw connection on whichever backend is configured."""
    if _USE_TURSO:
        import libsql  # lazy: a missing package only breaks the Turso path

        return _Conn(libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN))
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        # WAL mode: readers (dashboard/API endpoints) must not block the
        # writer (poll loop). Lock contention froze the API 2026-11-16.
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return _Conn(conn)


_db_ready = False
_db_init_last_try = 0.0


def _first_enter(conn, ticker):
    """First ENTER signal of a window at minute <= 9 — the actionable call.

    One row per window: the lowest-id ENTER logged at minute 9 or
    earlier. Later re-fires and enters after minute 9 are not the call
    anyone could act on, so they never grade the window. Returns the
    row (direction, confidence, minute) or None when the window never
    produced an actionable ENTER.
    """
    return conn.execute(
        "SELECT direction, confidence, minute FROM signals WHERE window = ? "
        "AND action = 'ENTER' AND minute <= 9 ORDER BY id ASC LIMIT 1",
        (ticker,),
    ).fetchone()


def _track_record_counts(conn, series):
    """(settled, correct) for one series, graded on the FIRST call.

    One row per settled window: its FIRST ENTER (minute <= 9) vs how
    the window resolved. A window with no actionable ENTER is not a
    trade and is not counted. The ticker range uses the windows PK
    index; the MIN(id) subquery uses idx_signals_window_action_id.
    Used by the /track-record fallback and the boot regrade — the hot
    path reads the track_record_summary table instead (see
    settle_window).
    """
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN (f.direction = 'UP' "
        "AND w.resolution = 'YES') OR (f.direction = 'DOWN' "
        "AND w.resolution = 'NO') THEN 1 ELSE 0 END) "
        "FROM windows w JOIN signals f ON f.id = (SELECT MIN(id) "
        "FROM signals WHERE window = w.ticker AND action = 'ENTER' "
        "AND minute <= 9) "
        "WHERE w.resolution IS NOT NULL AND w.ticker >= ? AND w.ticker < ?",
        (series, series + "\U0010FFFF"),
    ).fetchone()
    return (row[0] or 0, row[1] or 0)


def _track_record_counts_last_signal(conn, series):
    """(settled, correct) graded on the LAST signal — diagnostic only.

    The pre-2026-11-18 grading rule, kept so the boot regrade can
    store the old rate alongside the first-call rate for comparison.
    It is never the headline and never what validation judges.
    """
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN (s.direction = 'UP' "
        "AND w.resolution = 'YES') OR (s.direction = 'DOWN' "
        "AND w.resolution = 'NO') THEN 1 ELSE 0 END) "
        "FROM windows w JOIN signals s ON s.window = w.ticker "
        "WHERE w.resolution IS NOT NULL AND w.ticker >= ? AND w.ticker < ? "
        "AND s.id = (SELECT MAX(id) FROM signals WHERE window = w.ticker)",
        (series, series + "\U0010FFFF"),
    ).fetchone()
    return (row[0] or 0, row[1] or 0)


_KALSHI_WINDOW_COLUMNS = (
    ("strike_usd", "REAL"),
    ("volume_24h", "REAL"),
    ("open_interest", "REAL"),
    ("last_price_cents", "REAL"),
    ("market_result", "TEXT"),
    ("expiration_value", "REAL"),
)


def _migrate_kalshi_extras(conn):
    """Add the Kalshi-extras columns to a pre-existing windows table.

    Fresh databases get the columns from the CREATE TABLE above; a
    database created before this piece keeps working because each
    ALTER is attempted and duplicate-column errors are ignored.
    Never raises."""
    for name, kind in _KALSHI_WINDOW_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE windows ADD COLUMN {name} {kind}")
        except Exception:
            pass


def _migrate_grading(conn):
    """Add the first-call grading columns to track_record_summary.

    Fresh databases get them from the CREATE TABLE above; a database
    created before this change keeps working because each ALTER is
    attempted and duplicate-column errors are ignored. Existing rows
    keep grading='last' (the default) until _regrade_series recomputes
    them — that is the marker the regrade looks for. Never raises."""
    for sql in (
        "ALTER TABLE track_record_summary ADD COLUMN grading TEXT DEFAULT 'last'",
        "ALTER TABLE track_record_summary ADD COLUMN last_settled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE track_record_summary ADD COLUMN last_correct INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(sql)
        except Exception:
            pass


def _migrate_would_sit_out(conn):
    """Add the selectivity shadow flag column to windows.

    Same pattern as _migrate_kalshi_extras: fresh databases get it
    from the CREATE TABLE above; existing ones get it here. NULL means
    "no first ENTER to judge" (or "not settled yet"). Never raises."""
    try:
        conn.execute("ALTER TABLE windows ADD COLUMN would_sit_out INTEGER")
    except Exception:
        pass


def _regrade_series(conn, series):
    """Recompute one series' summary under first-call grading, once.

    Runs at boot from _init_db_once. If the series has no summary row,
    it is seeded (fresh database: 0/0). If the row still carries
    grading='last' — the pre-2026-11-18 rule — the stored counts are
    recomputed from the stored signals (first ENTER per window, minute
    <= 9) and the old last-signal counts are stored alongside as the
    labeled diagnostic. Raw signal rows are never touched. A row
    already graded 'first' is left alone, so this runs exactly once
    per series per database, ever. Also backfills would_sit_out for
    settled windows missing it (first ENTER confidence < 70).
    Never raises — a failure is logged and the old row stands."""
    try:
        row = conn.execute(
            "SELECT settled, grading FROM track_record_summary WHERE series = ?",
            (series,),
        ).fetchone()
        if row is None:
            settled, correct = _track_record_counts(conn, series)
            last_settled, last_correct = _track_record_counts_last_signal(
                conn, series
            )
            conn.execute(
                "INSERT INTO track_record_summary (series, settled, correct, "
                "last_settled, last_correct, grading) VALUES (?, ?, ?, ?, ?, 'first')",
                (series, settled, correct, last_settled, last_correct),
            )
            needs_backfill = True
        elif (row["grading"] or "last") != "first":
            settled, correct = _track_record_counts(conn, series)
            last_settled, last_correct = _track_record_counts_last_signal(
                conn, series
            )
            conn.execute(
                "UPDATE track_record_summary SET settled = ?, correct = ?, "
                "last_settled = ?, last_correct = ?, grading = 'first' "
                "WHERE series = ?",
                (settled, correct, last_settled, last_correct, series),
            )
            needs_backfill = True
        else:
            needs_backfill = False
        if needs_backfill:
            # Shadow flag backfill (one-time, with the regrade): first
            # ENTER confidence < 70 => would have sat out. Windows with
            # no actionable ENTER keep NULL — no call to judge.
            conn.execute(
                "UPDATE windows SET would_sit_out = (SELECT CASE WHEN "
                "f.confidence < ? THEN 1 ELSE 0 END FROM signals f "
                "WHERE f.id = (SELECT MIN(id) FROM signals WHERE "
                "window = windows.ticker AND action = 'ENTER' "
                "AND minute <= 9)) "
                "WHERE resolution IS NOT NULL AND would_sit_out IS NULL "
                "AND ticker >= ? AND ticker < ?",
                (SIT_OUT_CONFIDENCE_FLOOR, series, series + "\U0010FFFF"),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - logged, old row stands
        print(f"[db] regrade {series} failed: {exc}", flush=True)


def _init_db_once():
    """Create tables + indexes if missing. Returns True on success."""
    try:
        conn = _raw_db()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS spot_samples (ts REAL PRIMARY KEY, price REAL);
            CREATE TABLE IF NOT EXISTS eth_spot_samples (ts REAL PRIMARY KEY, price REAL);
            CREATE TABLE IF NOT EXISTS gold_spot_samples (ts REAL PRIMARY KEY, price REAL);
            CREATE TABLE IF NOT EXISTS sol_spot_samples (ts REAL PRIMARY KEY, price REAL);
            CREATE TABLE IF NOT EXISTS xrp_spot_samples (ts REAL PRIMARY KEY, price REAL);
            CREATE TABLE IF NOT EXISTS doge_spot_samples (ts REAL PRIMARY KEY, price REAL);
            CREATE TABLE IF NOT EXISTS bnb_spot_samples (ts REAL PRIMARY KEY, price REAL);
            CREATE TABLE IF NOT EXISTS silver_spot_samples (ts REAL PRIMARY KEY, price REAL);
            CREATE TABLE IF NOT EXISTS binanceus_spot_samples (ts REAL PRIMARY KEY, price REAL);
            CREATE TABLE IF NOT EXISTS windows (
                ticker TEXT PRIMARY KEY, open_ts REAL, close_ts REAL,
                target REAL, target_provisional INTEGER,
                resolution TEXT, resolved_at REAL,
                strike_usd REAL, volume_24h REAL, open_interest REAL,
                last_price_cents REAL, market_result TEXT,
                expiration_value REAL,
                would_sit_out INTEGER
            );
            CREATE TABLE IF NOT EXISTS kalshi_window_extras (
                ticker TEXT PRIMARY KEY, settled_at REAL,
                volume_total REAL, volume_24h REAL, open_interest REAL,
                last_price_cents REAL, market_result TEXT,
                expiration_value REAL, settlement_ts TEXT,
                trades_sample_n INTEGER,
                trades_sample_contracts REAL,
                trades_sample_yes_contracts REAL,
                trades_sample_no_contracts REAL,
                trades_sample_yes_vwap_cents REAL,
                trades_sample_last_price_cents REAL,
                trades_sample_newest_ts TEXT,
                trades_sample_oldest_ts TEXT,
                trades_sample_block_count INTEGER,
                fetched_at REAL
            );
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, window TEXT, ts REAL,
                minute REAL, direction TEXT, confidence REAL,
                action TEXT, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS track_record_summary (
                series TEXT PRIMARY KEY,
                settled INTEGER NOT NULL DEFAULT 0,
                correct INTEGER NOT NULL DEFAULT 0,
                grading TEXT DEFAULT 'last',
                last_settled INTEGER NOT NULL DEFAULT 0,
                last_correct INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS level_bounces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series TEXT, level_usd REAL, kind TEXT,
                touched_at REAL, touch_price REAL,
                decided_at REAL, decide_price REAL,
                outcome TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_level_bounces_series
                ON level_bounces(series, decided_at);
            CREATE INDEX IF NOT EXISTS idx_signals_window_action_id
                ON signals(window, action, id);
            CREATE INDEX IF NOT EXISTS idx_windows_close_ts
                ON windows(close_ts);
            """
        )
        # Kalshi extras (piece 3): backfill the new windows columns on
        # a database created before they existed. Fresh databases
        # already have them from the CREATE TABLE above.
        _migrate_kalshi_extras(conn)
        # Grade-the-first-call (2026-11-18): grading + diagnostic
        # columns on the summary, shadow flag on windows. Same
        # migrate-then-regrade pattern as the Kalshi extras above.
        _migrate_grading(conn)
        _migrate_would_sit_out(conn)
        # Accounts + VIP gate tables (added 2026-11-17): auth owns the
        # schema (AUTH_SCHEMA_SQL in auth.py) so the tables are created
        # identically on both backends. Nothing about the existing
        # tables changes shape.
        auth.init_auth_tables(conn)
        # Summary seed + one-time regrade (added 2026-11-17, regraded
        # 2026-11-18): the summary is the ONLY thing /track-record
        # reads on the hot path, so a series with history but no
        # summary row gets its counts computed once here, at boot,
        # before the poll thread can settle anything — and a series
        # still graded under the old last-signal rule is recomputed
        # from the stored signals to first-call grading (see
        # _regrade_series). On a fresh database this inserts (0, 0)
        # and costs nothing.
        for series in ("KXBTC15M", "KXETH15M"):
            _regrade_series(conn, series)
        conn.commit()
        conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 - logged, retried lazily
        print(f"[db] init failed: {exc}", flush=True)
        return False


def init_db():
    """Create the schema at import, retrying a few times on Turso.

    A transient Turso outage at boot must not kill the import: db()
    retries the init lazily afterwards (at most once a minute).
    """
    global _db_ready, _db_init_last_try
    _db_init_last_try = time.time()
    if _USE_TURSO:
        for _ in range(3):
            if _init_db_once():
                _db_ready = True
                return
            time.sleep(2)
        print("[db] Turso init failed after 3 attempts; will retry lazily",
              flush=True)
    else:
        _db_ready = _init_db_once()


def db():
    """A connection on the configured backend (Turso or local SQLite).

    If the schema was never created successfully (e.g. Turso was down
    at boot), retry the init at most once a minute — a primary-down
    at boot must not brick the service permanently.
    """
    global _db_ready, _db_init_last_try
    if not _db_ready:
        now = time.time()
        if now - _db_init_last_try >= 60:
            _db_init_last_try = now
            _db_ready = _init_db_once()
    return _raw_db()


init_db()

# Accounts + VIP gate (added 2026-11-17): the auth module needs this
# file's db() factory, and the routers must be registered before the
# static /app mount at the end of this file (Starlette matches in
# registration order, so the /app/live + /app/callouts API routes win
# over the static files). The public endpoints below are untouched.
auth.set_db_factory(db)
app.include_router(auth.router)
app.include_router(billing.router)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "BTC-Oracle-Pro-API/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


# ------------------------------------------------------- live state
_state = {
    "spot": None,
    "spot_ts": None,
    "window": None,          # {ticker, open_ts, close_ts}
    "markets_ts": None,
    "target": None,
    "target_provisional": False,
    "sigma_1m": None,
    "last_signal": None,
    "levels": None,          # support/resistance from recent spot history
    "levels_ts": None,
    "book": None,            # {yes_bid_cents, yes_ask_cents, no_bid_cents, no_ask_cents}
    "book_ts": None,
    "kalshi_extras": None,     # latest parsed /markets extras (KXBTC15M)
    "kalshi_extras_ts": None,
    "eth_spot": None,
    "eth_spot_ts": None,
    "eth_window": None,
    "eth_markets_ts": None,
    "eth_target": None,
    "eth_target_provisional": False,
    "eth_sigma_1m": None,
    "eth_last_signal": None,
    "eth_levels": None,
    "eth_levels_ts": None,
    "eth_book": None,
    "eth_book_ts": None,
    "g15_spot": None,
    "g15_spot_ts": None,
    "g15_window": None,
    "g15_markets_ts": None,
    "g15_target": None,
    "g15_target_provisional": False,
    "g15_sigma_1m": None,
    "g15_last_signal": None,
    "g15_levels": None,
    "g15_levels_ts": None,
    "g15_book": None,
    "g15_book_ts": None,
    "sol_spot": None,
    "sol_spot_ts": None,
    "sol_window": None,
    "sol_markets_ts": None,
    "sol_target": None,
    "sol_target_provisional": False,
    "sol_sigma_1m": None,
    "sol_last_signal": None,
    "sol_levels": None,
    "sol_levels_ts": None,
    "sol_book": None,
    "sol_book_ts": None,
    "xrp_spot": None,
    "xrp_spot_ts": None,
    "xrp_window": None,
    "xrp_markets_ts": None,
    "xrp_target": None,
    "xrp_target_provisional": False,
    "xrp_sigma_1m": None,
    "xrp_last_signal": None,
    "xrp_levels": None,
    "xrp_levels_ts": None,
    "xrp_book": None,
    "xrp_book_ts": None,
    "doge_spot": None,
    "doge_spot_ts": None,
    "doge_window": None,
    "doge_markets_ts": None,
    "doge_target": None,
    "doge_target_provisional": False,
    "doge_sigma_1m": None,
    "doge_last_signal": None,
    "doge_levels": None,
    "doge_levels_ts": None,
    "doge_book": None,
    "doge_book_ts": None,
    "bnb_spot": None,
    "bnb_spot_ts": None,
    "bnb_window": None,
    "bnb_markets_ts": None,
    "bnb_target": None,
    "bnb_target_provisional": False,
    "bnb_sigma_1m": None,
    "bnb_last_signal": None,
    "bnb_levels": None,
    "bnb_levels_ts": None,
    "bnb_book": None,
    "bnb_book_ts": None,
    "sil_spot": None,
    "sil_spot_ts": None,
    "sil_window": None,
    "sil_markets_ts": None,
    "sil_target": None,
    "sil_target_provisional": False,
    "sil_sigma_1m": None,
    "sil_last_signal": None,
    "sil_levels": None,
    "sil_levels_ts": None,
    "sil_book": None,
    "sil_book_ts": None,
    "gold_spot": None,       # USD/oz from the public gold feed
    "gold_spot_ts": None,
    "gold_market": None,     # {ticker, question, close_ts, strike_usd}
    "top_markets": None,     # cached /top-markets payload
    "top_markets_ts": None,
    "top_markets_prev": None,  # {ticker: yes_price_cents} at last refresh
    "top_markets_note": None,
    "gold_market_ts": None,
    "gold_book": None,
    "gold_book_ts": None,
    "binanceus_spot": None,  # Binance.US BTCUSD spot (extra-spot, display only)
    "binanceus_spot_ts": None,
    "binanceus_spot_try_ts": None,
    "kraken_mid": None,      # always None server-side: the Kraken mid is
    "kraken_mid_ts": None,   # derived in the viewer from its already-polled depth feed
    "extra_spot_consensus": None,
    "extra_spot_diverged": False,
    "started_at": time.time(),
    "poll_last_ok": None,
    "errors": [],
}


def avg_spot(start_ts, end_ts, table="spot_samples"):
    conn = db()
    row = conn.execute(
        f"SELECT AVG(price) AS a, COUNT(*) AS n FROM {table} "
        "WHERE ts >= ? AND ts < ?",
        (start_ts, end_ts),
    ).fetchone()
    conn.close()
    return (row["a"], row["n"]) if row and row["n"] else (None, 0)


def current_window(url, window_secs, tag):
    """The open market closing soonest — never just the first row.

    The markets list order is not guaranteed, and at rollover the list can
    be momentarily empty (old window closed, new one not yet listed).
    An empty or unparseable list returns None instead of raising: a single
    bad tick must never kill the poll thread (that froze the API 2026-11-16).
    """
    try:
        data = fetch_json(url)
    except Exception as exc:  # noqa: BLE001 - surfaced in /health
        _state["errors"].append(f"{tag} markets {exc}")
        return None
    now = time.time()
    best = None
    for m in data.get("markets", []):
        try:
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            continue
        close_ts = close.timestamp()
        if close_ts <= now:
            continue  # already closed; never the "current" window
        if best is None or close_ts < best["close_ts"]:
            best = {
                "ticker": m["ticker"],
                "open_ts": close_ts - window_secs,
                "close_ts": close_ts,
                # Kalshi extras (piece 3): the chosen row's verified
                # fields, parsed here so the rollover INSERT can store
                # them with zero extra Kalshi calls and zero extra
                # writes. Display/diagnostics only — never signal.
                "extras": parse_kalshi_market_extras(m),
            }
    return best


def fetch_book(ticker):
    """Live bid/ask for one market from the public REST orderbook.

    The book lives in `orderbook_fp`: yes_dollars/no_dollars bid ladders
    (bids only). YES ask = 1 - NO bid, NO ask = 1 - YES bid. Returns
    cents or None. Never raises — a failed fetch just leaves the last
    good book in place.
    """
    try:
        data = fetch_json(f"{KALSHI_ORDERBOOK}/{ticker}/orderbook")
        ob = data.get("orderbook_fp", {})
        yes = ob.get("yes_dollars") or []
        no = ob.get("no_dollars") or []
        if not yes or not no:
            return None
        yes_bid = max(float(p) for p, _ in yes)
        no_bid = max(float(p) for p, _ in no)

        def top_ladder(side):
            rows = sorted(
                ((float(p), float(s)) for p, s in side), reverse=True
            )[:6]
            return [
                {
                    "price_cents": round(p * 100, 1),
                    "size": int(s) if s == int(s) else s,
                }
                for p, s in rows
            ]

        return {
            "yes_bid_cents": round(yes_bid * 100, 1),
            "yes_ask_cents": round((1 - no_bid) * 100, 1),
            "no_bid_cents": round(no_bid * 100, 1),
            "no_ask_cents": round((1 - yes_bid) * 100, 1),
            "yes_ladder": top_ladder(yes),
            "no_ladder": top_ladder(no),
        }
    except Exception:
        return None


# ------------------------------------------------- Kalshi extras (piece 3)
# Verified keyless 2026-11-18 (single spaced calls, api.elections host):
# - /markets rows carry volume_24h_fp, volume_fp, open_interest_fp,
#   last_price_dollars, yes/no bid+ask dollars and sizes, floor_strike
#   (the market's own strike), and — once settled — result,
#   expiration_value (the BRTI settlement average) and settlement_ts.
#   previous_price_dollars exists but read '0.0000' on every sample.
# - /markets/trades?ticker=...&limit=N works keyless (HTTP 200): the
#   live public tape, newest first. WebSocket is NOT needed for a
#   settle-time sample. One page only — never paginated in the poll loop.
# Display/diagnostics ONLY: nothing here feeds the v1 signal, the
# windows resolution, or track_record_summary. Storage is one row per
# settled BTC window (plus the same rollover INSERT's new columns), so
# the extra write load is 2 rows x 4 windows/hour = 5,760/month —
# 0.36% of the ~1.6M/month free-tier headroom. The settle-time
# snapshot adds 2 Kalshi calls per settled BTC window (settled-row
# fetch + one tape page: 8/hour, at rollover only); no existing
# poll frequency changes.


def _extras_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_kalshi_market_extras(m):
    """Verified-available fields off one /markets row (open or settled).

    Pure parsing — no network, never raises. All money strings are
    Kalshi fixed-point dollar strings ('0.6700'); volumes/OI are
    '_fp' contract strings. Missing/unparseable fields become None,
    never 0 (0 would read as a real price/volume)."""
    if not isinstance(m, dict):
        return {}

    def cents(key):
        v = _extras_float(m.get(key))
        return round(v * 100, 1) if v is not None else None

    return {
        "strike_usd": _extras_float(m.get("floor_strike")),
        "volume_24h": _extras_float(m.get("volume_24h_fp")),
        "volume_total": _extras_float(m.get("volume_fp")),
        "open_interest": _extras_float(m.get("open_interest_fp")),
        "last_price_cents": cents("last_price_dollars"),
        "previous_price_cents": cents("previous_price_dollars"),
        "yes_bid_cents": cents("yes_bid_dollars"),
        "yes_ask_cents": cents("yes_ask_dollars"),
        "no_bid_cents": cents("no_bid_dollars"),
        "no_ask_cents": cents("no_ask_dollars"),
        "yes_bid_size": _extras_float(m.get("yes_bid_size_fp")),
        "yes_ask_size": _extras_float(m.get("yes_ask_size_fp")),
        "status": m.get("status"),
        "market_result": m.get("result") or None,
        "expiration_value": _extras_float(m.get("expiration_value")),
        "settlement_ts": m.get("settlement_ts"),
    }


def parse_kalshi_trades(trades):
    """Aggregate ONE page of /markets/trades rows (newest first).

    Returns a labelled SAMPLE aggregate — the page holds the most
    recent trades at fetch time, never the full window tape. Counts
    and contract totals are exact for the page; VWAP is
    contract-weighted on the taker side's yes price. Never raises;
    empty/None input yields n=0 with every other field None/0."""
    rows = trades if isinstance(trades, list) else []
    total = yes_c = no_c = 0.0
    yes_notional = 0.0
    blocks = 0
    last_price = None
    newest_ts = None
    oldest_ts = None
    for t in rows:
        if not isinstance(t, dict):
            continue
        count = _extras_float(t.get("count_fp")) or 0.0
        total += count
        side = t.get("taker_side")
        if side == "yes":
            yes_c += count
            p = _extras_float(t.get("yes_price_dollars"))
            if p is not None:
                yes_notional += count * p
        elif side == "no":
            no_c += count
        if t.get("is_block_trade"):
            blocks += 1
        ts = t.get("created_time")
        if newest_ts is None:
            newest_ts = ts
            p = _extras_float(t.get("yes_price_dollars"))
            last_price = round(p * 100, 1) if p is not None else None
        oldest_ts = ts
    return {
        "sample_n": len(rows),
        "sample_contracts": round(total, 2),
        "sample_yes_contracts": round(yes_c, 2),
        "sample_no_contracts": round(no_c, 2),
        "sample_yes_vwap_cents": (
            round(100 * yes_notional / yes_c, 1) if yes_c > 0 else None
        ),
        "sample_last_price_cents": last_price,
        "sample_newest_ts": newest_ts,
        "sample_oldest_ts": oldest_ts,
        "sample_block_count": blocks,
    }


def fetch_market_trades(ticker, limit=100):
    """One page of the public tape for one market (keyless, verified).

    GET /markets/trades?ticker={ticker}&limit={limit}. Returns the
    raw trades list, or None when the endpoint is unavailable (HTTP
    error, empty body, wrong shape). Never raises. One call only —
    callers never paginate."""
    try:
        data = fetch_json(
            f"{KALSHI_TRADES}?ticker={ticker}&limit={int(limit)}"
        )
        trades = data.get("trades")
        return trades if isinstance(trades, list) else None
    except Exception:
        return None


def fetch_settled_market_extras(ticker, series=KALSHI_EXTRAS_SERIES):
    """The settled row for one ticker from the public /markets list.

    GET /markets?series_ticker=...&limit=5&status=settled, matched on
    ticker. Returns parsed extras or None (not settled yet / fetch
    failed / ticker absent). Never raises."""
    try:
        data = fetch_json(
            "https://api.elections.kalshi.com/trade-api/v2/markets"
            f"?series_ticker={series}&limit=5&status=settled"
        )
    except Exception:
        return None
    for m in data.get("markets", []) or []:
        if m.get("ticker") == ticker:
            return parse_kalshi_market_extras(m)
    return None


def record_kalshi_window_extras(ticker, series=KALSHI_EXTRAS_SERIES):
    """Settle-time extras snapshot for one closed BTC window.

    Fetches the settled market row + one 100-trade tape page (2 Kalshi
    calls, only at window rollover: 8/hour for BTC), then writes ONE
    row to kalshi_window_extras and backfills the final fields onto
    the window's existing windows row (UPDATE, never INSERT — the
    windows row and its resolution are owned by settle_window, which
    already ran). Display/diagnostics only: never writes signals or
    track_record_summary. Returns the stored dict, or None when the
    settled row could not be fetched (a tape failure alone still
    stores the market row with a zero-trade sample). Never raises."""
    try:
        ex = fetch_settled_market_extras(ticker, series)
        if ex is None:
            return None
        tape = parse_kalshi_trades(fetch_market_trades(ticker, limit=100) or [])
        now = time.time()
        stored = {
            "ticker": ticker,
            "settled_at": now,
            "volume_total": ex.get("volume_total"),
            "volume_24h": ex.get("volume_24h"),
            "open_interest": ex.get("open_interest"),
            "last_price_cents": ex.get("last_price_cents"),
            "market_result": ex.get("market_result"),
            "expiration_value": ex.get("expiration_value"),
            "settlement_ts": ex.get("settlement_ts"),
            "trades_sample_n": tape["sample_n"],
            "trades_sample_contracts": tape["sample_contracts"],
            "trades_sample_yes_contracts": tape["sample_yes_contracts"],
            "trades_sample_no_contracts": tape["sample_no_contracts"],
            "trades_sample_yes_vwap_cents": tape["sample_yes_vwap_cents"],
            "trades_sample_last_price_cents": tape["sample_last_price_cents"],
            "trades_sample_newest_ts": tape["sample_newest_ts"],
            "trades_sample_oldest_ts": tape["sample_oldest_ts"],
            "trades_sample_block_count": tape["sample_block_count"],
            "fetched_at": now,
        }
        conn = db()
        conn.execute(
            "INSERT OR REPLACE INTO kalshi_window_extras "
            "(ticker, settled_at, volume_total, volume_24h, open_interest, "
            "last_price_cents, market_result, expiration_value, settlement_ts, "
            "trades_sample_n, trades_sample_contracts, "
            "trades_sample_yes_contracts, trades_sample_no_contracts, "
            "trades_sample_yes_vwap_cents, trades_sample_last_price_cents, "
            "trades_sample_newest_ts, trades_sample_oldest_ts, "
            "trades_sample_block_count, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (stored["ticker"], stored["settled_at"], stored["volume_total"],
             stored["volume_24h"], stored["open_interest"],
             stored["last_price_cents"], stored["market_result"],
             stored["expiration_value"], stored["settlement_ts"],
             stored["trades_sample_n"], stored["trades_sample_contracts"],
             stored["trades_sample_yes_contracts"],
             stored["trades_sample_no_contracts"],
             stored["trades_sample_yes_vwap_cents"],
             stored["trades_sample_last_price_cents"],
             stored["trades_sample_newest_ts"],
             stored["trades_sample_oldest_ts"],
             stored["trades_sample_block_count"], stored["fetched_at"]),
        )
        conn.execute(
            "UPDATE windows SET volume_24h = ?, open_interest = ?, "
            "last_price_cents = ?, market_result = ?, expiration_value = ? "
            "WHERE ticker = ?",
            (stored["volume_24h"], stored["open_interest"],
             stored["last_price_cents"], stored["market_result"],
             stored["expiration_value"], ticker),
        )
        conn.commit()
        conn.close()
        return stored
    except Exception:
        return None


def fetch_candles(product):
    """1-minute candles from Coinbase's public Exchange API (no key).

    Returns the last 60 candles as [{t, o, h, l, c, v}] (time, open,
    high, low, close, volume) or None. Rows arrive newest-first as
    [time, low, high, open, close, volume]. Never raises."""
    try:
        data = fetch_json(
            f"https://api.exchange.coinbase.com/products/{product}/candles"
            "?granularity=60"
        )
        out = []
        for row in sorted(data, key=lambda r: r[0])[-60:]:
            ts, low, high, open_, close, vol = row
            out.append({
                "t": int(ts), "o": open_, "h": high,
                "l": low, "c": close, "v": vol,
            })
        return out
    except Exception:
        return None


def candles_from_samples(table):
    """1-minute OHLC tape built from this service's own stored spot
    samples — the fallback for gold/silver, whose feed (api.gold-api.com)
    publishes a price only, no candle history.

    Buckets the last 60 minutes of samples by minute: open = first
    sample, high = max, low = min, close = last, v = None (no volume
    in a spot feed). Returns [{t, o, h, l, c, v}] oldest first, or []
    when the table holds nothing yet (e.g. right after a restart).
    The table name comes only from SAMPLE_TAPE_TABLES (hardcoded), never
    from a request. Never raises."""
    try:
        conn = db()
        now = time.time()
        floor = int(now // 60) * 60 - 59 * 60
        rows = conn.execute(
            f"SELECT ts, price FROM {table} WHERE ts >= ? ORDER BY ts",
            (floor,),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    buckets = {}
    for ts, price in rows:
        minute = int(ts // 60) * 60
        bucket = buckets.get(minute)
        if bucket is None:
            buckets[minute] = {
                "t": minute, "o": price, "h": price,
                "l": price, "c": price, "v": None,
            }
        else:
            bucket["h"] = max(bucket["h"], price)
            bucket["l"] = min(bucket["l"], price)
            bucket["c"] = price
    return [buckets[m] for m in sorted(buckets)]


def fetch_gold_spot():
    """Live gold spot (USD/oz) from a free public feed. Never raises."""
    try:
        data = fetch_json(GOLD_SPOT)
        return float(data["price"])
    except Exception:
        return None


def fetch_silver_spot():
    """Live silver spot (USD/oz) from the same free public feed family
    as gold (api.gold-api.com/price/XAG). Never raises."""
    try:
        data = fetch_json(SILVER_SPOT)
        return float(data["price"])
    except Exception:
        return None


# ---- EXTRA SPOT SOURCES (piece 2, display/diagnostics only) ----
# Added 2026-11-18. NOTHING in this block feeds the v1 signal, gates
# it, or touches the track record: it is a second opinion on the spot
# price for display and diagnostics. Two sources:
#   * Binance.US BTCUSD spot — NEW server poll, every 60s, public and
#     keyless (verified live 2026-11-18:
#     {"symbol": "BTCUSD", "price": "64011.98000000"}). Stored in
#     binanceus_spot_samples on the same backend as the other spot
#     tables (Turso when configured, else local SQLite). Write math:
#     1 insert / 60 s = 1,440/day = 43,200/month = 0.43% of Turso's
#     10M/month free cap, ~2.7% of the ~1.6M/month headroom left over
#     the measured ~8.4M/month — plus one hourly prune DELETE
#     (~720/month). Fits with headroom.
#   * Kraken BTC mid-price — NO new request anywhere. The Kraken depth
#     feed (api.kraken.com/0/public/Depth?pair=XBTUSD&count=10) is
#     already polled every 15s by the viewer (index-pulse.html, browser
#     side — tracker.py itself never fetches Kraken; the 15s poll lives
#     in the viewer, not the server). The depth response carries best
#     bid/ask as result.<pair>.bids[0][0] / result.<pair>.asks[0][0]
#     (verified live 2026-11-18: best bid 64082.30, best ask 64082.40
#     -> mid 64082.35), so kraken_mid_from_depth() below derives
#     mid = (best bid + best ask) / 2 from that response with no extra
#     poll. The mid is derived where the depth already exists — in the
#     viewer — and the server keeps only the derivation helper plus a
#     payload slot that stays None server-side (see
#     extra_spot_payload()).
# The consensus check compares the sources the server holds (Coinbase
# spot + Binance.US) and, in the viewer, all three including the Kraken
# mid. Threshold 0.20% (~$128 at $64k): ~7x the 0.01-0.03%
# simultaneous cross-exchange dispersion measured 2026-11-18 (Kraken
# mid vs Coinbase 0.012%, Binance.US BTCUSD vs Coinbase 0.028%), so
# ordinary arbitrage noise never trips it, while a stale or broken
# feed diverging in a live market does. A divergent flag is a log line
# + a field on the live read only — never a signal input.
BINANCEUS_TICKER = "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSD"
BINANCEUS_SPOT_TABLE = "binanceus_spot_samples"
BINANCEUS_POLL_SECS = 60  # one public, keyless request a minute
SPOT_DIVERGENCE_PCT = 0.20  # max pairwise spread (%) before flagging


def kraken_mid_from_depth(payload):
    """Mid-price from a raw Kraken Depth response — pure, no network.

    Kraken's public Depth endpoint returns
    {"error": [], "result": {"XXBTZUSD": {"asks": [["64082.40000",
    "0.088", ...]], "bids": [["64082.30000", "0.547", ...]]}}} — the
    pair key is Kraken's own (XXBTZUSD), so the first result entry is
    used, exactly like the viewer's parse. Best bid is bids[0][0],
    best ask is asks[0][0]; mid = (best bid + best ask) / 2. Returns
    {"pair", "best_bid", "best_ask", "mid", "spread_usd"} or None when
    the response is an error, empty, one-sided, or non-numeric — never
    raises, never estimates.
    """
    try:
        if not isinstance(payload, dict):
            return None
        errors = payload.get("error") or []
        if errors:
            return None
        result = payload.get("result") or {}
        if not isinstance(result, dict) or not result:
            return None
        pair, book = next(iter(result.items()))
        if not isinstance(book, dict):
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        if not isinstance(bids[0], (list, tuple)) or not bids[0]:
            return None
        if not isinstance(asks[0], (list, tuple)) or not asks[0]:
            return None
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if not (math.isfinite(best_bid) and math.isfinite(best_ask)):
            return None
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return None
        return {
            "pair": pair,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": (best_bid + best_ask) / 2,
            "spread_usd": best_ask - best_bid,
        }
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def parse_binanceus_ticker(payload):
    """BTC price from a Binance.US ticker/price response — pure.

    The public endpoint returns {"symbol": "BTCUSD", "price":
    "64011.98000000"}. Returns the float price, or None for a wrong
    symbol, a missing/non-numeric price, or a non-positive price.
    """
    try:
        if not isinstance(payload, dict):
            return None
        if payload.get("symbol") != "BTCUSD":
            return None
        price = float(payload["price"])
        if not math.isfinite(price) or price <= 0:
            return None
        return price
    except (TypeError, ValueError, KeyError):
        return None


def fetch_binanceus_spot():
    """Live Binance.US BTCUSD spot (USD). Public, no key. Never raises."""
    try:
        return parse_binanceus_ticker(fetch_json(BINANCEUS_TICKER))
    except Exception:
        return None


def check_spot_consensus(prices, threshold_pct=SPOT_DIVERGENCE_PCT):
    """Cross-exchange agreement check — pure, diagnostic only.

    prices: {"coinbase": 63994.14, "kraken_mid": 63986.65, ...} with
    None values skipped. Returns {"sources", "max_spread_pct",
    "worst_pair", "threshold_pct", "diverged", "note"}. diverged is
    True only when the widest pairwise spread is STRICTLY above the
    threshold, so a spread exactly at the threshold does not flag.
    With fewer than two usable sources there is nothing to compare:
    diverged is False and max_spread_pct is None. Never raises.
    """
    try:
        usable = {
            name: float(value)
            for name, value in (prices or {}).items()
            if value is not None
        }
    except (TypeError, ValueError, AttributeError):
        usable = {}
    usable = {
        name: value
        for name, value in usable.items()
        if math.isfinite(value) and value > 0
    }
    if len(usable) < 2:
        return {
            "sources": usable,
            "max_spread_pct": None,
            "worst_pair": None,
            "threshold_pct": threshold_pct,
            "diverged": False,
            "note": "Need 2+ spot sources to compare.",
        }
    worst_pair, worst_spread = None, 0.0
    names = sorted(usable)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            spread = abs(usable[a] - usable[b]) / ((usable[a] + usable[b]) / 2) * 100
            if spread > worst_spread:
                worst_pair, worst_spread = [a, b], spread
    diverged = worst_spread > threshold_pct
    return {
        "sources": usable,
        "max_spread_pct": round(worst_spread, 4),
        "worst_pair": worst_pair,
        "threshold_pct": threshold_pct,
        "diverged": diverged,
        "note": (
            "Spot sources disagree by more than "
            f"{threshold_pct}% — diagnostic only, never a signal input."
            if diverged
            else "Spot sources agree within threshold."
        ),
    }


def poll_extra_spot(now):
    """One Binance.US spot tick (throttled to BINANCEUS_POLL_SECS).

    Called once per poll loop, not per series: at most one public,
    keyless Binance.US request a minute. Stores the sample in
    binanceus_spot_samples (same backend as the other spot tables),
    refreshes the in-memory state, and recomputes the consensus over
    the sources the server holds. Divergence is logged only on the
    False -> True transition (and a one-line "resolved" on True ->
    False), so a stuck feed logs once, not every minute. Never raises:
    a failed fetch leaves the last good read in place.
    """
    try:
        if now - (_state.get("binanceus_spot_try_ts") or 0) < BINANCEUS_POLL_SECS:
            return
        _state["binanceus_spot_try_ts"] = now
        price = fetch_binanceus_spot()
        if price is None:
            return
        _state["binanceus_spot"] = price
        _state["binanceus_spot_ts"] = now
        conn = db()
        conn.execute(
            f"INSERT OR IGNORE INTO {BINANCEUS_SPOT_TABLE} (ts, price) VALUES (?, ?)",
            (now, price),
        )
        if now - _last_prune.get(BINANCEUS_SPOT_TABLE, 0) >= 3600:
            conn.execute(
                f"DELETE FROM {BINANCEUS_SPOT_TABLE} WHERE ts < ?", (now - 86400,)
            )
            _last_prune[BINANCEUS_SPOT_TABLE] = now
        conn.commit()
        conn.close()
        consensus = check_spot_consensus({
            "coinbase": _state.get("spot"),
            "binanceus": price,
            "kraken_mid": _state.get("kraken_mid"),
        })
        _state["extra_spot_consensus"] = consensus
        was = _state.get("extra_spot_diverged") or False
        _state["extra_spot_diverged"] = bool(consensus["diverged"])
        if consensus["diverged"] and not was:
            print(
                "[extra-spot] DIVERGENCE: spot sources disagree by "
                f"{consensus['max_spread_pct']}% "
                f"(worst pair {consensus['worst_pair']}, threshold "
                f"{SPOT_DIVERGENCE_PCT}%) — diagnostic only",
                flush=True,
            )
        elif was and not consensus["diverged"]:
            print("[extra-spot] spot sources agree again", flush=True)
    except Exception as exc:  # noqa: BLE001 - a diagnostics failure must
        # never kill the poll thread; the last good read stays in place.
        _state["errors"].append(f"extra-spot {exc}")


def extra_spot_payload():
    """The extra-spot block for the BTC /live payload (display only).

    kraken_mid_usd is None server-side BY DESIGN: the Kraken mid is
    derived in the viewer from the depth feed it already polls every
    15s (the server never fetches Kraken), and the viewer fills in the
    three-way comparison from this same payload. The server consensus
    covers the two sources the server holds (Coinbase + Binance.US).
    """
    now = time.time()
    return {
        "coinbase_usd": _state.get("spot"),
        "binanceus_usd": _state.get("binanceus_spot"),
        "binanceus_age_seconds": (
            round(now - _state["binanceus_spot_ts"])
            if _state.get("binanceus_spot_ts")
            else None
        ),
        "binanceus_source": (
            "api.binance.us/api/v3/ticker/price?symbol=BTCUSD "
            "(public, no key, polled every 60s)"
        ),
        "kraken_mid_usd": _state.get("kraken_mid"),
        "kraken_note": (
            "Derived in the viewer from the Kraken depth feed it already "
            "polls every 15s (api.kraken.com/0/public/Depth); the server "
            "never fetches Kraken — see kraken_mid_from_depth()."
        ),
        "consensus": _state.get("extra_spot_consensus"),
        "threshold_pct": SPOT_DIVERGENCE_PCT,
        "note": (
            "Display/diagnostic only — never an input to the signal "
            "or the track record."
        ),
    }
# ---- END EXTRA SPOT SOURCES ----


def fetch_gold_market():
    """The open KXGOLD market closing soonest — or None when no gold
    market is open (verified: none open 2026-11-16 evening). Gold
    markets are daily; the tab shows an honest empty state until one
    opens. Never raises."""
    try:
        data = fetch_json(KALSHI_GOLD_MARKETS)
    except Exception as exc:  # noqa: BLE001
        _state["errors"].append(f"kalshi gold {exc}")
        return None
    now = time.time()
    best = None
    for m in data.get("markets", []):
        try:
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            continue
        close_ts = close.timestamp()
        if close_ts <= now:
            continue
        if best is None or close_ts < best["close_ts"]:
            strike = m.get("floor_strike")
            if strike is None:
                strike = m.get("strike_price")
            best = {
                "ticker": m["ticker"],
                "question": m.get("question"),
                "close_ts": close_ts,
                "strike_usd": float(strike) if strike is not None else None,
            }
    return best


def _round_usd(value, spot):
    """Round a dollar figure with decimals that fit the price scale.

    BTC/ETH/gold (spot >= 10) keep the historical 2 decimals. Sub-$10
    coins need more: XRP ~$1 and DOGE ~$0.06 would otherwise round
    sigma and spot-vs-target to 0.0. Returns None for a None value.
    """
    if value is None:
        return None
    if spot and spot < 1:
        return round(value, 6)
    if spot and spot < 10:
        return round(value, 4)
    return round(value, 2)


def compute_levels(now, spot, table, prominence=5, cluster=25, round_step=500):
    """Support/resistance from recent spot history (swing highs/lows).

    Builds 1-minute bars from the spot table over the last 6 hours. A
    swing high is a 1-min high at least `prominence` dollars above its 3
    neighbours each side (the prominence bar keeps flat tops/bottoms from
    counting twice); swings within `cluster` dollars merge into one
    level, counted by touches. Round levels (every `round_step` dollars)
    are added as psychological levels (touches 0, kind "round"). Returns
    None when there is under 30 minutes of history — levels need data to
    mean anything, and the free tier wipes history on restart, so early
    levels are thin by design.
    """
    try:
        conn = db()
        rows = conn.execute(
            f"SELECT ts, price FROM {table} WHERE ts >= ? ORDER BY ts",
            (now - 6 * 3600,),
        ).fetchall()
        conn.close()
    except Exception:
        return None
    if not rows or (rows[-1]["ts"] - rows[0]["ts"]) < 1800:
        return None
    bars = {}
    for r in rows:
        m = int(r["ts"] // 60) * 60
        b = bars.setdefault(m, [r["price"], r["price"]])
        b[0] = max(b[0], r["price"])
        b[1] = min(b[1], r["price"])
    minutes = sorted(bars)
    highs = [bars[m][0] for m in minutes]
    lows = [bars[m][1] for m in minutes]

    def swings(values, is_high):
        out = []
        for i in range(3, len(values) - 3):
            v = values[i]
            neigh = values[i - 3:i] + values[i + 1:i + 4]
            if is_high and v - max(neigh) >= prominence:
                out.append((minutes[i], v))
            if (not is_high) and min(neigh) - v >= prominence:
                out.append((minutes[i], v))
        return out

    def cluster_levels(points, is_high):
        levels = []
        for ts, price in sorted(points, key=lambda p: p[1], reverse=is_high):
            hit = None
            for lv in levels:
                if abs(lv["price_usd"] - price) <= cluster:
                    hit = lv
                    break
            if hit:
                hit["touches"] += 1
                hit["price_usd"] = (
                    max(hit["price_usd"], price) if is_high
                    else min(hit["price_usd"], price)
                )
                hit["last_tested"] = max(hit["last_tested"], ts)
            else:
                levels.append({
                    "price_usd": _round_usd(price, spot), "touches": 1,
                    "last_tested": ts, "kind": "swing",
                })
        return levels

    res = [lv for lv in cluster_levels(swings(highs, True), True)
           if spot is None or lv["price_usd"] > spot]
    sup = [lv for lv in cluster_levels(swings(lows, False), False)
           if spot is None or lv["price_usd"] < spot]
    res.sort(key=lambda lv: lv["price_usd"])
    sup.sort(key=lambda lv: lv["price_usd"], reverse=True)
    if spot:
        up = (int(spot // round_step) + 1) * round_step
        dn = int(spot // round_step) * round_step
        res.append({"price_usd": _round_usd(float(up), spot), "touches": 0,
                    "last_tested": None, "kind": "round"})
        sup.append({"price_usd": _round_usd(float(dn), spot), "touches": 0,
                    "last_tested": None, "kind": "round"})
        res.sort(key=lambda lv: lv["price_usd"])
        sup.sort(key=lambda lv: lv["price_usd"], reverse=True)
    lookback = round((rows[-1]["ts"] - rows[0]["ts"]) / 60)
    return {
        "lookback_minutes": lookback,
        "resistance": res[:3],
        "support": sup[:3],
        "nearest_resistance_usd": res[0]["price_usd"] if res else None,
        "nearest_support_usd": sup[0]["price_usd"] if sup else None,
    }


def _level_bounce_bucket(series, level_usd):
    """Bucket a level price for hold-rate grouping.

    A level's recomputed price drifts a few dollars as new swings
    arrive, so grouping by exact price would fragment one level into
    many one-touch rows. Bucket width is the series' cluster radius
    (BTC $25, ETH $10) — the same radius that merges swings into a
    level in compute_levels."""
    bucket = LEVEL_BOUNCE_BUCKETS.get(series, 25.0)
    return round(math.floor(level_usd / bucket) * bucket, 2)


def _record_level_bounces(series, spot, levels, cluster, now, conn=None):
    """Detect touches of the current key levels and decide past ones.

    Display-only: never touches the signal, the entry log, or the
    track record. Runs at the 10-minute levels cadence (the levels
    themselves are only current at that cadence, so finer sampling
    would be false precision).
    - Touch: |spot - level| <= cluster (the level's own merge radius).
      One pending row per level at a time, and no re-log within the
      observe window of a decided touch: price hovering on a level is
      one touch, not many.
    - Decision: 15 minutes after the touch, the level HELD if price
      is back on its own side (support: spot >= level; resistance:
      spot <= level), else it BROKE. Pending touches older than 6h
      with no decision are marked 'stale' (never counted in the hold
      rate). Writes: one INSERT per touch + one UPDATE per decision —
      touches are rare (a few per day per series at most).
    """
    own_conn = conn is None
    if own_conn:
        conn = db()
    try:
        if spot is None or not levels:
            return
        # --- decide pending touches whose observe window has passed
        pending = conn.execute(
            "SELECT id, level_usd, kind, touched_at FROM level_bounces "
            "WHERE series = ? AND outcome IS NULL ORDER BY touched_at",
            (series,),
        ).fetchall()
        for p in pending:
            age = now - p["touched_at"]
            if age >= LEVEL_BOUNCE_STALE_SECS:
                conn.execute(
                    "UPDATE level_bounces SET outcome = 'stale', "
                    "decided_at = ?, decide_price = ? WHERE id = ?",
                    (now, spot, p["id"]),
                )
            elif age >= LEVEL_BOUNCE_OBSERVE_SECS:
                held = (
                    (spot >= p["level_usd"]) if p["kind"] == "support"
                    else (spot <= p["level_usd"])
                )
                conn.execute(
                    "UPDATE level_bounces SET outcome = ?, decided_at = ?, "
                    "decide_price = ? WHERE id = ?",
                    ("held" if held else "broke", now, spot, p["id"]),
                )
        # --- detect new touches against the current levels
        for kind in ("resistance", "support"):
            for lv in (levels.get(kind) or []):
                price = lv["price_usd"]
                if price is None or abs(spot - price) > cluster:
                    continue
                recent = conn.execute(
                    "SELECT id FROM level_bounces WHERE series = ? "
                    "AND kind = ? AND outcome IS NULL "
                    "AND ABS(level_usd - ?) <= ? "
                    "UNION ALL SELECT id FROM level_bounces WHERE series = ? "
                    "AND kind = ? AND outcome IN ('held', 'broke') "
                    "AND decided_at >= ? AND ABS(level_usd - ?) <= ? LIMIT 1",
                    (series, kind, price, cluster,
                     series, kind, now - LEVEL_BOUNCE_OBSERVE_SECS,
                     price, cluster),
                ).fetchone()
                if recent is not None:
                    continue
                conn.execute(
                    "INSERT INTO level_bounces (series, level_usd, kind, "
                    "touched_at, touch_price) VALUES (?, ?, ?, ?, ?)",
                    (series, price, kind, now, spot),
                )
        conn.commit()
    finally:
        if own_conn:
            conn.close()


def _level_bounce_summary(conn, series, limit=20):
    """Per-level hold rates + recent decided outcomes for /level-bounces.

    Levels are grouped by price bucket (the series' cluster radius)
    so a drifting level still counts as one level. Returns (levels,
    recent): levels sorted by touches desc, recent newest first.
    'stale' outcomes are never counted in the hold rate.
    """
    like = (series + "%",) if series else ("%",)
    rows = conn.execute(
        "SELECT series, level_usd, kind, outcome FROM level_bounces "
        "WHERE outcome IN ('held', 'broke') AND series LIKE ?",
        (like[0],),
    ).fetchall()
    groups = {}
    for r in rows:
        bucket = _level_bounce_bucket(r["series"], r["level_usd"])
        key = (r["series"], r["kind"], bucket)
        g = groups.setdefault(
            key, {"series": r["series"], "kind": r["kind"],
                  "level_usd": bucket, "touches": 0, "held": 0, "broke": 0}
        )
        g["touches"] += 1
        if r["outcome"] == "held":
            g["held"] += 1
        else:
            g["broke"] += 1
    levels = []
    for g in sorted(groups.values(), key=lambda x: -x["touches"]):
        g["hold_rate_pct"] = (
            round(100 * g["held"] / g["touches"], 1) if g["touches"] else None
        )
        levels.append(g)
    recent_rows = conn.execute(
        "SELECT series, level_usd, kind, touched_at, touch_price, "
        "decided_at, decide_price, outcome FROM level_bounces "
        "WHERE outcome IN ('held', 'broke') AND series LIKE ? "
        "ORDER BY decided_at DESC LIMIT ?",
        (like[0], limit),
    ).fetchall()
    recent = [
        {
            "series": r["series"],
            "level_usd": r["level_usd"],
            "kind": r["kind"],
            "touched_at": datetime.fromtimestamp(
                r["touched_at"], tz=timezone.utc
            ).isoformat(),
            "touch_price": r["touch_price"],
            "decided_at": datetime.fromtimestamp(
                r["decided_at"], tz=timezone.utc
            ).isoformat() if r["decided_at"] else None,
            "decide_price": r["decide_price"],
            "outcome": r["outcome"],
        }
        for r in recent_rows
    ]
    return levels, recent


def sigma_1m(now, table="spot_samples"):
    """Std dev of spot over the last 60 seconds, in dollars."""
    try:
        conn = db()
        rows = conn.execute(
            f"SELECT price FROM {table} WHERE ts >= ? ORDER BY ts", (now - 60,)
        ).fetchall()
        conn.close()
    except Exception:
        return None
    prices = [r["price"] for r in rows]
    if len(prices) < 5:
        return None
    mean = sum(prices) / len(prices)
    var = sum((p - mean) ** 2 for p in prices) / len(prices)
    return math.sqrt(var)


def direction_and_confidence(spot, target, sig):
    """v1 signal logic — UNVALIDATED. Distance from target in sigmas."""
    if spot is None or target is None:
        return None, None, "HOLD", "waiting for live data"
    diff = spot - target
    direction = "UP" if diff >= 0 else "DOWN"
    if sig and sig > 0:
        z = abs(diff) / sig
    else:
        z = 0.0
    confidence = round(min(95.0, 50.0 + 22.0 * z), 1)
    return direction, confidence, None, None


def settle_window(ticker, table="spot_samples"):
    """Judge a closed window from the final-minute spot average."""
    conn = db()
    row = conn.execute(
        "SELECT target, close_ts, resolution FROM windows WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    if not row or row["resolution"]:
        conn.close()
        return
    target, close_ts = row["target"], row["close_ts"]
    avg, n = avg_spot(close_ts - 60, close_ts, table)
    if avg is None or target is None:
        conn.close()
        return
    resolution = "YES" if avg >= target else "NO"
    # First-call grading (2026-11-18): the actionable call is the
    # first ENTER at minute <= 9. Its confidence also decides the
    # selectivity shadow flag (first ENTER < 70 => would have sat
    # out). The flag rides this same resolution UPDATE — zero extra
    # writes — and is logging only: no endpoint ever shows it.
    first = _first_enter(conn, ticker)
    would_sit_out = None
    if first is not None:
        would_sit_out = (
            1 if first["confidence"] < SIT_OUT_CONFIDENCE_FLOOR else 0
        )
    conn.execute(
        "UPDATE windows SET resolution = ?, resolved_at = ?, "
        "would_sit_out = ? WHERE ticker = ?",
        (resolution, time.time(), would_sit_out, ticker),
    )
    # Keep the track-record summary in step, in the SAME commit as the
    # resolution write (added 2026-11-17): /track-record reads the
    # summary, not the windows table, so a settle that resolved the
    # window but missed the summary would undercount forever. The
    # graded count is the FIRST call (2026-11-18): a window with no
    # actionable ENTER is not a trade and is not counted at all. The
    # last-signal verdict is kept alongside as a labeled diagnostic
    # only — it never becomes the headline.
    last = conn.execute(
        "SELECT direction FROM signals WHERE window = ? "
        "ORDER BY id DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if first is not None or last is not None:
        series = ticker.split("-")[0]
        first_correct = 0
        if first is not None:
            first_correct = 1 if (
                (first["direction"] == "UP" and resolution == "YES")
                or (first["direction"] == "DOWN" and resolution == "NO")
            ) else 0
        last_correct = 0
        if last is not None:
            last_correct = 1 if (
                (last["direction"] == "UP" and resolution == "YES")
                or (last["direction"] == "DOWN" and resolution == "NO")
            ) else 0
        conn.execute(
            "INSERT INTO track_record_summary (series, settled, correct, "
            "last_settled, last_correct, grading) "
            "VALUES (?, ?, ?, ?, ?, 'first') ON CONFLICT(series) DO UPDATE SET "
            "settled = settled + excluded.settled, "
            "correct = correct + excluded.correct, "
            "last_settled = last_settled + excluded.last_settled, "
            "last_correct = last_correct + excluded.last_correct, "
            "grading = 'first'",
            (
                series,
                1 if first is not None else 0,
                first_correct,
                1 if last is not None else 0,
                last_correct,
            ),
        )
    conn.commit()
    conn.close()


def poll_series(cfg, now):
    """One tick of tracking for one 15-minute market (BTC or ETH).

    Same code for both series: spot sample, window (throttled markets
    fetch — every 30s steady-state, every tick in the last minute before
    close so rollovers are caught), target, v1 signal logged once per
    minute (never in the last 3 minutes), live book, key levels.
    A failure in any one step must never kill the poll thread.
    """
    K = cfg["keys"]
    S = _state
    # --- spot sample (every loop, ~5s)
    # Gold uses the gold-api feed ({"price": ...}), not Coinbase's shape.
    # The ~5s cadence is what the target rule needs: >=5 samples in the
    # minute before the window opens. The sample also refreshes the
    # shared daily-gold spot, so the `gold` entry stays fresh too.
    try:
        if cfg.get("spot_kind") == "gold":
            price = fetch_gold_spot()
            if price is not None:
                S["gold_spot"] = price
                S["gold_spot_ts"] = now
        elif cfg.get("spot_kind") == "silver":
            price = fetch_silver_spot()
        else:
            data = fetch_json(cfg["spot_url"])
            price = float(data["data"]["amount"])
    except Exception as exc:  # noqa: BLE001
        S["errors"].append(f"{cfg['name']} spot {exc}")
        price = None
    if price is not None:
        S[K["spot"]] = price
        S[K["spot_ts"]] = now
        conn = db()
        conn.execute(
            f"INSERT OR IGNORE INTO {cfg['spot_table']} (ts, price) VALUES (?, ?)",
            (now, price),
        )
        # Prune 24h-old samples at most once an hour per table (see
        # _last_prune): the same rows are deleted, but ~108 DELETE
        # round trips a minute become 8 an hour. No query reads samples
        # older than 6 hours, so an hour-late prune changes nothing.
        if now - _last_prune.get(cfg["spot_table"], 0) >= 3600:
            conn.execute(
                f"DELETE FROM {cfg['spot_table']} WHERE ts < ?", (now - 86400,)
            )
            _last_prune[cfg["spot_table"]] = now
        conn.commit()
        conn.close()

    # --- window (throttled fetch; the window can only change at its close)
    stored = S[K["window"]]
    need = (
        stored is None
        or now >= stored["close_ts"] - 60
        or now - (S.get(K["markets_ts"]) or 0) >= 30
    )
    win = stored
    if need:
        fetched = current_window(cfg["markets_url"], cfg["window_secs"], cfg["name"])
        S[K["markets_ts"]] = now
        if fetched:
            win = fetched
    if win:
        prev = stored
        if not prev or prev["ticker"] != win["ticker"]:
            # window rolled: settle the old one, start the new one
            # (gold writes no windows/signals rows — no track record)
            if prev and cfg.get("track_record", True):
                try:
                    settle_window(prev["ticker"], cfg["spot_table"])
                except Exception as exc:  # noqa: BLE001
                    # A settle failure must never kill the poll thread
                    # (an uncaught settle error froze the API on
                    # 2026-11-16). The window stays unsettled; the
                    # error is visible in /health.
                    S["errors"].append(f"settle {exc}")
            # Kalshi extras (piece 3, BTC only, display/diagnostics):
            # one settle-time snapshot of the closed window — final
            # volume / OI / result / settlement value + a 100-trade
            # tape sample. Never touches the resolution, signals or
            # the track-record summary. Failures are swallowed: a
            # missing extras row must never block the rollover.
            if prev and cfg.get("series_ticker") == KALSHI_EXTRAS_SERIES:
                try:
                    record_kalshi_window_extras(prev["ticker"])
                except Exception as exc:  # noqa: BLE001
                    S["errors"].append(f"kalshi-extras {exc}")
            S[K["window"]] = win
            S[K["target"]] = None
            S[K["target_provisional"]] = False
            # Kalshi extras (piece 3): keep the live window's parsed
            # market fields in memory for /kalshi-extras (BTC only).
            if cfg.get("series_ticker") == KALSHI_EXTRAS_SERIES and win.get("extras"):
                S["kalshi_extras"] = win["extras"]
                S["kalshi_extras_ts"] = now
            # target = 60s average before the window opened
            avg, n = avg_spot(win["open_ts"] - 60, win["open_ts"], cfg["spot_table"])
            if avg is not None and n >= 5:
                target, prov = avg, False
            else:
                # started mid-window: provisional from earliest samples
                early, n2 = avg_spot(
                    win["open_ts"], win["open_ts"] + 120, cfg["spot_table"]
                )
                target, prov = early, True
            if target is None and S[K["spot"]]:
                # restarted after minute 2: no pre-open or early samples
                # exist, so anchor provisionally to the live spot rather
                # than leaving the whole window targetless.
                target, prov = S[K["spot"]], True
            S[K["target"]] = target
            S[K["target_provisional"]] = prov
            if cfg.get("track_record", True):
                conn = db()
                # Kalshi extras (piece 3): the rollover INSERT also
                # carries the market's own strike, 24h volume, open
                # interest and last price from the /markets row already
                # fetched for the window — zero extra Kalshi calls and
                # zero extra writes. Display/diagnostics only: the
                # signal still uses the computed target, never strike.
                ex = win.get("extras") or {}
                conn.execute(
                    "INSERT OR IGNORE INTO windows "
                    "(ticker, open_ts, close_ts, target, target_provisional, "
                    "strike_usd, volume_24h, open_interest, last_price_cents) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (win["ticker"], win["open_ts"], win["close_ts"],
                     target, 1 if prov else 0, ex.get("strike_usd"),
                     ex.get("volume_24h"), ex.get("open_interest"),
                     ex.get("last_price_cents")),
                )
                conn.commit()
                conn.close()
        S[K["sigma"]] = sigma_1m(now, cfg["spot_table"])

        # --- v1 signal, logged once per minute (never in the last 3 min)
        minute = (now - win["open_ts"]) / 60.0
        last_minute = cfg["window_secs"] / 60.0 - 3
        # Gold has no signal: the whole block is skipped when
        # cfg["signal"] is False, so nothing is ever logged for gold.
        if (
            cfg.get("signal", True)
            and minute <= last_minute
            and S[K["spot"]]
            and S[K["target"]]
        ):
            direction, conf, _, _ = direction_and_confidence(
                S[K["spot"]], S[K["target"]], S[K["sigma"]]
            )
            action = "ENTER" if conf and conf >= 60 else "HOLD"
            reason = (
                f"spot {'above' if direction == 'UP' else 'below'} target "
                f"by ${abs(S[K['spot']] - S[K['target']]):.2f}"
                + (" — confidence below 60, do not chase" if action == "HOLD" else "")
            )
            # note proximity to the nearest key level in the reason
            lv = S.get(K["levels"]) or {}
            near = cfg["round_step"] // 10
            if direction == "UP" and lv.get("nearest_resistance_usd"):
                gap = lv["nearest_resistance_usd"] - S[K["spot"]]
                if 0 <= gap <= near:
                    reason += (
                        f" — ${gap:.0f} below resistance "
                        f"${lv['nearest_resistance_usd']:,.0f}"
                    )
            elif direction == "DOWN" and lv.get("nearest_support_usd"):
                gap = S[K["spot"]] - lv["nearest_support_usd"]
                if 0 <= gap <= near:
                    reason += (
                        f" — ${gap:.0f} above support "
                        f"${lv['nearest_support_usd']:,.0f}"
                    )
            last = S[K["last_signal"]]
            if not last or last["window"] != win["ticker"] or int(last["minute"]) != int(minute) or last["direction"] != direction:
                conn = db()
                conn.execute(
                    "INSERT INTO signals (window, ts, minute, direction, "
                    "confidence, action, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (win["ticker"], now, round(minute, 1), direction,
                     conf, action, reason),
                )
                conn.commit()
                conn.close()
                S[K["last_signal"]] = {
                    "window": win["ticker"], "minute": minute,
                    "direction": direction, "confidence": conf,
                    "action": action, "reason": reason, "ts": now,
                }
        # --- live Kalshi book (BTC/ETH every ~10s; gold every ~60s)
        if now - (S.get(K["book_ts"]) or 0) >= cfg.get("book_secs", 10):
            book = fetch_book(win["ticker"])
            if book:
                S[K["book"]] = book
                S[K["book_ts"]] = now
        # --- key levels (every LEVELS_REFRESH_SECS; needs history to mean anything)
        if now - (S.get(K["levels_ts"]) or 0) >= LEVELS_REFRESH_SECS:
            S[K["levels"]] = compute_levels(
                now, S[K["spot"]], cfg["spot_table"],
                cfg["prominence"], cfg["cluster"], cfg["round_step"],
            )
            S[K["levels_ts"]] = now
            # --- level bounce tracking (display only; signal series
            # only — every feed-only config sets "signal": False, so
            # this is BTC + ETH). A touch is sampled at the levels
            # cadence and decided 15 minutes later. Never touches the
            # signal or the track record; a failure here must never
            # kill the poll thread.
            if cfg.get("signal", True):
                try:
                    _record_level_bounces(
                        cfg["series_ticker"], S[K["spot"]], S[K["levels"]],
                        cfg["cluster"], now,
                    )
                except Exception as exc:  # noqa: BLE001
                    S["errors"].append(f"level bounce {exc}")


def poll_forever():
    while True:
        now = time.time()
        for cfg in (BTC_CFG, ETH_CFG, GOLD15_CFG, SOL15_CFG, XRP15_CFG, DOGE15_CFG,
                    BNB15_CFG, SILVER15_CFG):
            try:
                poll_series(cfg, now)
            except Exception as exc:  # noqa: BLE001 - backstop; poll_series
                # already guards each step, so reaching here is unexpected.
                _state["errors"].append(f"{cfg['name']} poll {exc}")
        # --- extra spot sources (piece 2, display only): Binance.US
        # BTCUSD spot, throttled to one public keyless request a minute
        # inside poll_extra_spot(). No Kraken request is made here —
        # the Kraken mid is derived in the viewer from its own feed.
        poll_extra_spot(now)
        # --- gold (slow: spot every ~30s, market every ~60s, book ~60s)
        # Gold is a daily market; it does not need 5-second polling, and
        # gentle polling respects the undocumented keyless rate limit.
        if now - (_state.get("gold_spot_ts") or 0) >= 30:
            price = fetch_gold_spot()
            if price:
                _state["gold_spot"] = price
                _state["gold_spot_ts"] = now
        if now - (_state.get("gold_market_ts") or 0) >= 60:
            _state["gold_market"] = fetch_gold_market()
            _state["gold_market_ts"] = now
        gm = _state.get("gold_market")
        if gm and now - (_state.get("gold_book_ts") or 0) >= 60:
            book = fetch_book(gm["ticker"])
            if book:
                _state["gold_book"] = book
                _state["gold_book_ts"] = now
        _state["errors"] = _state["errors"][-10:]
        _state["poll_last_ok"] = time.time()
        time.sleep(5)


def _poll_supervised():
    """Run the poll loop forever: if one tick raises, log it and restart
    the loop instead of letting the thread die (2026-11-16 freeze)."""
    while True:
        try:
            poll_forever()
        except Exception as exc:  # noqa: BLE001
            _state["errors"].append(f"poll {exc}")
            _state["errors"] = _state["errors"][-10:]
        time.sleep(5)


_poll_thread = threading.Thread(target=_poll_supervised, daemon=True)
_poll_thread.start()


def _watchdog():
    """Restart the poll thread if it ever dies — a dead thread froze the
    API on 2026-11-16 (spot and window stuck, every dashboard box empty)."""
    global _poll_thread
    while True:
        time.sleep(30)
        # Restart ONLY a dead thread. Restarting on a stale heartbeat
        # spawned extra threads that fought over the SQLite lock (2026-11-16).
        if not _poll_thread.is_alive():
            _state["errors"].append("poll thread restarted by watchdog")
            _state["errors"] = _state["errors"][-10:]
            _poll_thread = threading.Thread(target=_poll_supervised, daemon=True)
            _poll_thread.start()


threading.Thread(target=_watchdog, daemon=True).start()


# ------------------------------------------------------------ endpoints
@app.get("/health")
def health():
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _state["started_at"]),
        "spot_age_seconds": (
            round(time.time() - _state["spot_ts"]) if _state["spot_ts"] else None
        ),
        "eth_spot_age_seconds": (
            round(time.time() - _state["eth_spot_ts"])
            if _state.get("eth_spot_ts")
            else None
        ),
        "eth_window_ticker": (
            _state["eth_window"]["ticker"] if _state.get("eth_window") else None
        ),
        "poll_age_seconds": (
            round(time.time() - _state["poll_last_ok"])
            if _state.get("poll_last_ok")
            else None
        ),
        "gold_spot_age_seconds": (
            round(time.time() - _state["gold_spot_ts"])
            if _state.get("gold_spot_ts")
            else None
        ),
        "gold_market_ticker": (
            _state["gold_market"]["ticker"] if _state.get("gold_market") else None
        ),
        "gold_15m_window_ticker": (
            _state["g15_window"]["ticker"] if _state.get("g15_window") else None
        ),
        "sol_15m_window_ticker": (
            _state["sol_window"]["ticker"] if _state.get("sol_window") else None
        ),
        "xrp_15m_window_ticker": (
            _state["xrp_window"]["ticker"] if _state.get("xrp_window") else None
        ),
        "doge_15m_window_ticker": (
            _state["doge_window"]["ticker"] if _state.get("doge_window") else None
        ),
        "bnb_15m_window_ticker": (
            _state["bnb_window"]["ticker"] if _state.get("bnb_window") else None
        ),
        "silver_15m_window_ticker": (
            _state["sil_window"]["ticker"] if _state.get("sil_window") else None
        ),
        "recent_errors": _state["errors"][-3:],
    }


def live_series_payload(cfg):
    """The /live payload for one 15-minute market (BTC or ETH)."""
    K = cfg["keys"]
    win = _state[K["window"]]
    spot = _state[K["spot"]]
    target = _state[K["target"]]
    sig = _state[K["sigma"]]
    now = time.time()
    direction, conf, _, _ = direction_and_confidence(spot, target, sig)
    payload = {
        "as_of": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "market": (
            {
                "series": cfg["series_ticker"],
                "ticker": win["ticker"],
                "question": cfg["question"],
                "closes_in_seconds": max(0, round(win["close_ts"] - now)),
                "minute_of_15": round((now - win["open_ts"]) / 60.0, 1),
            }
            if win
            else None
        ),
        "spot_usd": spot,
        "target_usd": target,
        "target_provisional": _state[K["target_provisional"]],
        "target_note": "60-second spot average in the minute before the window opened (the market's strike definition). Provisional when the service started mid-window.",
        "spot_vs_target_usd": (
            _round_usd(spot - target, spot) if spot and target else None
        ),
        "spot_vs_target_pct": (
            round(100 * (spot - target) / target, 3) if spot and target else None
        ),
        "above_target": (spot >= target) if spot and target else None,
        "sigma_1m_usd": _round_usd(sig, spot) if sig else None,
        "levels": _state.get(K["levels"]),
        "signal": (
            {
                "direction": direction,
                "confidence_pct": conf,
                "status": "UNVALIDATED — v1 logic, no validated edge",
            }
            if direction
            else None
        ),
        "kalshi_book": (
            {
                "status": "LIVE",
                "yes_bid_cents": _state[K["book"]]["yes_bid_cents"],
                "yes_ask_cents": _state[K["book"]]["yes_ask_cents"],
                "no_bid_cents": _state[K["book"]]["no_bid_cents"],
                "no_ask_cents": _state[K["book"]]["no_ask_cents"],
                "yes_ladder": _state[K["book"]].get("yes_ladder"),
                "no_ladder": _state[K["book"]].get("no_ladder"),
                "age_seconds": (
                    round(now - _state[K["book_ts"]]) if _state.get(K["book_ts"]) else None
                ),
            }
            if _state.get(K["book"])
            else {
                "status": "LIVE DATA UNAVAILABLE",
                "note": "Kalshi's public orderbook returned no live bids right now.",
            }
        ),
    }
    # Extra spot sources (piece 2, BTC only, display only): the
    # consensus block rides on the BTC payload so the viewer can show
    # the three-way check without a new request. ETH has no second
    # spot source in this piece.
    if cfg["series_ticker"] == "KXBTC15M":
        payload["extra_spot"] = extra_spot_payload()
    return payload


def live_btc_payload():
    return live_series_payload(BTC_CFG)


def live_eth_payload():
    return live_series_payload(ETH_CFG)


@app.get("/live")
def live():
    return live_btc_payload()


def live_gold_payload():
    """Gold v1: live spot + the open gold market and its book, if any.
    No bot signal — the market's own YES price is the market's view.
    """
    now = time.time()
    gm = _state.get("gold_market")
    spot = _state.get("gold_spot")
    book = _state.get("gold_book")
    strike = gm["strike_usd"] if gm else None
    return {
        "as_of": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "spot_usd": spot,
        "spot_age_seconds": (
            round(now - _state["gold_spot_ts"]) if _state.get("gold_spot_ts") else None
        ),
        "spot_source": "api.gold-api.com/price/XAU (public feed)",
        "market": (
            {
                "series": "KXGOLD",
                "ticker": gm["ticker"],
                "question": gm["question"],
                "closes_in_seconds": max(0, round(gm["close_ts"] - now)),
                "strike_usd": strike,
                "spot_vs_strike_usd": (
                    round(spot - strike, 2) if spot and strike else None
                ),
            }
            if gm
            else None
        ),
        "market_note": (
            "No gold market open right now." if not gm else None
        ),
        "kalshi_book": (
            {
                "status": "LIVE",
                "yes_bid_cents": book["yes_bid_cents"],
                "yes_ask_cents": book["yes_ask_cents"],
                "no_bid_cents": book["no_bid_cents"],
                "no_ask_cents": book["no_ask_cents"],
                "yes_ladder": book.get("yes_ladder"),
                "no_ladder": book.get("no_ladder"),
                "age_seconds": (
                    round(now - _state["gold_book_ts"])
                    if _state.get("gold_book_ts")
                    else None
                ),
            }
            if book
            else None
        ),
        "signal": None,
        "signal_note": "No gold signal yet — gold v1 shows the live spot and the market's own price. A gold signal needs its own strategy and validation run.",
    }


def live_gold15_payload():
    """Gold 15-minute: the same window/target/sigma/book/levels shape as
    BTC/ETH, but NO signal — the market's own YES price is the market's
    view. A gold signal needs its own strategy and validation run."""
    payload = live_series_payload(GOLD15_CFG)
    payload["signal"] = None
    payload["signal_note"] = (
        "No gold signal — gold 15-minute shows the live window, target "
        "and the market's own price. A gold signal needs its own "
        "strategy and validation run."
    )
    return payload


def _feed_only_payload(cfg, coin):
    """SOL / XRP / DOGE 15-minute: the same window/target/sigma/book/
    levels shape as BTC/ETH, but NO signal — the market's own YES price
    is the market's view. A signal needs its own strategy and
    validation run."""
    payload = live_series_payload(cfg)
    payload["signal"] = None
    payload["signal_note"] = (
        f"No {coin} signal — {coin} 15-minute shows the live window, "
        "target and the market's own price. A signal needs its own "
        "strategy and validation run."
    )
    return payload


def live_sol15_payload():
    return _feed_only_payload(SOL15_CFG, "SOL")


def live_xrp15_payload():
    return _feed_only_payload(XRP15_CFG, "XRP")


def live_doge15_payload():
    return _feed_only_payload(DOGE15_CFG, "DOGE")


def live_bnb15_payload():
    return _feed_only_payload(BNB15_CFG, "BNB")


def live_silver15_payload():
    return _feed_only_payload(SILVER15_CFG, "silver")


@app.get("/live/all")
def live_all():
    return {
        "btc_15m": live_btc_payload(),
        "eth_15m": live_eth_payload(),
        "gold": live_gold_payload(),
        "gold_15m": live_gold15_payload(),
        "sol_15m": live_sol15_payload(),
        "xrp_15m": live_xrp15_payload(),
        "doge_15m": live_doge15_payload(),
        "bnb_15m": live_bnb15_payload(),
        "silver_15m": live_silver15_payload(),
    }


# ------------------------------------------------------------ accounts + VIP gate
# Added 2026-11-17. The public endpoints above stay public and
# unchanged (the VM capture hook polls /live/all with no login). These
# two endpoints are the gated viewer: login required, tier-filtered.
# The gate is auth.effective_vip — the vip flag PLUS a currently
# active subscription for subscription-granted VIP (see auth.py).
FREE_APP_KEYS = ("btc_15m", "eth_15m")
VIP_APP_KEYS = (
    "btc_15m", "eth_15m", "gold", "gold_15m", "sol_15m", "xrp_15m",
    "doge_15m", "bnb_15m", "silver_15m",
)
_APP_PAYLOADS = {
    "btc_15m": live_btc_payload,
    "eth_15m": live_eth_payload,
    "gold": live_gold_payload,
    "gold_15m": live_gold15_payload,
    "sol_15m": live_sol15_payload,
    "xrp_15m": live_xrp15_payload,
    "doge_15m": live_doge15_payload,
    "bnb_15m": live_bnb15_payload,
    "silver_15m": live_silver15_payload,
}


def _validation_unlocked_now():
    conn = auth.get_db()
    try:
        return auth.validation_unlocked(conn)
    finally:
        conn.close()


@app.get("/app/live")
def app_live(request: Request):
    """The gated viewer payload: live windows for the user's tier.

    Free: BTC + ETH 15-minute (the two signal markets) + Top Markets.
    VIP: all nine served markets + Top Markets. Polymarket BTC 5m and
    Broader Market are not served by this API yet, so they are in
    neither tier — the response says so rather than faking them.
    Every signal surface carries the UNVALIDATED label, same as /live.
    """
    user = auth.require_user(request)
    vip = auth.effective_vip(user)
    keys = VIP_APP_KEYS if vip else FREE_APP_KEYS
    return {
        "tier": "vip" if vip else "free",
        "email": user["email"],
        "validation_unlocked": _validation_unlocked_now(),
        "markets": {key: _APP_PAYLOADS[key]() for key in keys},
        "top_markets": refresh_top_markets(),
        "not_served_yet": [
            "Polymarket BTC 5m",
            "Broader Market",
        ],
        "not_served_note": (
            "Two of the final 12 markets have no feed in this API yet, "
            "so they appear in neither tier. They arrive with their own "
            "feeds later — nothing here stands in for them."
        ),
    }


@app.get("/app/callouts")
def app_callouts(request: Request):
    """Timely callouts — VIP only. The latest ENTER signals logged at
    minute 9 or earlier (the only signals that reliably reach you in
    time to act), one per (window, direction) — the same alert
    identity as /entries — newest first, with the window's eventual
    resolution so a call can be checked after it settles. In v1 only
    BTC/ETH have signals, so callouts are BTC/ETH only."""
    user = auth.require_user(request)
    if not auth.effective_vip(user):
        unlocked = _validation_unlocked_now()
        raise HTTPException(
            status_code=403,
            detail=(
                "Callouts are a VIP feature"
                + (
                    ", and VIP is locked until the 100-window validation passes."
                    if not unlocked
                    else ". Your account is on the free tier."
                )
            ),
        )
    conn = auth.get_db()
    try:
        rows = conn.execute(
            "SELECT s.window, s.ts, s.minute, s.direction, s.confidence, "
            "s.reason, w.resolution FROM signals s JOIN windows w "
            "ON w.ticker = s.window WHERE s.action = 'ENTER' AND s.minute <= 9 "
            "AND s.id = (SELECT MIN(id) FROM signals WHERE window = s.window "
            "AND direction = s.direction AND action = 'ENTER') "
            "ORDER BY s.ts DESC LIMIT 10",
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        res = r["resolution"]
        if res is None:
            outcome = "pending"
        elif (r["direction"] == "UP" and res == "YES") or (
            r["direction"] == "DOWN" and res == "NO"
        ):
            outcome = "win"
        else:
            outcome = "loss"
        out.append(
            {
                "window": r["window"],
                "series": r["window"].split("-")[0],
                "direction": r["direction"],
                "confidence_pct": r["confidence"],
                "minute": r["minute"],
                "entered_at": datetime.fromtimestamp(
                    r["ts"], tz=timezone.utc
                ).isoformat(),
                "reason": r["reason"],
                "resolution": res,
                "outcome": outcome,
            }
        )
    return {
        "tier": "vip",
        "status": "UNVALIDATED — v1 logic, no validated edge",
        "note": (
            "Only BTC and ETH have signals in v1; the feed-only markets "
            "have none. A callout is the first ENTER signal of its window, "
            "logged at minute 9 or earlier — the only signals that "
            "reliably reach you in time to act."
        ),
        "callouts": out,
    }


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def refresh_top_markets():
    """Refresh the cached Top Markets board (at most once per TTL).

    Ranks open markets by Kalshi's public 24h volume (volume_24h_fp)
    across the tracked liquid series in TOP_MARKETS_SERIES. The
    unfiltered /markets list cannot be ranked in one call — it is
    flooded with 40,000+ zero-volume combo markets — so the pool is
    named in the response. Movers are the biggest yes-price moves
    since the previous refresh (~5 min); 15-minute windows are excluded
    from movers because their prices move mechanically toward 0 or 1
    as the window closes. Display only: no signal, no advice. Never
    raises — a failed refresh keeps the last good board in place.
    """
    now = time.time()
    if (
        _state.get("top_markets")
        and _state.get("top_markets_ts")
        and now - _state["top_markets_ts"] < TOP_MARKETS_TTL
    ):
        return _state["top_markets"]
    prev = _state.get("top_markets_prev") or {}
    rows = []
    try:
        for series in TOP_MARKETS_SERIES:
            data = fetch_json(
                "https://api.elections.kalshi.com/trade-api/v2/markets"
                f"?series_ticker={series}&limit=50&status=open"
            )
            for m in data.get("markets", []):
                vol24 = _to_float(m.get("volume_24h_fp"))
                if vol24 <= 0:
                    continue
                last = _to_float(m.get("last_price_dollars"))
                rows.append({
                    "ticker": m.get("ticker"),
                    "series": series,
                    "title": m.get("title"),
                    "yes_price_cents": round(last * 100, 1) if last else None,
                    "volume_24h": vol24,
                    "volume_total": _to_float(m.get("volume_fp")),
                    "close_time": m.get("close_time"),
                })
    except Exception as exc:  # noqa: BLE001 - keep the last good board
        _state["errors"].append(f"top-markets refresh {exc}")
        _state["errors"] = _state["errors"][-10:]
        if _state.get("top_markets"):
            return _state["top_markets"]
        return {
            "as_of": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "status": "unavailable",
            "note": "Kalshi's public markets feed did not answer just now.",
            "by_volume": [],
            "movers": [],
        }
    rows.sort(key=lambda r: r["volume_24h"], reverse=True)
    by_volume = rows[:10]
    # Movers: biggest yes-price move since the previous refresh, in
    # percentage points. 15-minute windows excluded (mechanical drift
    # to 0/1 near expiry, not a market move). Empty until two refreshes
    # exist (first refresh after a restart has no baseline).
    movers = []
    for r in rows:
        if r["series"].endswith("15M"):
            continue
        old = prev.get(r["ticker"])
        if old is None or r["yes_price_cents"] is None:
            continue
        change = round(r["yes_price_cents"] - old, 1)
        if change:
            movers.append({
                "ticker": r["ticker"],
                "series": r["series"],
                "title": r["title"],
                "yes_price_cents": r["yes_price_cents"],
                "change_pp": change,
            })
    movers.sort(key=lambda m: abs(m["change_pp"]), reverse=True)
    board = {
        "as_of": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "status": "ok",
        "source": "Kalshi public /markets — 24h volume is the exchange's own volume_24h_fp field",
        "pool": list(TOP_MARKETS_SERIES),
        "pool_note": (
            "Ranked across the tracked liquid series only. The unfiltered "
            "/markets list is flooded with 40,000+ zero-volume combo "
            "markets and cannot be ranked in one call."
        ),
        "by_volume": by_volume,
        "movers": movers[:5],
        "movers_note": (
            "Biggest yes-price moves since the previous refresh (~5 min). "
            "15-minute windows are excluded: their prices drift "
            "mechanically toward 0 or 1 as the window closes. Empty until "
            "two refreshes exist after a restart."
            if movers else
            "No movers yet — the board needs two refreshes after a restart "
            "to measure a move. 15-minute windows are excluded from movers: "
            "their prices drift mechanically toward 0 or 1 as the window "
            "closes."
        ),
        "cache_age_seconds": 0,
        "note": "Display only. No signal, no advice. Prices are the exchange's own public quotes.",
    }
    _state["top_markets"] = board
    _state["top_markets_ts"] = now
    _state["top_markets_prev"] = {
        r["ticker"]: r["yes_price_cents"]
        for r in rows if r["yes_price_cents"] is not None
    }
    return board


@app.get("/top-markets")
def top_markets():
    board = refresh_top_markets()
    if board.get("status") == "ok" and _state.get("top_markets_ts"):
        board = dict(board)
        board["cache_age_seconds"] = round(time.time() - _state["top_markets_ts"])
    return board


@app.get("/candles")
def candles(series: str = "KXBTC15M"):
    """1-hour tape: the last 60 1-minute candles.

    BTC/ETH/SOL/XRP/DOGE/BNB come from Coinbase's public Exchange API
    (spot, not Kalshi — no key). Gold (KXGOLD15M and daily KXGOLD) and
    silver (KXSILVER15M) come from this service's own stored spot
    samples — their feed (api.gold-api.com) publishes a price only, no
    candle history — so the tape is partial after a restart until an
    hour of samples exists, and the response says so. Cached ~45s; the
    data only moves once a minute, so Pulse polls it about that often.
    An unsupported series returns status "unavailable" and no candles —
    never another coin's data. (Fixed 2026-11-17: the old code silently
    returned BTC candles for any unknown series.)"""
    now = time.time()
    product = COINBASE_CANDLE_PRODUCTS.get(series)
    if product is not None:
        key = "candles_" + series
        ts_key = key + "_ts"
        if _state.get(key) and now - (_state.get(ts_key) or 0) < 45:
            data = _state[key]
        else:
            data = fetch_candles(product)
            if data:
                _state[key] = data
                _state[ts_key] = now
        return {
            "series": series,
            "source": "Coinbase public Exchange API — 1-minute spot candles, no key",
            "status": "ok" if data else "unavailable",
            "candles": data or [],
        }
    table = SAMPLE_TAPE_TABLES.get(series)
    if table is not None:
        key = "candles_" + series
        ts_key = key + "_ts"
        if _state.get(key) and now - (_state.get(ts_key) or 0) < 45:
            data = _state[key]
        else:
            data = candles_from_samples(table)
            if data:
                _state[key] = data
                _state[ts_key] = now
        payload = {
            "series": series,
            "source": "Stored spot samples — 1-minute OHLC built from this service's own sampling; gold-api publishes no candle history",
            "status": "ok" if data else "unavailable",
            "candles": data or [],
        }
        if data and len(data) < 60:
            payload["note"] = (
                f"Tape is partial — {len(data)} minute(s) since the last "
                "restart; it fills in over the next hour."
            )
        elif not data:
            payload["note"] = "No spot samples stored yet since the last restart."
        return payload
    return {
        "series": series,
        "source": "No candle feed for this series",
        "status": "unavailable",
        "note": (
            f"No candles for {series}: this endpoint serves BTC, ETH, SOL, "
            "XRP, DOGE and BNB via Coinbase, and gold/silver from stored "
            "spot samples."
        ),
        "candles": [],
    }


@app.get("/signals")
def signals(limit: int = 20, series: str = ""):
    conn = db()
    if series:
        rows = conn.execute(
            "SELECT window, ts, minute, direction, confidence, action, reason "
            "FROM signals WHERE window LIKE ? ORDER BY id DESC LIMIT ?",
            (series + "%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT window, ts, minute, direction, confidence, action, reason "
            "FROM signals ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [
        {
            "window": r["window"],
            "time": datetime.fromtimestamp(r["ts"], tz=timezone.utc).isoformat(),
            "minute": r["minute"],
            "direction": r["direction"],
            "confidence_pct": r["confidence"],
            "action": r["action"],
            "reason": r["reason"],
        }
        for r in rows
    ]


@app.get("/track-record")
def track_record(series: str = "KXBTC15M"):
    """Win rate graded on the FIRST call per window (2026-11-18).

    Defaults to the BTC 15-minute series; pass ?series=KXETH15M for ETH.
    Each series has its own record — validation never transfers across.
    The headline win rate grades the first ENTER at minute <= 9 — the
    call you could act on. The old last-signal-per-window rate is kept
    as a clearly-labeled diagnostic only; it is not the headline and
    not what validation judges.
    """
    conn = db()
    # Hot path reads the summary table (added 2026-11-17): one PK lookup,
    # ~1 row, however much history exists. Counting from the windows
    # table costs ~11 rows per settled window per call (~32k rows/call
    # at 30 days of history, growing ~1k/call/day) — at once-a-minute
    # polling that alone would break Turso's free 500M rows-read cap.
    # settle_window keeps the summary in step; the boot regrade seeds
    # it for a series with history but no row yet. If the row is missing
    # for any other reason, count live once and store the result.
    row = conn.execute(
        "SELECT settled, correct, last_settled, last_correct, grading "
        "FROM track_record_summary WHERE series = ?",
        (series,),
    ).fetchone()
    if row is None:
        settled, correct = _track_record_counts(conn, series)
        last_settled, last_correct = _track_record_counts_last_signal(
            conn, series
        )
        conn.execute(
            "INSERT OR IGNORE INTO track_record_summary (series, settled, "
            "correct, last_settled, last_correct, grading) "
            "VALUES (?, ?, ?, ?, ?, 'first')",
            (series, settled, correct, last_settled, last_correct),
        )
        conn.commit()
        grading = "first"
    else:
        if (row["grading"] or "last") != "first":
            # Boot regrade was missed (e.g. it failed once): heal it
            # now, then re-read. Runs at most once per series, ever.
            _regrade_series(conn, series)
            row = conn.execute(
                "SELECT settled, correct, last_settled, last_correct, grading "
                "FROM track_record_summary WHERE series = ?",
                (series,),
            ).fetchone()
        settled, correct = row["settled"], row["correct"]
        last_settled, last_correct = row["last_settled"], row["last_correct"]
        grading = row["grading"] or "last"
    conn.close()
    return {
        "series": series,
        "strategy": "momentum-v1",
        "status": "UNVALIDATED — needs 100+ paper trades + out-of-sample validation",
        "windows_settled": settled,
        "signals_correct": correct,
        "win_rate_pct": round(100 * correct / settled, 1) if settled else None,
        "grading": "first-call" if grading == "first" else "last-signal",
        "grading_note": (
            "Win rate grades the FIRST ENTER per window (minute <= 9) — "
            "the call you could act on. Windows with no actionable ENTER "
            "are not trades and are not counted."
            if grading == "first"
            else "Regrade pending: this row still carries the old "
            "last-signal grading. The first-call regrade runs at boot."
        ),
        "last_signal_win_rate_pct": (
            round(100 * last_correct / last_settled, 1) if last_settled else None
        ),
        "last_signal_note": (
            "Diagnostic only: the old last-signal-per-window rate, kept "
            "for comparison. It is not the headline and not what "
            "validation judges."
        ),
        "resolution_note": "Resolution judged from the final-minute spot average vs target — an approximation of the real CF Benchmarks BRTI settlement rule.",
    }


@app.get("/entries")
def entries(limit: int = 50, series: str = ""):
    """One row per entry alert: the first ENTER signal per (window, direction).

    Matches the alerts hook identity (window + direction): a signal that
    holds across minutes alerts once per window, so the first ENTER signal
    for that window+direction IS the alert, and its confidence is the
    percentage the user was told to enter at. Each row carries the window's
    eventual resolution, so a user can see whether the call they were
    alerted on held for a win. Defaults to both series; pass
    ?series=KXBTC15M or KXETH15M for one series.
    """
    limit = max(1, min(200, limit))
    conn = db()
    like = (series + "%",) if series else ("%",)
    # CROSS JOIN (added 2026-11-17): forces the windows-first join order
    # so the scan walks windows newest-first on idx_windows_close_ts and
    # probes signals per window on idx_signals_window_action_id — ~6ms
    # on a 30-day database vs ~2.4s for the planner's signals-first
    # order. Same rows, same order, same response shape.
    rows = conn.execute(
        "SELECT w.ticker, w.open_ts, w.close_ts, w.resolution, s.direction, "
        "s.confidence, s.minute, s.ts FROM windows w CROSS JOIN signals s "
        "WHERE w.ticker LIKE ? AND s.window = w.ticker AND s.action = 'ENTER' "
        "AND s.id = (SELECT MIN(id) FROM signals "
        "WHERE window = w.ticker AND direction = s.direction "
        "AND action = 'ENTER') "
        "ORDER BY w.close_ts DESC LIMIT ?",
        (like[0], limit),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        res = r["resolution"]
        if res is None:
            outcome = "pending"
        elif (r["direction"] == "UP" and res == "YES") or (
            r["direction"] == "DOWN" and res == "NO"
        ):
            outcome = "win"
        else:
            outcome = "loss"
        out.append(
            {
                "window": r["ticker"],
                "series": r["ticker"].split("-")[0],
                "opened": datetime.fromtimestamp(
                    r["open_ts"], tz=timezone.utc
                ).isoformat(),
                "closed": datetime.fromtimestamp(
                    r["close_ts"], tz=timezone.utc
                ).isoformat(),
                "direction": r["direction"],
                "confidence_pct": r["confidence"],
                "minute": r["minute"],
                "entered_at": datetime.fromtimestamp(
                    r["ts"], tz=timezone.utc
                ).isoformat(),
                "resolution": res,
                "outcome": outcome,
            }
        )
    return out


@app.get("/level-bounces")
def level_bounces(series: str = "", limit: int = 20):
    """What happened after price touched a key level — display only.

    Per-level hold rates (held vs broke, grouped by price bucket) plus
    the most recent decided touches. A level HELD if price was back on
    its own side 15 minutes after the touch (support: spot >= level;
    resistance: spot <= level); otherwise it BROKE. 'stale' touches
    (no decision within 6h) are never counted. Nothing here feeds the
    signal or the track record. Defaults to both series; pass
    ?series=KXBTC15M or KXETH15M for one series.
    """
    limit = max(1, min(100, limit))
    conn = db()
    try:
        levels, recent = _level_bounce_summary(conn, series, limit)
    finally:
        conn.close()
    return {
        "series": series or "all",
        "note": (
            "Display only: held = price was back on the level's own "
            "side 15 minutes after the touch; broke = it crossed "
            "through. Touches are sampled at the 10-minute levels "
            "cadence. Never feeds the signal or the track record."
        ),
        "levels": levels,
        "recent": recent,
    }


@app.get("/kalshi-extras")
def kalshi_extras(limit: int = 10, series: str = "KXBTC15M"):
    """Verified-available Kalshi extras for KXBTC15M — display only.

    The live window's parsed market fields (strike, 24h/total volume,
    open interest, last price — from the /markets fetch the tracker
    already makes, no extra polling) plus the last settled windows'
    stored snapshots (final volume/OI/result/settlement value and the
    100-trade tape sample). Nothing here feeds the signal or the track
    record. previous_price_dollars is intentionally absent: it read
    '0.0000' on every verified sample, open and settled."""
    limit = max(1, min(50, limit))
    conn = db()
    rows = conn.execute(
        "SELECT ticker, settled_at, volume_total, volume_24h, open_interest, "
        "last_price_cents, market_result, expiration_value, settlement_ts, "
        "trades_sample_n, trades_sample_contracts, "
        "trades_sample_yes_contracts, trades_sample_no_contracts, "
        "trades_sample_yes_vwap_cents, trades_sample_last_price_cents, "
        "trades_sample_newest_ts, trades_sample_oldest_ts, "
        "trades_sample_block_count "
        "FROM kalshi_window_extras WHERE ticker LIKE ? "
        "ORDER BY settled_at DESC LIMIT ?",
        (series + "%", limit),
    ).fetchall()
    conn.close()
    live = _state.get("kalshi_extras")
    return {
        "series": series,
        "status": "ok" if live else "waiting",
        "live_window": (
            dict(
                live,
                as_of=(
                    datetime.fromtimestamp(
                        _state["kalshi_extras_ts"], tz=timezone.utc
                    ).isoformat()
                    if _state.get("kalshi_extras_ts") else None
                ),
            )
            if live else None
        ),
        "settled_windows": [
            {
                "ticker": r["ticker"],
                "settled_at": (
                    datetime.fromtimestamp(r["settled_at"], tz=timezone.utc).isoformat()
                    if r["settled_at"] else None
                ),
                "volume_total": r["volume_total"],
                "volume_24h": r["volume_24h"],
                "open_interest": r["open_interest"],
                "last_price_cents": r["last_price_cents"],
                "market_result": r["market_result"],
                "expiration_value": r["expiration_value"],
                "settlement_ts": r["settlement_ts"],
                "trades_sample": {
                    "n": r["trades_sample_n"],
                    "contracts": r["trades_sample_contracts"],
                    "yes_contracts": r["trades_sample_yes_contracts"],
                    "no_contracts": r["trades_sample_no_contracts"],
                    "yes_vwap_cents": r["trades_sample_yes_vwap_cents"],
                    "last_price_cents": r["trades_sample_last_price_cents"],
                    "newest_ts": r["trades_sample_newest_ts"],
                    "oldest_ts": r["trades_sample_oldest_ts"],
                    "block_count": r["trades_sample_block_count"],
                },
            }
            for r in rows
        ],
        "notes": [
            "Display only. Nothing here feeds the v1 signal or the track record.",
            "The tape sample is the 100 most recent trades at settle time, not the full window tape.",
            "Strike is the market's own floor_strike; the signal still uses the computed target.",
        ],
    }


@app.get("/windows")
def windows(limit: int = 10, series: str = ""):
    conn = db()
    if series:
        rows = conn.execute(
            "SELECT ticker, open_ts, close_ts, target, target_provisional, "
            "resolution, strike_usd, volume_24h, open_interest, "
            "last_price_cents, market_result, expiration_value "
            "FROM windows WHERE ticker LIKE ? "
            "ORDER BY close_ts DESC LIMIT ?",
            (series + "%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ticker, open_ts, close_ts, target, target_provisional, "
            "resolution, strike_usd, volume_24h, open_interest, "
            "last_price_cents, market_result, expiration_value "
            "FROM windows ORDER BY close_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [
        {
            "ticker": r["ticker"],
            "opened": datetime.fromtimestamp(r["open_ts"], tz=timezone.utc).isoformat(),
            "closed": datetime.fromtimestamp(r["close_ts"], tz=timezone.utc).isoformat(),
            "target_usd": r["target"],
            "target_provisional": bool(r["target_provisional"]),
            "resolution": r["resolution"],
            "strike_usd": r["strike_usd"],
            "volume_24h": r["volume_24h"],
            "open_interest": r["open_interest"],
            "last_price_cents": r["last_price_cents"],
            "market_result": r["market_result"],
            "expiration_value": r["expiration_value"],
        }
        for r in rows
    ]


@app.get("/")
def root():
    return JSONResponse(
        {
            "service": "BTC Oracle Pro — public tracking API",
            "paper_only": True,
            "endpoints": ["/live", "/live/all", "/candles", "/signals", "/track-record", "/entries", "/windows", "/kalshi-extras", "/level-bounces", "/top-markets", "/health", "/app", "/me", "/app/live", "/app/callouts", "/auth/signup", "/auth/login", "/auth/logout", "/stripe/webhook"],
            "note": "Signals are UNVALIDATED v1 logic. Not financial advice. No live trading.",
        }
    )


# ------------------------------------------------------------ the /app frontend
# Static viewer (added 2026-11-17): api/static/index.html, served at
# /app. Mounted LAST so the /app/live and /app/callouts API routes
# above win over the static files — Starlette matches in registration
# order. Same origin as the API, so the login cookie works and no CORS
# change is needed. If the static directory is ever missing (a partial
# deploy), the service still starts and the API still works.
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/app", StaticFiles(directory=_STATIC_DIR, html=True), name="app")
