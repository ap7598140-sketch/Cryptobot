# Crypto Trading Bot System

7-bot async crypto trading system running on Alpaca paper trading with Claude AI.

## Architecture

All 7 bots run as concurrent `asyncio` tasks inside a single `main.py` orchestrator. Signal flow:

```
Market Scanner (Haiku, 60s)
    → [score ≥ 70] → Trend Analyzer Haiku
                        → [score ≥ 70] → Trend Analyzer Sonnet
                                            → [score ≥ 75] → Trade Executor (Sonnet)

Position Manager (Haiku, 30s) — monitors all open positions
Risk Guard       (Haiku, 30s) — hard gates, cannot be bypassed
Performance Tracker (Sonnet, 2h) — metrics, decay detection
Overnight Analyst   (Opus, 6am UTC) — regime, parameters
```

## The 7 Bots

| # | Bot | Model | Interval | Role |
|---|-----|-------|----------|------|
| 1 | Market Scanner | Haiku | 60 s | Batch-scan all active coins, forward signals ≥ 70 |
| 2 | Trend Analyzer | Haiku → Sonnet | On signal | Multi-TF confluence, 4h check, confirm ≥ 75 |
| 3 | Trade Executor | Sonnet | On signal | 2:1 R:R, limit orders, scaled entry |
| 4 | Position Manager | Haiku | 30 s | Trailing stops, partial exits, hard stops |
| 5 | Risk Guard | Haiku | 30 s | All hard gates, reconciliation |
| 6 | Performance Tracker | Sonnet | 2 h | Metrics, decay detection, A/B tracking |
| 7 | Overnight Analyst | Haiku + Opus | 6am UTC | Regime, params, morning briefing |

## Setup

### 1. Clone and install

```bash
cd ~/Desktop/crypto-bot-system
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in all keys
```

Required keys:
- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` — from app.alpaca.markets (paper account)
- `TELEGRAM_BOT_TOKEN` — create via @BotFather on Telegram
- `TELEGRAM_CHAT_ID` — send a message to your bot, then call: `https://api.telegram.org/bot<TOKEN>/getUpdates`

Optional (degrades gracefully if missing):
- `COINGLASS_API_KEY` — free tier at coinglass.com (liquidation levels)
- `COINGECKO_API_KEY` — free tier at coingecko.com (BTC dominance, stablecoin flow)

### 3. Run

```bash
python main.py
```

To stop: send `/stop` to your Telegram bot, or press `Ctrl+C`.

## Phased Coin Rollout

Edit `settings.py`:

```python
ROLLOUT_PHASE = 1   # 1 = BTC only | 2 = BTC+ETH | 3 = BTC+ETH+SOL
```

Phase schedule:
- **Phase 1** (weeks 1–2): BTC/USD only
- **Phase 2** (weeks 3–4): add ETH/USD
- **Phase 3** (week 5+): add SOL/USD

## Risk Parameters (settings.py)

| Parameter | Value | Description |
|-----------|-------|-------------|
| `PORTFOLIO_VALUE` | $1,000 | Paper money starting balance |
| `MAX_RISK_PER_TRADE` | 1.5% | Max portfolio risk per trade |
| `HARD_STOP_PCT` | 3% | Hard stop — no AI, instant execution |
| `TAKE_PROFIT_PCT` | 6% | Full take-profit level |
| `PARTIAL_EXIT_PCT` | 4% | Sell 50% of position at 4% profit |
| `DAILY_LOSS_LIMIT` | 5% | Stop trading until midnight UTC |
| `MAX_DRAWDOWN` | 15% | System pause from all-time high |
| `CASH_RESERVE_PCT` | 40% | Minimum cash always held |
| `MAX_POSITIONS` | 2 | Simultaneous open positions |
| `MIN_SONNET_SCORE` | 75 | Minimum confidence to execute |
| `MIN_REWARD_RISK` | 2.0 | Minimum 2:1 reward-to-risk ratio |

## Cost Optimisations

- Haiku handles all first-pass filtering — Sonnet only activates when Haiku scores ≥ 70
- All coin scans batched into a single Claude API call per cycle
- Market regime classified once at 6am UTC, cached to `data/regime_cache.json`, read by all bots
- Overnight Analyst: Haiku pre-summarises logs (~80% token reduction) before Opus receives them
- Performance Tracker runs every 2 hours, not continuously
- Max tokens enforced: scanner=150, analyzer=200, executor=250, others=150
- All prompts pass only what each bot actually needs (max 12 candles)

## File Structure

```
crypto-bot-system/
├── main.py                     # Orchestrator — all 7 bots as async tasks
├── settings.py                 # All configuration (edit ROLLOUT_PHASE here)
├── .env                        # Your API keys (never commit this)
├── .env.example                # Template
├── requirements.txt
├── core/
│   ├── state.py                # Shared trading state (thread-safe)
│   ├── claude_client.py        # Claude API wrapper
│   ├── alpaca_client.py        # Alpaca paper trading wrapper
│   ├── telegram_notifier.py    # Telegram sends + /stop handler
│   ├── market_data.py          # OHLCV, indicators, external APIs
│   └── regime_cache.py         # Market regime file cache + A/B params
├── bots/
│   ├── market_scanner.py       # Bot 1
│   ├── trend_analyzer.py       # Bot 2
│   ├── trade_executor.py       # Bot 3
│   ├── position_manager.py     # Bot 4
│   ├── risk_guard.py           # Bot 5
│   ├── performance_tracker.py  # Bot 6
│   └── overnight_analyst.py    # Bot 7
├── data/
│   ├── trades.json             # All closed trades
│   ├── regime_cache.json       # Today's regime (written at 6am UTC)
│   └── params.json             # A/B test parameters
└── logs/
    ├── system.log              # Full system log
    ├── scan_*.json             # Per-scan results
    ├── analysis_*.json         # Per-signal analyzer decisions
    ├── trade_*.json            # Per-trade execution records
    ├── position_*.json         # Position events (stop, exit, etc.)
    └── perf_*.json             # Performance tracker reports
```

## Telegram Commands

| Command | Effect |
|---------|--------|
| `/stop` | Immediately halt all 7 bots |

## Notifications Sent

| Event | Message |
|-------|---------|
| Trade opened | Coin, price, qty, size ($), confidence score |
| Take-profit hit | Coin, gain %, gain $ |
| Stop-loss fired | Coin, loss %, loss $ |
| Trailing stop triggered | Coin, locked-in profit % |
| Partial exit (4%) | Coin, qty sold, remaining |
| Daily loss limit | Loss %, trading paused until midnight |
| Drawdown circuit breaker | Drawdown %, system fully paused |
| 2 consecutive losses | Resume time |
| Reconciliation mismatch | Phantom/orphaned position details |
| Strategy decay | Win rate, action taken |
| Volatility spike | Trading paused |
| Morning briefing | Regime, params, P&L, win rate, positions, light |
| /stop received | All bots halted |

## External APIs Used

| API | Purpose | Auth |
|-----|---------|------|
| Alpaca (paper) | Trading + OHLCV data | API key required |
| Binance futures | Funding rates | None (public) |
| alternative.me | Fear & Greed Index | None (free) |
| Coinglass | Liquidation levels | Optional free key |
| CoinGecko | BTC dominance, stablecoin flow | Optional free key |
