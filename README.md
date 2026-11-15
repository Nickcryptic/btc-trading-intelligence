# BTC / ETH Market Intelligence — v0.4.0

**Status: analysis-first research dashboard, NOT a production-ready 23-feed trading system.** The v0.4.0 UI has no price chart. Slow bias and fast timing scores must agree before showing provisional LONG/SHORT candidate levels. It now includes an exact last-market-update timestamp beside the 2-minute refresh control, a **15M / 1H / 1D completed-candle FVG selector**, real spot order-book bid/ask depth and grouped **resting-liquidity clusters**, and a **five-entry test signal journal** below the risk framework. No Telegram alerts or trades are sent. Data comes from Kraken BTC/USD and ETH/USD spot (or Binance USDT pairs when accessible); 24-hour Kraken changes are approximate using hourly candles.

## New v0.4.0 panels

- The FVG timeframe buttons change only the spot imbalance-zone display. Timing scores continue using the original 15-minute inputs, avoiding unannounced score changes. A separate CME data note clarifies that CME futures session gaps cannot be calculated from 24/7 spot candles; the live CME gap feed remains unconnected.
- Order-book depth uses up to **100 visible L2 price levels per side** from the existing exchange API, showing best levels, spread in basis points, USD-notional bid/ask shares, and a seven-level price ladder. The cluster view groups those resting orders into **5-basis-point price bands**, then ranks the largest up to eight bands. These are spot limit-order clusters, **not predicted forced-liquidation clusters**. Resting orders can disappear at any moment.
- Each new candidate side transition creates a time-stamped paper signal entry; a candidate persisting in the same direction may be sampled again after at least **30 minutes**. Per-asset journal shows the latest five entries with entry, stop, TP1–3, and **sampled-price milestones** (tracking, TP1/2/3 seen, SL seen). Milestones are based on 2-minute spot observations, not actual execution, candle-path reconstruction, guaranteed fills, realized return, or validated signal accuracy.
- The signal log is stored in SQLite on the Render instance and recent rows are also cached in the browser's local storage after being viewed. On free Render, server history can be lost on redeployment, cold instance replacement, or restart; browser cache is device/browser-specific and not a durable shared database. The ten five-minute decision snapshots remain a distinct, in-memory contextual log.

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

Connect the separate GitHub repository `Nickcryptic/btc-trading-intelligence`, then use Render > New > Blueprint and select `render.yaml`. The blueprint uses a **free** web service with **no paid persistent disk**. `MARKET_PROVIDER=kraken` avoids Binance's HTTP 451 region restriction on this Render host; set `MARKET_PROVIDER=auto` to try Binance first with a Kraken fallback. A free instance can spin down when idle: no 2-minute background collection or alert delivery occurs while it sleeps, and there may be a cold start when reopening the dashboard. Local SQLite files and in-memory decision snapshots on this tier are **ephemeral** and may be cleared on restart, redeploy or instance replacement. Keep one web worker so in-process monitoring does not duplicate polling. For 24/7 monitoring, use an always-on worker and a durable datastore after testing.

The free test blueprint creates a **public dashboard URL**. To protect the dashboard with HTTP Basic authentication, manually set `DASHBOARD_PASSWORD` in Render after deploying. The username is `trader`; never commit the password. Health endpoint remains accessible for Render's service checks. Telegram variables are not provisioned in the free test blueprint because no alerts are implemented.

## Critical caveats

- The agreed design includes **23 monitoring categories**, of which **Wintermute is monitoring-only**. Some need specialist paid/licensed data; others await an independently verifiable free or public integration. All pending categories are retained inside the expanded coverage section, not guessed.
- Market bias and timing use only a **subset** of connected indicators and **are not backtested or validated**. The three confluence gates are active for paper observation; they are not production-grade signal confirmation. Candidate levels are illustrative, not actionable recommendations.
- Recent-trade delta is **not full historical CVD**. Funding, OI and depth snapshots alone do not establish a predictive edge.
- FVGs are 15M, 1H, or 1D three-candle spot patterns, not guaranteed support/resistance. CME futures gap data is **not connected**.
- No verified live whale attribution, IBIT ETF flow, liquidation map, on-chain MVRV, long-term holder, DXY or Nasdaq integration yet. A live result is never invented for an unconnected provider.
- Wintermute logs the last 10 five-minute checks as **UNAVAILABLE**, not BUY/SELL, until a verified wallet data service is connected; Wintermute is excluded from both scores.
- Telegram credentials can later be set via `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, but alerts are deliberately **not implemented or enabled** until signal validation and de-duplication are completed.
- The signal journal captures at most the last five **paper candidate observations per selected symbol** in the UI. Signal results are sampled spot price milestones, not trade confirmations; no validated execution history is created. The separate last ten **five-minute paper snapshots** of each symbol's bias/timing/direction are held in memory and can reset. Wintermute observation checks remain in SQLite on ephemeral storage.
- `ALERTS_ENABLED` must remain `false`: there is no validated Telegram signal broadcaster in this release. Never interpret displayed candidate prices as actual instructions to trade.
- Never expose tokens in repository files. For reliable historical tracking use Render Postgres or persistent disk; ephemeral filesystem storage will reset on redeploy.
