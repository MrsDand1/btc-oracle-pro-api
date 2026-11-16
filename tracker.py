"""BTC Oracle Pro — public tracking API (paper only, never live trades).

Tracks the live Kalshi 15-minute BTC and ETH windows and serves them as
public JSON: current window, spot price, computed target, sigma, signals,
and the win rate counted from settled signals. Also watches gold (live
spot + the open KXGOLD market, no gold signal in v1).

Data reality (verified Nov 16, 2026; corrected the same day):
- Kalshi's public REST API returns the live window (ticker, close time).
  Market snapshots are all null (no volume, no last price), BUT the
  per-market orderbook endpoint DOES return live bids in `orderbook_fp`
  (yes_dollars/no_dollars bid ladders; YES ask = 1 - NO bid). The
  earlier "orderbooks are empty" finding read the legacy `yes`/`no`
  keys Kalshi removed on 2026-03-12. Live book prices need no key;
  volume and the live trade tape do (WebSocket, API key).
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

Run:  uvicorn tracker:app --host 0.0.0.0 --port 8000
Endpoints: /live, /live/all, /signals, /track-record, /windows, /health
"""

import json
import math
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

DB_PATH = "oracle.db"
KALSHI_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXBTC15M&limit=5&status=open"
)
KALSHI_ETH_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXETH15M&limit=5&status=open"
)
KALSHI_ORDERBOOK = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_GOLD_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXGOLD&limit=10&status=open"
)
GOLD_SPOT = "https://api.gold-api.com/price/XAU"
COINBASE_SPOT = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINBASE_ETH_SPOT = "https://api.coinbase.com/v2/prices/ETH-USD/spot"

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


# ---------------------------------------------------------------- db
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # WAL mode: readers (dashboard/API endpoints) must not block the
        # writer (poll loop). Lock contention froze the API 2026-11-16.
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS spot_samples (ts REAL PRIMARY KEY, price REAL);
        CREATE TABLE IF NOT EXISTS eth_spot_samples (ts REAL PRIMARY KEY, price REAL);
        CREATE TABLE IF NOT EXISTS windows (
            ticker TEXT PRIMARY KEY, open_ts REAL, close_ts REAL,
            target REAL, target_provisional INTEGER,
            resolution TEXT, resolved_at REAL
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, window TEXT, ts REAL,
            minute REAL, direction TEXT, confidence REAL,
            action TEXT, reason TEXT
        );
        """
    )
    conn.commit()
    conn.close()


init_db()


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
    "gold_spot": None,       # USD/oz from the public gold feed
    "gold_spot_ts": None,
    "gold_market": None,     # {ticker, question, close_ts, strike_usd}
    "gold_market_ts": None,
    "gold_book": None,
    "gold_book_ts": None,
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
        return {
            "yes_bid_cents": round(yes_bid * 100, 1),
            "yes_ask_cents": round((1 - no_bid) * 100, 1),
            "no_bid_cents": round(no_bid * 100, 1),
            "no_ask_cents": round((1 - yes_bid) * 100, 1),
        }
    except Exception:
        return None


def fetch_gold_spot():
    """Live gold spot (USD/oz) from a free public feed. Never raises."""
    try:
        data = fetch_json(GOLD_SPOT)
        return float(data["price"])
    except Exception:
        return None


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
                    "price_usd": round(price, 2), "touches": 1,
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
        res.append({"price_usd": float(up), "touches": 0,
                    "last_tested": None, "kind": "round"})
        sup.append({"price_usd": float(dn), "touches": 0,
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
    conn.execute(
        "UPDATE windows SET resolution = ?, resolved_at = ? WHERE ticker = ?",
        (resolution, time.time(), ticker),
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
    try:
        data = fetch_json(cfg["spot_url"])
        price = float(data["data"]["amount"])
        S[K["spot"]] = price
        S[K["spot_ts"]] = now
        conn = db()
        conn.execute(
            f"INSERT OR IGNORE INTO {cfg['spot_table']} (ts, price) VALUES (?, ?)",
            (now, price),
        )
        conn.execute(f"DELETE FROM {cfg['spot_table']} WHERE ts < ?", (now - 86400,))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        S["errors"].append(f"{cfg['name']} spot {exc}")

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
            if prev:
                try:
                    settle_window(prev["ticker"], cfg["spot_table"])
                except Exception as exc:  # noqa: BLE001
                    # A settle failure must never kill the poll thread
                    # (an uncaught settle error froze the API on
                    # 2026-11-16). The window stays unsettled; the
                    # error is visible in /health.
                    S["errors"].append(f"settle {exc}")
            S[K["window"]] = win
            S[K["target"]] = None
            S[K["target_provisional"]] = False
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
            conn = db()
            conn.execute(
                "INSERT OR IGNORE INTO windows "
                "(ticker, open_ts, close_ts, target, target_provisional) "
                "VALUES (?, ?, ?, ?, ?)",
                (win["ticker"], win["open_ts"], win["close_ts"],
                 target, 1 if prov else 0),
            )
            conn.commit()
            conn.close()
        S[K["sigma"]] = sigma_1m(now, cfg["spot_table"])

        # --- v1 signal, logged once per minute (never in the last 3 min)
        minute = (now - win["open_ts"]) / 60.0
        last_minute = cfg["window_secs"] / 60.0 - 3
        if minute <= last_minute and S[K["spot"]] and S[K["target"]]:
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
        # --- live Kalshi book (every ~10s, public orderbook_fp)
        if now - (S.get(K["book_ts"]) or 0) >= 10:
            book = fetch_book(win["ticker"])
            if book:
                S[K["book"]] = book
                S[K["book_ts"]] = now
        # --- key levels (every ~60s; needs history to mean anything)
        if now - (S.get(K["levels_ts"]) or 0) >= 60:
            S[K["levels"]] = compute_levels(
                now, S[K["spot"]], cfg["spot_table"],
                cfg["prominence"], cfg["cluster"], cfg["round_step"],
            )
            S[K["levels_ts"]] = now


def poll_forever():
    while True:
        now = time.time()
        for cfg in (BTC_CFG, ETH_CFG):
            try:
                poll_series(cfg, now)
            except Exception as exc:  # noqa: BLE001 - backstop; poll_series
                # already guards each step, so reaching here is unexpected.
                _state["errors"].append(f"{cfg['name']} poll {exc}")
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
    return {
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
            round(spot - target, 2) if spot and target else None
        ),
        "spot_vs_target_pct": (
            round(100 * (spot - target) / target, 3) if spot and target else None
        ),
        "above_target": (spot >= target) if spot and target else None,
        "sigma_1m_usd": round(sig, 2) if sig else None,
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


def live_btc_payload():
    return live_series_payload(BTC_CFG)


def live_eth_payload():
    return live_series_payload(ETH_CFG)


@app.get("/live")
def live():
    return live_btc_payload()


def live_gold_payload():
    """Gold v1: live spot + the open gold market and its book, if any.
    No bot signal — the market's own YES price is the market's view."""
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


@app.get("/live/all")
def live_all():
    return {
        "btc_15m": live_btc_payload(),
        "eth_15m": live_eth_payload(),
        "gold": live_gold_payload(),
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
    """Win rate counted from settled windows: last signal vs resolution.

    Defaults to the BTC 15-minute series; pass ?series=KXETH15M for ETH.
    Each series has its own record — validation never transfers across.
    """
    conn = db()
    rows = conn.execute(
        "SELECT w.ticker, w.resolution, s.direction FROM windows w "
        "JOIN signals s ON s.window = w.ticker "
        "WHERE w.resolution IS NOT NULL AND w.ticker LIKE ? "
        "AND s.id = (SELECT MAX(id) FROM signals WHERE window = w.ticker)",
        (series + "%",),
    ).fetchall()
    conn.close()
    settled = len(rows)
    correct = sum(
        1
        for r in rows
        if (r["direction"] == "UP" and r["resolution"] == "YES")
        or (r["direction"] == "DOWN" and r["resolution"] == "NO")
    )
    return {
        "series": series,
        "strategy": "momentum-v1",
        "status": "UNVALIDATED — needs 100+ paper trades + out-of-sample validation",
        "windows_settled": settled,
        "signals_correct": correct,
        "win_rate_pct": round(100 * correct / settled, 1) if settled else None,
        "resolution_note": "Resolution judged from the final-minute spot average vs target — an approximation of the real CF Benchmarks BRTI settlement rule.",
    }


@app.get("/windows")
def windows(limit: int = 10, series: str = ""):
    conn = db()
    if series:
        rows = conn.execute(
            "SELECT ticker, open_ts, close_ts, target, target_provisional, "
            "resolution FROM windows WHERE ticker LIKE ? "
            "ORDER BY close_ts DESC LIMIT ?",
            (series + "%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ticker, open_ts, close_ts, target, target_provisional, "
            "resolution FROM windows ORDER BY close_ts DESC LIMIT ?",
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
        }
        for r in rows
    ]


@app.get("/")
def root():
    return JSONResponse(
        {
            "service": "BTC Oracle Pro — public tracking API",
            "paper_only": True,
            "endpoints": ["/live", "/live/all", "/signals", "/track-record", "/windows", "/health"],
            "note": "Signals are UNVALIDATED v1 logic. Not financial advice. No live trading.",
        }
    )
