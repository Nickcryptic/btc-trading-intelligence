# BTC / ETH Market Intelligence — v0.2.0

**Status: deployable first-stage dashboard, NOT a production-ready 23-feed trading system.** This release has a responsive UI, live Binance price/candle/depth/recent-trade data when accessible, 200D/50D trend, funding/OI (if Binance futures API accessible), 15M FVG detection, provisional two-score logic, and explicit unavailable indicators. It does **not** send trading alerts or place trades.

## Local start

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://localhost:8000. The backend refreshes every 120 seconds. `/api/state` and `/api/health` expose JSON.

## Render deployment

Connect the separate GitHub repository `Nickcryptic/btc-trading-intelligence`, then use Render > New > Blueprint and select `render.yaml`. The blueprint requests a **paid Starter service and a persistent disk**: check the total cost before confirming. Render free services can sleep and cannot guarantee 24/7 refresh. Keep one web worker so the in-process monitor does not duplicate polling. For production, move ingestion to an independent worker with a transactional datastore.

To protect the dashboard with HTTP Basic authentication, set `DASHBOARD_PASSWORD` in Render. The username is `trader`; never commit the password. Without this variable the dashboard is public. Health endpoint remains accessible for Render's service checks.

## Critical caveats

- The original design includes **23 monitoring parameters**, of which **Wintermute is monitoring-only**. Many need paid/licensed data. Unconnected indicators are displayed as unavailable, not guessed.
- The initial market bias and timing scores use only a **subset** of connected indicators and **are not validated**. Their candidate levels are illustrative, not actionable signals.
- Recent-trade delta is **not full historical CVD**. Funding, OI and depth snapshots alone do not establish a predictive edge.
- FVGs are 15-minute candle patterns, not guaranteed support/resistance. CME gap data is **not connected**.
- No real-time whale attribution, IBIT ETF data, liquidation map, on-chain MVRV, long-term holder, DXY or Nasdaq integration yet. All 23 monitoring categories are listed; a live result is never invented for an unconnected provider.
- Wintermute logs the last 10 five-minute checks as **UNAVAILABLE**, not BUY/SELL, until a verified wallet data service is connected; Wintermute is excluded from both scores.
- Telegram credentials can later be set via `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, but alerts are deliberately **not implemented or enabled** until signal validation and de-duplication are completed.
- The signal database schema is provisioned but trade history will remain empty until a validated signal engine is implemented. Wintermute observations are stored using SQLite on a Render persistent disk.
- `ALERTS_ENABLED` must remain `false`: there is no validated Telegram signal broadcaster in this release. Never interpret displayed candidate prices as actual instructions to trade.
- Never expose tokens in repository files. For reliable historical tracking use Render Postgres or persistent disk; ephemeral filesystem storage will reset on redeploy.
