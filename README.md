# BTC / ETH Market Intelligence — v0.6.0

**Status: analysis-first research dashboard, NOT a production-ready 23-feed trading system.** The v0.6.0 dashboard retains the chart-free Bias + Timing decision, individual indicators, FVG timeframes, live spot depth/clusters, five-entry test signal journal, and independent Liquidity Pressure & Absorption Monitor. The new liquidity price map makes visible spot buy and sell walls easy to locate. The liquidity monitor **does not influence the overall Market Bias, Execution Timing, LONG/SHORT decision, illustrative entry/TP/SL levels, or Telegram behavior.** No Telegram alerts or trades are sent. Data comes from Kraken BTC/USD and ETH/USD spot (or Binance USDT pairs when accessible); 24-hour Kraken changes are approximate using hourly candles.

## New v0.6.0 liquidity price map

- The liquidity card clearly identifies the **largest visible buy wall** and **largest visible sell wall** with their actual observed price range, USD-notional size, and signed percentage distance from spot. A current spot-price marker separates asks above from bids below when the ticker and book are aligned.
- Price bands are still 5 basis points (0.05%) wide, but their displayed range comes from **real resting limit-order prices in that band**, not just a synthetic bucket midpoint. The panel shows the top six bands on **each side** so a dominant bid book cannot hide all sell-side walls (and vice versa).
- The price map displays the current visible book coverage in USD. It is intentionally limited to up to 100 public spot price levels per side and **does not identify futures forced-liquidation prices, stop-loss concentrations, iceberg orders or guaranteed support/resistance**. It is a separate context display and does not alter any trading signal or liquidity-pressure score.

## New v0.5.0 liquidity monitor

- Each successful 2-minute refresh captures up to 100 visible spot limit-order levels per side and up to 1,000 recent exchange trades. Each trade's timestamp, price, and reported taker side are used to calculate an **observed, not full-stream, 120-second taker buy/sell notional**. The REST tape is marked `PARTIAL` and excluded from the pressure score when it cannot cover the 120-second window sufficiently.
- At least two fresh snapshots are required. The monitor compares visible **bid and ask depth changes**, **bid-share imbalance**, **taker-buy share when coverage is complete**, and approximate **persistence of the largest price bands**. Values are heuristics with unvalidated weights; a screen can show `BUY PRESSURE`, `SELL PRESSURE`, `MIXED / NEUTRAL`, `LIQUIDITY WITHDRAWING`, or `WARMING UP`.
- `POSSIBLE BUY ABSORPTION` requires substantial observed aggressive selling, price movement within ±0.2%, remaining bid depth and an observed persistent bid wall. Sell absorption is symmetric. Two-minute snapshots **cannot prove that orders were cancelled, that a specific level absorbed trades, or that a hidden/iceberg order exists**; the absorption flag is a hypothesis only.
- The separate ten-entry liquidity history is **in-memory and resets on a restart or redeployment**. Comparisons across gaps longer than eight minutes or shorter than 45 seconds reset to `WARMING UP` instead of treating stale book states as real change. On free Render, monitoring stops while the service sleeps.
- No liquidity pressure threshold has been backtested. No additional confirmed trade signals, Telegram notifications, or changes to existing confluence scores are generated.

## Existing v0.4.0 panels

- The FVG timeframe buttons change only the spot imbalance-zone display. Timing scores continue using the original 15-minute inputs, avoiding unannounced score changes. A separate CME data note clarifies that CME futures session gaps cannot be calculated from 24/7 spot candles; the live CME gap feed remains unconnected.
- Order-book depth uses up to **100 visible L2 price levels per side** from the existing exchange API, showing best levels, spread in basis points, USD-notional bid/ask shares, and a seven-level price ladder. The pressure monitor still uses the eight largest 5-basis-point bands globally; the new price map separately shows up to six strong bands **per side**. These are spot limit-order clusters, **not predicted forced-liquidation clusters**. Resting orders can disappear at any moment.
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
