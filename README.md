# BTC / ETH Market Intelligence — v0.7.0

**Status: unvalidated research / paper-signal dashboard, not an established profitable strategy.** v0.7.0 broadens liquidity depth and replaces permissive score-only trade candidates with a conservative **six-check trend, price-action, volume and risk filter**. No verified win rate, profitable-trader execution feed, historical backtest, Telegram trading alert or brokerage execution is provided. BTC/ETH near-market data comes from Kraken spot (or Binance when accessible). The independent short-term liquidity pressure monitor remains outside all trade-scoring decisions.

## v0.7.0 — distant liquidity and stricter trade setups

- The *distant* book separately fetches up to **1,000 public Bybit spot L2 price levels per side** (BTC/USDT or ETH/USDT). Failed, invalid or stale Bybit snapshots fall back to **up to 500 Kraken spot levels per side** (or 1,000 Binance levels when Binance is the configured primary source). The primary Kraken near-price ladder and pressure monitor still use the nearest 100 levels, independently.
- The map shows only **actual resting bids 0.5%–5% below** and **actual asks 0.5%–5% above** the *book's own* spot midprice, aggregated into **0.25% bands**. Each side lists up to six largest measured bands with observed price range, total notional, distance and multiple vs its median observed band. **No sufficiently distant data = no verified wall**, even when a user would like to see a particular price.
- Bybit USDT notionals are labelled as USDT, **not USD**; books from different exchanges are never combined into fake aggregate depth. A separate *potential swing-sweep target* view uses actual 1H/4H candle swing highs/lows **but does not claim those price levels contain resting limit orders, stop losses or liquidations**.
- **Slow score (1D/4H)** considers a daily closed-candle vs 200D average (30 points), the 50D/200D trend (25) and closed 4H EMA21 direction/slope (45). **Fast score (1H/15M)** uses 1H EMA21/EMA55 and slope (35), 15M EMA9/EMA21 (30), RSI zone (20), and 1H momentum (15). The scores are directional rule counts, **not predictive probabilities**. Fear & Greed, stablecoin supply, funding/OI, visible depth and sampled taker flow stay visible as context but do **not** secretly enter the trading scores.
- A candidate is possible only with *all six* gates: (1) aligned daily/4H regime, (2) aligned 1H/15M trend and non-extreme RSI, (3) last **completed** 15M breakout, breakdown, sweep/reclaim or trend-pullback entry trigger, (4) volume ≥**1.15×** the median of the prior 20 completed 15M bars, (5) ATR/spread/price-distance filters that reject chasing moves, and (6) volatility-aware stop and ≥1.5R room before a repeated opposing swing level. Otherwise the result is **NO TRADE** or a non-actionable directional watch.
- The five-row signal journal now tags new entries **v0.7.0** and old entries **LEGACY** without erasing their prior paper observations. The log still uses periodic sampled price milestones rather than verified executions and remains ephemeral on the free Render tier.
- The rule choices are hypotheses; **tests of mathematical behavior are not evidence of profitability**. Validate with forward observations and proper out-of-sample historical testing (including costs, slippage and ambiguous intrabar TP/SL ordering) before considering live capital.

Sources: [Bybit V5 REST order book](https://bybit-exchange.github.io/docs/v5/market/orderbook) and [Kraken API market depth documentation](https://blog.kraken.com/product/api/unlocked-3-the-market-data-feeds-systematic-traders-use).

## Previous v0.6.0 near-price liquidity map

- The original 0.05% near-price buckets are retained for the independent short-term pressure monitor. The user-facing liquidity price map now uses the wider verified-depth view above and does not masquerade as a futures forced-liquidation heatmap.

## New v0.5.0 liquidity monitor

- Each successful 2-minute refresh captures up to 100 visible spot limit-order levels per side and up to 1,000 recent exchange trades. Each trade's timestamp, price, and reported taker side are used to calculate an **observed, not full-stream, 120-second taker buy/sell notional**. The REST tape is marked `PARTIAL` and excluded from the pressure score when it cannot cover the 120-second window sufficiently.
- At least two fresh snapshots are required. The monitor compares visible **bid and ask depth changes**, **bid-share imbalance**, **taker-buy share when coverage is complete**, and approximate **persistence of the largest price bands**. Values are heuristics with unvalidated weights; a screen can show `BUY PRESSURE`, `SELL PRESSURE`, `MIXED / NEUTRAL`, `LIQUIDITY WITHDRAWING`, or `WARMING UP`.
- `POSSIBLE BUY ABSORPTION` requires substantial observed aggressive selling, price movement within ±0.2%, remaining bid depth and an observed persistent bid wall. Sell absorption is symmetric. Two-minute snapshots **cannot prove that orders were cancelled, that a specific level absorbed trades, or that a hidden/iceberg order exists**; the absorption flag is a hypothesis only.
- The separate ten-entry liquidity history is **in-memory and resets on a restart or redeployment**. Comparisons across gaps longer than eight minutes or shorter than 45 seconds reset to `WARMING UP` instead of treating stale book states as real change. On free Render, monitoring stops while the service sleeps.
- No liquidity pressure threshold has been backtested. No additional confirmed trade signals, Telegram notifications, or changes to existing confluence scores are generated.

## Existing v0.4.0 panels

- The FVG timeframe buttons change only the spot imbalance-zone display. Timing scores continue using the original 15-minute inputs, avoiding unannounced score changes. A separate CME data note clarifies that CME futures session gaps cannot be calculated from 24/7 spot candles; the live CME gap feed remains unconnected.
- The near-price order-book depth uses the **nearest 100 visible L2 levels per side**, showing best levels, spread, bid/ask shares and a seven-level ladder. The pressure monitor uses eight 5-basis-point clusters. The independent distant map uses a separate deeper snapshot and does not alter the pressure score or trading gates.
- Each new candidate side transition creates a time-stamped paper signal entry; a candidate persisting in the same direction may be sampled again after at least **30 minutes**. Per-asset journal shows the latest five entries with entry, stop, TP1–3, and **sampled-price milestones** (tracking, TP1/2/3 seen, SL seen). Milestones are based on 2-minute spot observations, not actual execution, candle-path reconstruction, guaranteed fills, realized return, or validated signal accuracy.
- The signal log is stored in SQLite on the Render instance and recent rows are also cached in the browser's local storage after being viewed. On free Render, server history can be lost on redeployment, cold instance replacement, or restart; browser cache is device/browser-specific and not a durable shared database. The ten five-minute decision snapshots remain a distinct, in-memory contextual log.

**Additional free context:** Alternative.me Fear & Greed, DefiLlama aggregate USD-pegged stablecoin supply change over 30 days, and mempool.space Bitcoin chain height / next halving. These update on a slower 10-minute cache because their underlying timelines are daily or multi-year. Failure of one provider does not fabricate data or prevent the spot analysis from running. All are contextual and excluded from the new directional decision scores.

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
