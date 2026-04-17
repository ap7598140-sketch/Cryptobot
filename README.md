# Crypto Trading Bot

7-bot async crypto trading system using Alpaca paper trading and Claude AI.

## Architecture

| Bot | Model | Role |
|-----|-------|------|
| Market Scanner | Haiku | Scans coins every 60s, scores signals |
| Trend Analyzer | Haiku -> Sonnet | Two-pass signal confirmation |
| Trade Executor | Sonnet | Sizes and places split-entry orders |
| Position Manager | Haiku | Monitors stops, trailing exits, time limits |
| Risk Guard | Haiku | Daily loss, drawdown, reconciliation |
| Performance Tracker | Sonnet | Trade logging, A/B params, decay detection |
| Overnight Analyst | Opus | 6am UTC regime classification |

## Setup

```bash
cp .env.example .env
# Fill in your credentials
pip install -r requirements.txt
python main.py
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ALPACA_API_KEY` | Alpaca paper trading key |
| `ALPACA_SECRET_KEY` | Alpaca paper trading secret |
| `ANTHROPIC_API_KEY` | Claude API key |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |
| `COINGECKO_API_KEY` | CoinGecko key (optional) |

## Telegram Commands

- `/stop` -- immediately halt all bots

## Key Settings (`settings.py`)

- `ROLLOUT_PHASE` -- 1=BTC only, 2=+ETH, 3=+SOL
- `MIN_HAIKU_SCORE` / `MIN_SONNET_SCORE` -- signal thresholds (60/65)
- `MAX_RISK_PER_TRADE` -- 1.5% of portfolio per trade
- `DAILY_LOSS_LIMIT` -- 5% daily loss halts trading
- `MAX_DRAWDOWN` -- 15% from peak triggers circuit breaker

## Data

- `data/trades.csv` -- all closed trades
- `data/regime_cache.json` -- daily market regime (written 6am UTC)
- `logs/ai_responses/` -- every Claude response saved for audit
