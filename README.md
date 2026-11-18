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
  That tape is partial on a fresh database until an hour of samples
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
  history exists — levels need data, so they start thin on a
  brand-new database. Signals note when spot is within $50 of the nearest
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

## Durable history (Turso, added Nov 17, 2026)

The free tier has no persistent disk, so the local SQLite file used to
start over on every restart or redeploy — the track record, entry log,
spot samples and Key Levels history all wiped. History now lives in a
Turso free database (SQLite-compatible libSQL) outside the service, so
restarts no longer wipe it.

- **Env vars (Render → service → Environment):**
`TURSO_DATABASE_URL` (e.g. `libsql://<name>-<org>.turso.io`) and
`TURSO_AUTH_TOKEN`. Both come from the Turso dashboard — create the
database there first, then paste the values into Render. Never put
the token in the repo.
- **Fallback:** when the env vars are absent, the service runs exactly
as before on the local `oracle.db` file (handy for local runs). A
`TURSO_DATABASE_URL` without a token refuses to start rather than
silently falling back — a silent fallback would reintroduce invisible
wiping.
- **Why Turso and not Render free Postgres:** Render's free Postgres
databases are deleted after ~90 days. Supabase free pauses after a
week idle (500 MB) and Neon free caps compute at 100 CU-hours/month
(an always-polling API could burn through it). Turso free: 5 GB,
500M rows read / 10M rows written per month, no card.
- **Row budget (measured on a 30-day bench database, Nov 17, 2026 —
sqld's own per-query read counts):** a naive client swap would have
blown the free cap — Key Levels alone read ~4,320 spot rows per
series per minute (~1.5B rows/month), and one unindexed
`/track-record` call read 199M rows. So levels refresh every 10
minutes instead of 60 seconds, sample pruning runs hourly instead of
every tick, and `/entries` forces the windows-first join order
(~6ms vs ~2.4s, ~95k rows/call down to a few hundred). Two indexes
back the hot queries (`signals(window, action, id)`,
`windows(close_ts)`). `/track-record` does NOT scan history per
call: a `track_record_summary` table holds the running (settled,
correct) counts per series, maintained by `settle_window` in the
same commit that writes the resolution (a window with no signals is
not counted, same as before) and seeded once at boot for a series
with history but no row yet. Steady-state budget ≈ 8.4M rows
written/month (~84% of the 10M cap — tight; the count assumes Turso
meters DELETEs as writes, which is unconfirmed) and ≈ 200M rows
read/month (~40% of the 500M cap) from the poll loop alone (levels
~149M + sigma ~50M), plus endpoint reads on top — `/track-record`
is now a 1-row lookup and `/entries` a few hundred rows, so ordinary
polling adds only tens of millions. If Turso ever reports the cap
close, the levers are the Developer plan $4.99/mo (25M writes,
2.5B reads) or the 10s sample cadence (6 samples/min still satisfies
the ≥5 target-rule samples).
- **What was NOT verified locally:** the embedded-replica mode of the
libsql client (the local dev server is too old to bootstrap it), so
the service connects directly to Turso Cloud; and real-Turso network
latency (localhost tests ran ~1–3ms per call).

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
- **History survives restarts (Turso).** The free tier has no
persistent disk, so the local SQLite file used to start over on
every restart or redeploy. History now lives in a Turso free
database (see "Durable history" above) — restarts no longer wipe
the track record, entry log, spot samples or Key Levels. The
service still sleeps after ~15 minutes idle; a VPS ($6–12/month)
remains the always-on fix for that, and is also where live trading
would eventually live.
-   **Rate limits need care.** Kalshi's keyed limits (Basic: 200 read /
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

## Accounts + VIP gate (added Nov 17, 2026)

Email + password accounts (stdlib `hashlib.scrypt`, no new
dependencies) and a VIP flag gated by the 100-window validation bar.
The web viewer is served from this same service at `/app`
(`static/index.html`, no build step). The public endpoints above are
unchanged and still need no login — the VM capture hook polls
`/live/all` with no login, and Pulse reads the public API.

- **Free tier:** BTC + ETH 15-minute live windows, signals,
  watchlist, sparklines, daily results note.
- **VIP tier:** additionally the timely callouts (`GET /app/callouts`
    — ENTER signals logged at minute 9 or earlier) and the full
    served markets. Polymarket BTC 5m and Broader Market are not
    served by this API yet, so they are in neither tier.
  - **The VIP lock:** `validation_unlocked` starts off. No account can
      hold VIP status until both series have 100+ settled windows
      (enforced in `POST /admin/unlock-validation`, which also grants VIP
      to every user whose subscription is already active). The owner
      flips the switch only after the 100-window backtest plus the
      out-of-sample review.
    - **Auth endpoints:** `POST /auth/signup` (18+ confirmation
        required), `POST /auth/login`, `POST /auth/logout`, `GET /me`.
        Sessions are random tokens stored hashed in the DB, in an HttpOnly
        SameSite=Lax Secure cookie, 30 days. There is no email
        verification and no self-serve password reset in v1 (no email
        service is connected); the owner resets passwords with
        `create_owner.py` / `reset_password.py` in the Render shell.
      - **Admin endpoints (owner account only):** `GET`
          /admin/validation-status, `POST /admin/unlock-validation`, `POST`
          /admin/grant-vip, `POST /admin/revoke-vip`, `POST`
          `/admin/link-stripe`. The first owner account is created with
          `create_owner.py` in the Render shell after deploy.
        - **Stripe Subscriptions is the payment rail** (webhook only — no
            product, no price, no checkout in this build). `POST`
            `/stripe/webhook` verifies the Stripe signature (stdlib HMAC) with
            the `STRIPE_WEBHOOK_SECRET` env var and flips the VIP flag on
            subscription active / cancelled / past-due. A subscription that
            becomes active while the gate is locked is recorded but grants no
            VIP until the unlock. `POST /billing/portal` creates a Stripe
            Customer Portal session (needs the `STRIPE_SECRET_KEY` env var)
            so a member can cancel in a couple of clicks.
          - **Cancellation policy:** cancel anytime, cancel-at-period-end —
              the member keeps VIP until the end of the period they paid for,
              and the webhook flips the flag off at period end. No refunds: no
              refund button, no refund endpoint, no automatic refunds. The
              no-refund + cancel-anytime terms are shown in the app's account
              panel and must be shown on the subscribe screen before payment.
              In the Stripe dashboard, set the portal's cancellation to "at the
              end of the billing period".
            - **Env vars (Render → service → Environment, both optional until
                configured):** `STRIPE_WEBHOOK_SECRET` (signing secret from the
                Stripe dashboard webhook endpoint), `STRIPE_SECRET_KEY` (account
                secret key, for portal sessions only). Neither is ever in the repo.
                Without them the webhook answers 503 and the portal answers 503;
                the rest of the service runs exactly as before.
              - **No balance or money display anywhere** in the app. No parlay,
                  promo, leaderboard, or copy-trading features. No push
                  notifications yet (separate step after the gate). No live trading
                  code, ever. The news-veto pop-up was dropped, not built.
