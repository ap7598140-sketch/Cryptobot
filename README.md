# Crypto Trading Bot

7-bot async crypto trading system using Alpaca paper trading and Claude AI.

## Architecture

| Bot | Model | Role |
|-----|-------|------|
| Market Scanner | Haiku | Scans coins every 60s |
| Trend Analyzer | Haiku -> Sonnet | Two-pass confirmation |
| Trade Executor | Sonnet | Sizes and places orders |
| Position Manager | Haiku | Stops, trailing exits |
| Risk Guard | Haiku | Daily loss, drawdown, reconciliation |
| Performance Tracker | Sonnet | CSV logging, A/B params |
| Overnight Analyst | Opus | 6am UTC regime classification |

## Setup

```bash
cp .env.example .env
pip install -r requirements.txt
python main.py
```

## Telegram Commands

- `/stop` -- immediately halt all bots
