# BTC / ETH Market Intelligence — v0.3.0

**Status: analysis-first research dashboard, NOT a production-ready 23-feed trading system.** The v0.3.0 UI removes the price chart, surfaces active slow bias and fast timing scores, makes the provisional confluence decision visible with three explicit checks, and shows illustrative trade levels **only when both scores agree**. A compact, expandable source-coverage list replaces dozens of prominent unavailable labels. No Telegram alerts or trades are sent. Data comes from Kraken BTC/USD and ETH/USD spot (or Binance USDT pairs when accessible); 24-hour Kraken changes are approximate using hourly candles.

**Additional free context:** Alternative.me Fear & Greed, DefiLlama aggregate USD-pegged stablecoin supply change over 30 days, and mempool.space Bitcoin chain height / next halving. These update on a slower 10-minute cache because their underlying timelines are daily or multi-year. Failure of one provider does not fabricate data or prevent the spot analysis from running; it remains excluded. Sentiment and stablecoin supply have provisional 10-point slow-score weights; halving is context-only and always neutral.

## Local start

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://localhost:8000. The backend refreshes every 120 seconds. `/api/state` and `/api/health` expose JSON.

## Render deployment

Connect the separate GitHub repository `Nickcryptic/btc-trading-intelligence`, then use Render > New > Blueprint and select `render.yaml`. The v0.3.0 blueprint uses a **free** web service with **no paid persistent disk**. `MARKET_PROVIDER=kraken` avoids Binance's HTTP 451 region restriction on this Render host; set `MARKET_PROVIDER=auto` to try Binance first with a Kraken fallback. A free instance can spin down when idle: no 2-minute background collection or alert delivery occurs while it sleeps, and there may be a cold start when reopening the dashboard. Local SQLite files and in-memory decision snapshots on this tier are **ephemeral** and may be cleared on restart, redeploy or instance replacement. Keep one web worker so in-process monitoring does not duplicate polling. For 24/7 monitoring, use an always-on worker and a durable datastore after testing.

The free test blueprint creates a **public dashboard URL**. To protect the dashboard with HTTP Basic authentication, manually set `DASHBOARD_PASSWORD` in Render after deploying. The username is `trader`; never commit the password. Health endpoint remains accessible for Render's service checks. Telegram variables are not provisioned in the free test blueprint because no alerts are implemented.

## Critical caveats

- The agreed design includes **23 monitoring categories**, of which **Wintermute is monitoring-only**. Some need specialist paid/licensed data; others await an independently verifiable free or public integration. All pending categories are retained inside the expanded coverage section, not guessed.
- Market bias and timing use only a **subset** of connected indicators and **are not backtested or validated**. The three confluence gates are active for paper observation; they are not production-grade signal confirmation. Candidate levels are illustrative, not actionable recommendations.
- Recent-trade delta is **not full historical CVD**. Funding, OI and depth snapshots alone do not establish a predictive edge.
- FVGs are 15-minute candle patterns, not guaranteed support/resistance. CME gap data is **not connected**.
- No verified live whale attribution, IBIT ETF flow, liquidation map, on-chain MVRV, long-term holder, DXY or Nasdaq integration yet. A live result is never invented for an unconnected provider.
- Wintermute logs the last 10 five-minute checks as **UNAVAILABLE**, not BUY/SELL, until a verified wallet data service is connected; Wintermute is excluded from both scores.
- Telegram credentials can later be set via `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, but alerts are deliberately **not implemented or enabled** until signal validation and de-duplication are completed.
- The signal database schema is provisioned but confirmed trade history remains empty until a validated engine is implemented. The dashboard instead shows the last ten **five-minute paper snapshots** of each symbol's bias/timing/direction; they are held in memory and can reset. Wintermute observation checks remain in SQLite on ephemeral storage.
- `ALERTS_ENABLED` must remain `false`: there is no validated Telegram signal broadcaster in this release. Never interpret displayed candidate prices as actual instructions to trade.
- Never expose tokens in repository files. For reliable historical tracking use Render Postgres or persistent disk; ephemeral filesystem storage will reset on redeploy.
