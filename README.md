# BTC Oracle Pro — Public Tracking API

A small public API that tracks the live Kalshi 15-minute BTC window and
serves it as JSON anyone can read. Paper only — it never trades, never
holds keys, never touches an account.

## What it serves

- `GET /live` — current KXBTC15M window (ticker, countdown, minute of 15),
  live BTC spot price, computed target, spot vs target ($ and %),
  1-minute sigma, current signal, and an explicit Kalshi-book status.
- `GET /live/all` — the 15-minute BTC feed plus the ETH, gold, SOL,
  XRP, DOGE, BNB and silver 15-minute feeds plus the daily gold feed
  in one call (`btc_15m` + `eth_15m` + `gold` + `gold_15m` +
  `sol_15m` + `xrp_15m` + `doge_15m` + `bnb_15m` + `silver_15m`),
  for the Pulse viewer tabs.
- `GET /signals` — recent signals (direction, confidence, action,
  reason). Pass `?series=KXETH15M` for ETH only; no parameter returns
  both series.
- `GET /track-record` — win rate counted from settled windows: each
  window's last signal vs how it actually resolved. Defaults to the
  BTC 15-minute series; pass `?series=KXETH15M` for ETH. Each series
  keeps its own record — validation never transfers across series.
- `GET /entries` — one row per entry alert: the first ENTER signal per
  (window, direction), with that alert's confidence % and the window's
  eventual resolution/outcome (win / loss / pending). Pass
  `?series=KXETH15M` for ETH only; no parameter returns both series.
- `GET /windows` — recent windows with targets and resolutions.
- `GET /top-markets` — the most-traded Kalshi markets by public 24h
  volume, ranked across the tracked liquid series, plus biggest
  movers since the last refresh. Display only, no signal.
- `GET /health` — uptime, spot freshness, recent errors.

## How the numbers are made (honest version)

- **Spot:** Coinbase public spot price, polled every 5 seconds
  (BTC-USD and ETH-USD, each into its own history table).
- **Windows:** Kalshi public API — the open-market list is fetched
  every 30 seconds in steady state and every tick in the last minute
  before a window closes, so rollovers are caught without hammering
  the endpoint.
- **ETH 15-minute (KXETH15M):** the same tracker as BTC, run a second
  time on the ETH series — same 15-minute shape, same target rule
  (60-second pre-open average), same v1 momentum signal
  (UNVALIDATED, its own record), same live book and key levels, but
  scaled to ETH: swing prominence $2, cluster $10, round levels every
  $100, level-proximity notes within $10. Verified live Nov 16, 2026
  (KXETH15M open market + Coinbase ETH spot). Signals for both series
  land in the same `/signals` feed, so the alert hook picks up ETH
  ENTER signals automatically.
- **Target:** the 60-second average of spot in the minute before the
  window opens — that is how the market's strike is defined. If the
  service starts mid-window, the target is marked `provisional`.
- **Resolution:** judged from the final-minute spot average vs the
  target. That is an approximation of the real settlement rule, which
  uses the CF Benchmarks BRTI average — the API says so in its response.
- **Kalshi book prices:** LIVE. The per-market orderbook endpoint
  returns live bids in `orderbook_fp` (yes_dollars/no_dollars bid
  ladders; YES ask = 1 - NO bid), polled every ~10 seconds and served
  in `/live` as `kalshi_book` (YES/NO bid and ask in cents, plus the
  top-6 `yes_ladder`/`no_ladder` with sizes for the order-book panel).
  Corrected Nov 16, 2026 — the earlier "orderbooks empty" finding read the
  legacy `yes`/`no` keys Kalshi removed on 2026-03-12. Volume and open
  interest ARE in the public `/markets` response (`volume_24h_fp`,
  `volume_fp`, `open_interest_fp` — corrected Nov 17, 2026); only the
  live trade tape still needs a key (WebSocket) — the API never
  invents it.
- **1-hour tape (`/candles`):** the last 60 1-minute candles (spot,
  not Kalshi — no key), cached ~45s. BTC, ETH, SOL, XRP, DOGE and BNB
  (`?series=KXSOL15M` etc.) come from Coinbase's public Exchange API
  (verified live Nov 17, 2026 — SOL-USD candles return ~$73.74).
  Gold (`?series=KXGOLD15M` or `KXGOLD`) and silver
  (`?series=KXSILVER15M`) have no free candle feed anywhere — their
  feed (`api.gold-api.com`) publishes a price only — so their tape is
  built from this service's own stored spot samples (1-minute OHLC:
  open = first sample, high/low = max/min, close = last, no volume).
  That tape is partial after a restart until an hour of samples
  exists, and the response says so in a `note`. Feeds the 1-hour tape
  chart in Pulse. The candles are spot, not Kalshi market data —
  never presented as Kalshi's tape. Fixed Nov 17, 2026: an unknown
  series used to silently return BTC candles; it now returns status
  `unavailable` with a note naming the series — never another coin's
  data.
- **Key levels:** support/resistance computed from the last 6 hours
  of spot samples (swing highs/lows clustered within $25, plus round
  $500 psychological levels), served in `/live` as `levels` with
  touches and lookback minutes. Returns null until 30 minutes of
  history exists — levels need data, and the free tier wipes history
  on restart. Signals note when spot is within $50 of the nearest
  level in their reason.
- **Gold (v1):** live gold spot from the free public feed
  `api.gold-api.com/price/XAU` (verified live Nov 16, 2026), polled
  every 30 seconds, plus the open KXGOLD market when one exists
  (ticker, question, expiry, strike, live book), polled every 60
  seconds. No gold market was open on the evening of Nov 16, 2026 —
  the feed honestly reports "No gold market open right now" until
  one appears. Gold v1 serves NO signal: the market's own YES price
  is the market's view, and a gold signal needs its own strategy and
  validation run.
- **Gold 15-minute (KXGOLD15M, added Nov 17, 2026):** Kalshi runs
  15-minute gold markets alongside the daily KXGOLD series — verified
  live Nov 17, 2026 (KXGOLD15M-26NOV170730-30, "Gold price up in next
  15 mins?", Up 76¢ / Down 25¢). The daily feed alone left Pulse's
  gold tab empty while a 15-minute gold market was live, so the API
  now tracks KXGOLD15M the same way as BTC/ETH: live window, 60s
  pre-open target, 1-minute sigma, live book (polled ~60s), key
  levels (swing prominence $2, cluster $10, round levels every $50).
  NO gold signal, NO track-record writes, NO ledger for gold — the
  `gold_15m` entry in `/live/all` carries `signal: null` with a note
  saying so. The daily KXGOLD feed is unchanged; the two gold series
  coexist.
- **SOL / XRP / DOGE 15-minute (KXSOL15M, KXXRP15M, KXDOGE15M, added
  Nov 17, 2026):** feed-only, tracked exactly like gold 15-minute —
  live window, 60s pre-open target, 1-minute sigma, live book (polled
  ~60s), key levels scaled to each coin (SOL ~$74: prominence $0.10,
  cluster $0.50, round every $5; XRP ~$0.97: $0.002 / $0.01 / $0.10;
  DOGE ~$0.059: $0.0002 / $0.001 / $0.005 — all verified live Nov 17,
  2026). NO signal, NO track-record writes, NO ledger for any of them
  — a signal needs its own strategy and validation run. Dollar figures
  are rounded to decimals that fit the price scale (6 for sub-$1
  coins) so sigma and spot-vs-target never read 0.0.
- **BNB / silver 15-minute (KXBNB15M, KXSILVER15M, added Nov 17,
  2026):** feed-only, tracked exactly like gold 15-minute — live
  window, 60s pre-open target, 1-minute sigma, live book (polled
  ~60s), key levels scaled to each market (BNB ~$592: $0.50 / $2.50 /
  $25; silver ~$47: $0.02 / $0.10 / $1 — both verified live Nov 17,
  2026). Spot feeds are free and public: Coinbase BNB-USD for BNB,
  `api.gold-api.com/price/XAG` for silver (same feed family as the
  gold XAU feed). NO signal, NO track-record writes, NO ledger for
  either — a signal needs its own strategy and validation run.
  NEAR, HYPE, ZEC, ADA, BCH and TON 15-minute markets exist on Kalshi
  but are NOT tracked: their books are too thin (HYPE showed $0
  volume, NEAR $659 in the owner's screenshots) and each new tab adds
  validation debt. A market earns its own tab when its volume + a
  validation run justify it; the rest stay visible on Top Markets.
  The same earn-a-tab rule holds for WTI, Brent, nat gas, copper,
  coffee, platinum, palladium, all FX pairs, S&P/Nasdaq/Dow and
  Treasury yields, and for Coin Race / Crypto Comparison (relative
  markets, no single spot price, cannot train the same way).
- **Top Markets (`GET /top-markets`, added Nov 17, 2026):** the
  most-traded Kalshi markets by public 24h volume (`volume_24h_fp`
  in `/markets`, verified Nov 17, 2026), ranked across the tracked
  liquid series (BTC/ETH/SOL/XRP/DOGE/BNB/gold/silver 15-minute,
  gold + BTC daily, NFL/NBA/NCAAB/NHL games), plus biggest movers
  since the last
  refresh (~5 min). The unfiltered `/markets` list cannot be ranked in
  one call: it is flooded with 40,000+ zero-volume combo markets, so
  the board names its pool honestly. Movers exclude 15-minute windows
  (their prices drift mechanically toward 0 or 1 as the window
  closes) and are empty until two refreshes exist after a restart.
  Display only — no signal, no advice.
- **Signals:** v1 momentum logic, UNVALIDATED. Nothing here is a
  validated strategy until it passes 100+ paper trades plus
  out-of-sample validation.
- **No money ledger in v1:** the API tracks signal correctness
  (direction vs resolution), not dollars. The paper-money ledger lives
  in the BTC Oracle Pro Dashboard, where trades are logged by hand.

## Putting it live (Render, free tier)

1. Put these four files (`tracker.py`, `requirements.txt`, `render.yaml`,
   `README.md`) in a new GitHub repo.
2. In Render: New → Web Service → connect the repo. Render reads
   `render.yaml` and deploys it. Free tier, no card needed.
3. Your public API is then at `https://btc-oracle-pro-api.onrender.com/live`
   (the exact name depends on what you call the service).

## Free-tier limits to know

- **It sleeps when idle.** Render's free web services spin down after
  ~15 minutes with no traffic, so tracking pauses when nobody is
  watching. It wakes on the next request. The dashboard polling it
  every 5 seconds keeps it awake while the dashboard is open.
- **History resets on restart.** The free tier has no persistent disk,
  so the SQLite history (signals, win rate) starts over when the
  service restarts or redeploys. For permanent history, add a free
  Postgres (Neon or Supabase free tier) later, or move to the
  $6–12/month VPS already in the BTC Oracle Pro plan — that is the
  always-on fix.
- **Rate limits need care.** Kalshi's keyed limits (Basic: 200 read /
  100 write tokens/sec) are generous, but the keyless limits this
  service runs under are undocumented — a burst of checks from one IP
  got an HTTP 429 on Nov 16, 2026. The service polls gently (BTC spot
  every 5s, gold spot every 30s, gold markets every 60s) and stays
  well inside what it has seen work; verification calls from one
  machine should be spaced out, not fired in bursts.

## Later

This service can become the dashboard's live feed: point the
dashboard's data URL at this API and the two stop being separate
trackers. That wiring is a later step, after the API has run live
for a while.
