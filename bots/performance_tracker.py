"""
Bot 6 — Performance Tracker (Sonnet)
Runs every 2 hours.
  - Reads data/trades.json
  - Tracks win rate, avg win/loss, per-coin, best/worst hours
  - Strategy decay: if win rate < 45% over last 20 trades → warning + reduce size
  - Rolling 30-day review
  - Feeds best/worst hours back to Risk Guard via state
"""
import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from core.claude_client import ClaudeClient
from core.regime_cache import record_trade_result
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import (
    TRADES_FILE, PERF_TRACKER_INTERVAL_S,
    DECAY_LOOKBACK, DECAY_WIN_RATE_FLOOR, LOGS_DIR,
)

logger = logging.getLogger(__name__)


def _load_trades() -> list[dict]:
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load trades: %s", e)
        return []


def _compute_stats(trades: list[dict]) -> dict:
    if not trades:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "avg_win_pct": 0, "avg_loss_pct": 0, "total_pnl_usd": 0,
            "per_coin": {}, "by_hour": {}, "best_hours": [], "worst_hours": [],
        }

    wins   = [t for t in trades if t.get("win")]
    losses = [t for t in trades if not t.get("win")]

    win_rate    = len(wins) / len(trades) if trades else 0
    avg_win_pct = sum(t.get("pnl_pct", 0) for t in wins) / max(len(wins), 1) * 100
    avg_loss_pct = sum(abs(t.get("pnl_pct", 0)) for t in losses) / max(len(losses), 1) * 100
    total_pnl   = sum(t.get("pnl_usd", 0) for t in trades)

    # Per-coin
    per_coin: dict = defaultdict(lambda: {"wins": 0, "total": 0, "pnl_usd": 0.0})
    for t in trades:
        coin = t.get("coin", "unknown")
        per_coin[coin]["total"] += 1
        if t.get("win"):
            per_coin[coin]["wins"] += 1
        per_coin[coin]["pnl_usd"] += t.get("pnl_usd", 0)

    per_coin_stats = {
        coin: {
            "win_rate": d["wins"] / max(d["total"], 1),
            "total": d["total"],
            "pnl_usd": round(d["pnl_usd"], 2),
        }
        for coin, d in per_coin.items()
    }

    # By hour
    by_hour: dict = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in trades:
        h = t.get("hour_utc", 0)
        by_hour[h]["total"] += 1
        if t.get("win"):
            by_hour[h]["wins"] += 1

    hour_rates = {
        h: d["wins"] / max(d["total"], 1)
        for h, d in by_hour.items()
        if d["total"] >= 3
    }
    sorted_hours = sorted(hour_rates, key=hour_rates.get, reverse=True)
    best_hours   = sorted_hours[:3]
    worst_hours  = sorted_hours[-3:] if len(sorted_hours) >= 3 else []

    return {
        "total":        len(trades),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(win_rate, 4),
        "avg_win_pct":  round(avg_win_pct, 2),
        "avg_loss_pct": round(avg_loss_pct, 2),
        "rr_ratio":     round(avg_win_pct / max(avg_loss_pct, 0.001), 2),
        "total_pnl_usd": round(total_pnl, 2),
        "per_coin":     per_coin_stats,
        "by_hour":      {str(h): round(v, 3) for h, v in hour_rates.items()},
        "best_hours":   best_hours,
        "worst_hours":  worst_hours,
    }


def _sonnet_prompt(stats: dict, recent_stats: dict) -> str:
    return (
        f"Crypto trading performance review. Return JSON summary.\n"
        f"All-time: trades={stats['total']} win_rate={stats['win_rate']*100:.1f}% "
        f"rr={stats['rr_ratio']:.2f} pnl=${stats['total_pnl_usd']:.2f}\n"
        f"Last {DECAY_LOOKBACK}: trades={recent_stats['total']} "
        f"win_rate={recent_stats['win_rate']*100:.1f}% rr={recent_stats['rr_ratio']:.2f}\n"
        f"Per-coin: {json.dumps(stats['per_coin'])}\n"
        f"Best hours UTC: {stats['best_hours']} Worst: {stats['worst_hours']}\n"
        f"Return: {{\"health\":\"good/warning/critical\","
        f"\"key_insight\":\"brief\","
        f"\"recommended_action\":\"brief\"}}"
    )


class PerformanceTracker:
    def __init__(self, claude: ClaudeClient, telegram: TelegramNotifier):
        self.claude   = claude
        self.telegram = telegram

    async def run(self):
        logger.info("Performance Tracker started — interval %ds", PERF_TRACKER_INTERVAL_S)
        while not state.stop_command:
            try:
                await self._track()
            except Exception as exc:
                logger.error("Performance tracker error: %s", exc)
            await asyncio.sleep(PERF_TRACKER_INTERVAL_S)

    async def _track(self):
        trades = await asyncio.to_thread(_load_trades)
        if not trades:
            logger.info("No trades to analyse yet")
            return

        all_stats    = _compute_stats(trades)
        recent       = trades[-DECAY_LOOKBACK:] if len(trades) >= DECAY_LOOKBACK else trades
        recent_stats = _compute_stats(recent)

        # ── Feed best/worst hours back to state ───────────────────────────────
        async with state._lock:
            state.best_hours  = all_stats["best_hours"]
            state.worst_hours = all_stats["worst_hours"]

        # ── Strategy decay check ──────────────────────────────────────────────
        recent_win_rate = recent_stats["win_rate"]
        decay_triggered = (
            len(recent) >= DECAY_LOOKBACK and recent_win_rate < DECAY_WIN_RATE_FLOOR
        )

        if decay_triggered and not state.strategy_decay:
            async with state._lock:
                state.strategy_decay = True
            action = f"Reducing position sizes by {int((1-state.size_multiplier)*100)}%"
            await self.telegram.strategy_decay_warning(recent_win_rate * 100, action)
            logger.warning("Strategy decay detected — win rate %.1f%%", recent_win_rate * 100)
        elif not decay_triggered and state.strategy_decay:
            async with state._lock:
                state.strategy_decay = False
            logger.info("Strategy decay cleared — win rate %.1f%%", recent_win_rate * 100)

        # ── Update A/B variant tracking ───────────────────────────────────────
        for t in recent:
            record_trade_result(t.get("win", False))

        # ── 30-day rolling filter ─────────────────────────────────────────────
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        trades_30d = [
            t for t in trades
            if datetime.fromisoformat(t.get("closed_at", "1970-01-01")).replace(tzinfo=timezone.utc) >= cutoff
        ]
        stats_30d = _compute_stats(trades_30d)

        # ── Claude insight (Sonnet) ───────────────────────────────────────────
        prompt  = _sonnet_prompt(all_stats, recent_stats)
        insight = await self.claude.sonnet(prompt, 150)
        if insight:
            logger.info("Performance insight: %s", insight.get("key_insight"))

        # ── Log report ────────────────────────────────────────────────────────
        report = {
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "all_time":     all_stats,
            "last_20":      recent_stats,
            "last_30d":     stats_30d,
            "decay":        decay_triggered,
            "size_mult":    state.size_multiplier,
            "insight":      insight,
        }
        _save_report(report)

    async def get_summary_for_opus(self) -> dict:
        """Called by Overnight Analyst so Haiku pre-summarises before Opus."""
        trades = await asyncio.to_thread(_load_trades)
        if not trades:
            return {"message": "No trades recorded"}

        # Last 24h only
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [
            t for t in trades
            if datetime.fromisoformat(t.get("closed_at", "1970-01-01")).replace(tzinfo=timezone.utc) >= cutoff
        ]
        stats = _compute_stats(recent)
        return {
            "trades_24h": len(recent),
            "win_rate_24h": stats["win_rate"],
            "pnl_24h_usd": stats["total_pnl_usd"],
            "per_coin": stats["per_coin"],
            "best_hours": stats["best_hours"],
        }


def _save_report(report: dict):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"perf_{ts}.json")
    try:
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
    except Exception as e:
        logger.error("Failed to save performance report: %s", e)
