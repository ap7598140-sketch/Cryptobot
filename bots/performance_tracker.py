"""
Bot 6 -- Performance Tracker (Sonnet).
Runs every 2h. Writes all trades to data/trades.csv.
Detects strategy decay, triggers A/B param switching.
"""
import asyncio
import csv
import logging
import os
from datetime import datetime, timezone

from core.claude_client import ClaudeClient
from core.regime_cache import get_params, save_params, get_better_variant
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import TRADES_CSV, PERF_TRACKER_INTERVAL_S, DECAY_LOOKBACK, DECAY_WIN_RATE_FLOOR

logger = logging.getLogger(__name__)

TRADES_HEADERS = [
    "timestamp", "coin", "direction", "strategy",
    "entry", "exit", "qty", "usd_value",
    "pnl_pct", "pnl_usd", "win", "score", "ab_variant",
]


def _ensure_csv():
    os.makedirs(os.path.dirname(TRADES_CSV), exist_ok=True)
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=TRADES_HEADERS).writeheader()


def append_trade(trade: dict):
    _ensure_csv()
    row = {h: trade.get(h, "") for h in TRADES_HEADERS}
    with open(TRADES_CSV, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=TRADES_HEADERS).writerow(row)


def _load_recent(n: int) -> list[dict]:
    if not os.path.exists(TRADES_CSV):
        return []
    rows = []
    with open(TRADES_CSV, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows[-n:]


def _analysis_prompt(recent: list[dict], params: dict) -> str:
    total    = len(recent)
    wins     = sum(1 for r in recent if r.get("win") == "True")
    win_rate = wins / total * 100 if total else 0
    avg_pnl  = sum(float(r.get("pnl_pct", 0)) for r in recent) / max(total, 1)
    variants: dict = {}
    for r in recent:
        v = r.get("ab_variant", "A")
        variants.setdefault(v, {"wins": 0, "total": 0})
        variants[v]["total"] += 1
        if r.get("win") == "True":
            variants[v]["wins"] += 1
    return (
        f"Performance review -- last {total} trades.\n"
        f"win_rate={win_rate:.1f}% avg_pnl={avg_pnl:.2f}%\n"
        f"ab_variants={variants} current_params={params}\n"
        f"Identify decay, suggest RSI threshold adjustments if needed.\n"
        f"Return: {{\"decay\":true/false,\"action\":\"brief\","
        f"\"rsi_oversold\":30,\"rsi_overbought\":70,\"switch_variant\":true/false}}"
    )


class PerformanceTracker:
    def __init__(self, claude: ClaudeClient, telegram: TelegramNotifier):
        self.claude   = claude
        self.telegram = telegram
        _ensure_csv()

    async def run(self):
        logger.info("Performance Tracker started -- interval %ds", PERF_TRACKER_INTERVAL_S)
        while not state.stop_command:
            try:
                await self._track()
            except Exception as e:
                logger.error("Performance Tracker error: %s", e)
            await asyncio.sleep(PERF_TRACKER_INTERVAL_S)

    async def _track(self):
        recent = _load_recent(DECAY_LOOKBACK)
        if not recent:
            return

        params   = get_params()
        s_result = await self.claude.sonnet(_analysis_prompt(recent, params),
                                            bot="perf_tracker")
        if not s_result or not isinstance(s_result, dict):
            return

        if s_result.get("decay"):
            wins     = sum(1 for r in recent if r.get("win") == "True")
            win_rate = wins / len(recent) * 100 if recent else 0
            await self.telegram.strategy_decay_warning(win_rate, s_result.get("action", ""))
            logger.warning("Strategy decay -- win rate %.1f%%", win_rate)

        if s_result.get("switch_variant"):
            params["ab_variant"] = get_better_variant()
            logger.info("A/B switching to variant %s", params["ab_variant"])

        new_os = int(s_result.get("rsi_oversold",  params.get("rsi_oversold", 30)))
        new_ob = int(s_result.get("rsi_overbought", params.get("rsi_overbought", 70)))
        if new_os != params.get("rsi_oversold") or new_ob != params.get("rsi_overbought"):
            params["rsi_oversold"]  = new_os
            params["rsi_overbought"] = new_ob
            logger.info("Updated RSI: oversold=%d overbought=%d", new_os, new_ob)

        save_params(params)
