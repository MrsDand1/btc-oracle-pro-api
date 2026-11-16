# BTC Oracle Pro — Public Tracking API

A small public API that tracks the live Kalshi 15-minute BTC window and
serves it as JSON anyone can read. Paper only — it never trades, never
holds keys, never touches an account.

## What it serves

- `GET /live` — current KXBTC15M window (ticker, countdown, minute of 15),
  live BTC spot price, computed target, spot vs target ($ and %),
  1-minute sigma, current signal, and an explicit Kalshi-book status.
- `GET /signals` — recent signals (direction, confidence, action, reason).
- `GET /track-record` — win rate counted from settled windows: each
  window's last signal vs how it actually resolved.
- `GET /windows` — recent windows with targets and resolutions.
- `GET /health` — uptime, spot freshness, recent errors.

## How the numbers are made (honest version)

- **Spot:** Coinbase public spot price, polled every 5 seconds.
- **Window:** Kalshi public API, polled every 30 seconds.
- **Target:** the 60-second average of spot in the minute before the
  window opens — that is how the market's strike is defined. If the
  service starts mid-window, the target is marked `provisional`.
- **Resolution:** judged from the final-minute spot average vs the
  target. That is an approximation of the real settlement rule, which
  uses the CF Benchmarks BRTI average — the API says so in its response.
- **Kalshi book prices:** NOT available. Kalshi's public API returns no
  bid/ask, volume, strike, or last price (verified Nov 16, 2026 —
  snapshots null, orderbooks empty, even for settled markets). The API
  says so instead of inventing numbers. Live book prices need a Kalshi
  API key (WebSocket).
- **Signals:** v1 momentum logic, UNVALIDATED. Nothing here is a
  validated strategy until it passes 100+ paper trades plus
  out-of-sample validation.
- **No money ledger in v1:** without live Kalshi prices there is no
  executable entry price, so the API tracks signal correctness
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
- **Rate limits are fine.** Kalshi's public API and Coinbase's public
  price feed are both keyless; this service polls well inside them.

## Later

This service can become the dashboard's live feed: point the
dashboard's data URL at this API and the two stop being separate
trackers. That wiring is a later step, after the API has run live
for a while.
