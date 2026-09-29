# ANCHOR

Bitget USDT-M futures agent (BTC, ETH, SOL). Survive first, then grow.

- **Brain:** GitHub Actions, scans every 30s, restarts itself every ~50 min.
- **Relay:** `relay/` on Vercel (Dublin) forwards signed Bitget calls.
- **Strategy:** 1h EMA50 trend + 15m EMA20 pullback, post-only limit entry,
  1.5 ATR stop and 2R take-profit placed on Bitget with every entry, 2 ATR trail after +1R.
- **Risk:** 1.5% per trade, 5x cap, max 2 open, max 8 entries/day,
  -6% day = pause, equity <= $28 = close all + HALT.

## Controls
- `MODE` = `demo` or `live`
- `HALT` file = stop. Delete it to restart (cron picks up within ~20 min).
- `params.json` = rules (no strategy changes before 30 closed trades).
- `journal.md` / `trades.jsonl` = every trade with fees and funding.

## Secrets
`RELAY_URL`, `DEMO_KEY`, `DEMO_SECRET`, `DEMO_PASSPHRASE`, `TG_TOKEN`, `TG_CHAT`
(later `LIVE_KEY`, `LIVE_SECRET`, `LIVE_PASSPHRASE`; optional `VERCEL_BYPASS`).
