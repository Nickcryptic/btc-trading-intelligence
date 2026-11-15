# BTC / ETH Market Intelligence — v0.2.1

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

Connect the separate GitHub repository `Nickcryptic/btc-trading-intelligence`, then use Render > New > Blueprint and select `render.yaml`. The v0.2.1 blueprint uses a **free** web service with **no paid persistent disk**. A free instance can spin down when idle: no 2-minute background collection or alert delivery occurs while it sleeps, and there may be a cold start when reopening the dashboard. Local SQLite files on this tier are **ephemeral** and may be cleared on restart, redeploy or instance replacement. Keep one web worker so in-process monitoring does not duplicate polling. For 24/7 monitoring, use an always-on worker and a durable datastore after testing.

The free test blueprint creates a **public dashboard URL**. To protect the dashboard with HTTP Basic authentication, manually set `DASHBOARD_PASSWORD` in Render after deploying. The username is `trader`; never commit the password. Health endpoint remains accessible for Render's service checks. Telegram variables are not provisioned in the free test blueprint because no alerts are implemented.

## Critical caveats

- The original design includes **23 monitoring parameters**, of which **Wintermute is monitoring-only**. Many need paid/licensed data. Unconnected indicators are displayed as unavailable, not guessed.
- The initial market bias and timing scores use only a **subset** of connected indicators and **are not validated**. Their candidate levels are illustrative, not actionable signals.
- Recent-trade delta is **not full historical CVD**. Funding, OI and depth snapshots alone do not establish a predictive edge.
- FVGs are 15-minute candle patterns, not guaranteed support/resistance. CME gap data is **not connected**.
- No real-time whale attribution, IBIT ETF data, liquidation map, on-chain MVRV, long-term holder, DXY or Nasdaq integration yet. All 23 monitoring categories are listed; a live result is never invented for an unconnected provider.
- Wintermute logs the last 10 five-minute checks as **UNAVAILABLE**, not BUY/SELL, until a verified wallet data service is connected; Wintermute is excluded from both scores.
- Telegram credentials can later be set via `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, but alerts are deliberately **not implemented or enabled** until signal validation and de-duplication are completed.
- The signal database schema is provisioned but trade history will remain empty until a validated signal engine is implemented. Wintermute observations are stored using SQLite on ephemeral storage in the free test release; the last 10 may reset on restart.
- `ALERTS_ENABLED` must remain `false`: there is no validated Telegram signal broadcaster in this release. Never interpret displayed candidate prices as actual instructions to trade.
- Never expose tokens in repository files. For reliable historical tracking use Render Postgres or persistent disk; ephemeral filesystem storage will reset on redeploy.
