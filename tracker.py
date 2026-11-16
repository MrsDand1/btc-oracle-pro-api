"""BTC Oracle Pro — public tracking API (paper only, never live trades).

Tracks the live Kalshi 15-minute BTC window and serves it as public JSON:
current window, spot price, computed target, sigma, signals, and the
win rate counted from settled signals.

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

Run:  uvicorn tracker:app --host 0.0.0.0 --port 8000
Endpoints: /live, /live/all, /signals, /track-record, /windows, /health

Gold (v1, added Nov 16, 2026): the API also watches Kalshi's KXGOLD
series and a public gold spot feed (api.gold-api.com/price/XAU,
verified live). Gold is polled slowly — spot every 30s, markets every
60s — because the daily market barely moves and the keyless Kalshi rate
limit is undocumented (a burst of checks from one IP got a 429 on
2026-11-16). Gold v1 serves the live spot and, when a gold market is
open, its ticker/question/expiry/strike and live book. It serves NO gold
signal: the market's own YES price is the market's view, and no bot
signal is computed for gold until a gold strategy is built and validated.
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
KALSHI_ORDERBOOK = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_GOLD_MARKETS = (
    "https://api.elections.kalshi.com/trade-api/v2/markets"
    "?series_ticker=KXGOLD&limit=10&status=open"
)
GOLD_SPOT = "https://api.gold-api.com/price/XAU"
COINBASE_SPOT = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

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
    "target": None,
    "target_provisional": False,
    "sigma_1m": None,
    "last_signal": None,
    "levels": None,          # support/resistance from recent spot history
    "levels_ts": None,
    "book": None,            # {yes_bid_cents, yes_ask_cents, no_bid_cents, no_ask_cents}
    "book_ts": None,
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


def avg_spot(start_ts, end_ts):
    conn = db()
    row = conn.execute(
        "SELECT AVG(price) AS a, COUNT(*) AS n FROM spot_samples "
        "WHERE ts >= ? AND ts < ?",
        (start_ts, end_ts),
    ).fetchone()
    conn.close()
    return (row["a"], row["n"]) if row and row["n"] else (None, 0)


def current_window():
    """The open 15-min market closing soonest — never just the first row.

    The markets list order is not guaranteed, and at rollover the list can
    be momentarily empty (old window closed, new one not yet listed).
    An empty or unparseable list returns None instead of raising: a single
    bad tick must never kill the poll thread (that froze the API 2026-11-16).
    """
    try:
        data = fetch_json(KALSHI_MARKETS)
    except Exception as exc:  # noqa: BLE001 - surfaced in /health
        _state["errors"].append(f"kalshi {exc}")
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
                "open_ts": close_ts - 900,
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


def compute_levels(now):
    """Support/resistance from recent spot history (swing highs/lows).

    Builds 1-minute bars from spot_samples over the last 6 hours. A
    swing high is a 1-min high at least $5 above its 3 neighbours each
    side (the prominence bar keeps flat tops/bottoms from counting
    twice); swings within $25 cluster into one level, counted by
    touches. Round $500 levels are added as psychological levels
    (touches 0, kind "round"). Returns None when there is under 30
    minutes of history — levels need data to mean anything, and the
    free tier wipes history on restart, so early levels are thin by
    design.
    """
    try:
        conn = db()
        rows = conn.execute(
            "SELECT ts, price FROM spot_samples WHERE ts >= ? ORDER BY ts",
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
            if is_high and v - max(neigh) >= 5:
                out.append((minutes[i], v))
            if (not is_high) and min(neigh) - v >= 5:
                out.append((minutes[i], v))
        return out

    def cluster(points, is_high):
        levels = []
        for ts, price in sorted(points, key=lambda p: p[1], reverse=is_high):
            hit = None
            for lv in levels:
                if abs(lv["price_usd"] - price) <= 25:
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

    spot = _state["spot"]
    res = [lv for lv in cluster(swings(highs, True), True)
           if spot is None or lv["price_usd"] > spot]
    sup = [lv for lv in cluster(swings(lows, False), False)
           if spot is None or lv["price_usd"] < spot]
    res.sort(key=lambda lv: lv["price_usd"])
    sup.sort(key=lambda lv: lv["price_usd"], reverse=True)
    if spot:
        up = (int(spot // 500) + 1) * 500
        dn = int(spot // 500) * 500
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


def sigma_1m(now):
    """Std dev of spot over the last 60 seconds, in dollars."""
    try:
        conn = db()
        rows = conn.execute(
            "SELECT price FROM spot_samples WHERE ts >= ? ORDER BY ts", (now - 60,)
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


def settle_window(ticker):
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
    avg, n = avg_spot(close_ts - 60, close_ts)
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


def poll_forever():
    while True:
        now = time.time()
        # --- spot sample (every loop, ~5s)
        try:
            data = fetch_json(COINBASE_SPOT)
            price = float(data["data"]["amount"])
            _state["spot"] = price
            _state["spot_ts"] = now
            conn = db()
            conn.execute(
                "INSERT OR IGNORE INTO spot_samples (ts, price) VALUES (?, ?)",
                (now, price),
            )
            conn.execute("DELETE FROM spot_samples WHERE ts < ?", (now - 86400,))
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            _state["errors"].append(f"coinbase {exc}")

        # --- window (every loop; Kalshi call is cheap, rate limit is generous)
        win = current_window()
        if win:
            prev = _state["window"]
            if not prev or prev["ticker"] != win["ticker"]:
                # window rolled: settle the old one, start the new one
                if prev:
                    try:
                        settle_window(prev["ticker"])
                    except Exception as exc:  # noqa: BLE001
                        # A settle failure must never kill the poll thread
                        # (an uncaught settle error froze the API on
                        # 2026-11-16). The window stays unsettled; the
                        # error is visible in /health.
                        _state["errors"].append(f"settle {exc}")
                _state["window"] = win
                _state["target"] = None
                _state["target_provisional"] = False
                # target = 60s average before the window opened
                avg, n = avg_spot(win["open_ts"] - 60, win["open_ts"])
                if avg is not None and n >= 5:
                    target, prov = avg, False
                else:
                    # started mid-window: provisional from earliest samples
                    early, n2 = avg_spot(win["open_ts"], win["open_ts"] + 120)
                    target, prov = early, True
                if target is None and _state["spot"]:
                    # restarted after minute 2: no pre-open or early samples
                    # exist, so anchor provisionally to the live spot rather
                    # than leaving the whole window targetless.
                    target, prov = _state["spot"], True
                _state["target"] = target
                _state["target_provisional"] = prov
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
            _state["sigma_1m"] = sigma_1m(now)

            # --- v1 signal, logged once per minute (never after minute 12)
            minute = (now - win["open_ts"]) / 60.0
            if minute <= 12 and _state["spot"] and _state["target"]:
                direction, conf, _, _ = direction_and_confidence(
                    _state["spot"], _state["target"], _state["sigma_1m"]
                )
                action = "ENTER" if conf and conf >= 60 else "HOLD"
                reason = (
                    f"spot {'above' if direction == 'UP' else 'below'} target "
                    f"by ${abs(_state['spot'] - _state['target']):.2f}"
                    + (" — confidence below 60, do not chase" if action == "HOLD" else "")
                )
                # note proximity to the nearest key level in the reason
                lv = _state.get("levels") or {}
                if direction == "UP" and lv.get("nearest_resistance_usd"):
                    gap = lv["nearest_resistance_usd"] - _state["spot"]
                    if 0 <= gap <= 50:
                        reason += (
                            f" — ${gap:.0f} below resistance "
                            f"${lv['nearest_resistance_usd']:,.0f}"
                        )
                elif direction == "DOWN" and lv.get("nearest_support_usd"):
                    gap = _state["spot"] - lv["nearest_support_usd"]
                    if 0 <= gap <= 50:
                        reason += (
                            f" — ${gap:.0f} above support "
                            f"${lv['nearest_support_usd']:,.0f}"
                        )
                last = _state["last_signal"]
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
                    _state["last_signal"] = {
                        "window": win["ticker"], "minute": minute,
                        "direction": direction, "confidence": conf,
                        "action": action, "reason": reason, "ts": now,
                    }
            # --- live Kalshi book (every ~10s, public orderbook_fp)
            if now - (_state.get("book_ts") or 0) >= 10:
                book = fetch_book(win["ticker"])
                if book:
                    _state["book"] = book
                    _state["book_ts"] = now
            # --- key levels (every ~60s; needs history to mean anything)
            if now - (_state.get("levels_ts") or 0) >= 60:
                _state["levels"] = compute_levels(now)
                _state["levels_ts"] = now
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


def live_btc_payload():
    win = _state["window"]
    spot = _state["spot"]
    target = _state["target"]
    sig = _state["sigma_1m"]
    now = time.time()
    direction, conf, _, _ = direction_and_confidence(spot, target, sig)
    return {
        "as_of": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "market": (
            {
                "series": "KXBTC15M",
                "ticker": win["ticker"],
                "question": "BTC price up in next 15 mins?",
                "closes_in_seconds": max(0, round(win["close_ts"] - now)),
                "minute_of_15": round((now - win["open_ts"]) / 60.0, 1),
            }
            if win
            else None
        ),
        "spot_usd": spot,
        "target_usd": target,
        "target_provisional": _state["target_provisional"],
        "target_note": "60-second spot average in the minute before the window opened (the market's strike definition). Provisional when the service started mid-window.",
        "spot_vs_target_usd": (
            round(spot - target, 2) if spot and target else None
        ),
        "spot_vs_target_pct": (
            round(100 * (spot - target) / target, 3) if spot and target else None
        ),
        "above_target": (spot >= target) if spot and target else None,
        "sigma_1m_usd": round(sig, 2) if sig else None,
        "levels": _state.get("levels"),
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
                "yes_bid_cents": _state["book"]["yes_bid_cents"],
                "yes_ask_cents": _state["book"]["yes_ask_cents"],
                "no_bid_cents": _state["book"]["no_bid_cents"],
                "no_ask_cents": _state["book"]["no_ask_cents"],
                "age_seconds": (
                    round(now - _state["book_ts"]) if _state.get("book_ts") else None
                ),
            }
            if _state.get("book")
            else {
                "status": "LIVE DATA UNAVAILABLE",
                "note": "Kalshi's public orderbook returned no live bids right now.",
            }
        ),
    }


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
    return {"btc_15m": live_btc_payload(), "gold": live_gold_payload()}


@app.get("/signals")
def signals(limit: int = 20):
    conn = db()
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
def track_record():
    """Win rate counted from settled windows: last signal vs resolution."""
    conn = db()
    rows = conn.execute(
        "SELECT w.ticker, w.resolution, s.direction FROM windows w "
        "JOIN signals s ON s.window = w.ticker "
        "WHERE w.resolution IS NOT NULL "
        "AND s.id = (SELECT MAX(id) FROM signals WHERE window = w.ticker)"
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
        "strategy": "momentum-v1",
        "status": "UNVALIDATED — needs 100+ paper trades + out-of-sample validation",
        "windows_settled": settled,
        "signals_correct": correct,
        "win_rate_pct": round(100 * correct / settled, 1) if settled else None,
        "resolution_note": "Resolution judged from the final-minute spot average vs target — an approximation of the real CF Benchmarks BRTI settlement rule.",
    }


@app.get("/windows")
def windows(limit: int = 10):
    conn = db()
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
