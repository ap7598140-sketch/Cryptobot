"""
Bot 7 — Overnight Analyst (Opus)
Runs once at 06:00 UTC.

Pipeline:
  1. Haiku pre-summarises all trade logs (reduces Opus input by ~80 %)
  2. Opus receives summary + external context
  3. Opus classifies market regime for the day
  4. Regime saved to cache; all bots read it
  5. A/B parameter testing: keeps better-performing variant
  6. Regime memory: pre-loads settings from last time this regime occurred
  7. Morning Telegram briefing sent
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from core.claude_client import ClaudeClient
from core.market_data import get_fear_and_greed, get_btc_dominance
from core.regime_cache import (
    get_regime, save_regime, regime_already_run_today,
    get_params, save_params, get_better_variant,
)
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import LOGS_DIR, TRADES_FILE

logger = logging.getLogger(__name__)


def _haiku_summarise_prompt(raw_trades: list[dict], raw_logs_sample: list[str]) -> str:
    trades_json = json.dumps(raw_trades[-30:], default=str)   # last 30 trades
    return (
        f"Summarise these crypto trading results into a compact JSON report. "
        f"Extract: total trades, wins, losses, win rate, total pnl, best coin, "
        f"worst coin, avg hold time, main exit reasons, and 3 key pattern observations.\n"
        f"Trades (last 30): {trades_json}\n"
        f"Return: {{\"summary\": {{...}} }}"
    )


def _opus_analysis_prompt(
    trade_summary: dict,
    fear_greed: dict,
    btc_dom: float,
    params: dict,
    macro_context: str,
    prev_regime_perf: dict,
) -> str:
    fg_val  = fear_greed.get("value", 50)
    fg_cls  = fear_greed.get("classification", "Neutral")
    variant = params.get("ab_variant", "A")
    a_wr    = params.get("variant_A_wins", 0) / max(params.get("variant_A_trades", 1), 1)
    b_wr    = params.get("variant_B_wins", 0) / max(params.get("variant_B_trades", 1), 1)

    return (
        f"Daily crypto trading analysis. Return comprehensive JSON.\n"
        f"Trade summary: {json.dumps(trade_summary)}\n"
        f"Fear&Greed: {fg_val} ({fg_cls})\n"
        f"BTC dominance: {btc_dom:.1f}%\n"
        f"Active param variant: {variant} | "
        f"A win_rate={a_wr*100:.1f}% ({params.get('variant_A_trades',0)} trades) | "
        f"B win_rate={b_wr*100:.1f}% ({params.get('variant_B_trades',0)} trades)\n"
        f"Current params: rsi_oversold={params.get('rsi_oversold',30)} "
        f"rsi_overbought={params.get('rsi_overbought',70)} "
        f"macd_signal={params.get('macd_signal_period',9)}\n"
        f"Prev regime performance: {json.dumps(prev_regime_perf)}\n"
        f"Macro notes: {macro_context}\n"
        f"Return: {{\n"
        f"  \"regime\": \"bull|bear|sideways\",\n"
        f"  \"green_light\": \"green|yellow|red\",\n"
        f"  \"fear_greed\": {fg_val},\n"
        f"  \"macro_events\": [\"...\"],\n"
        f"  \"best_variant\": \"A|B\",\n"
        f"  \"recommended_params\": {{\"rsi_oversold\":30,\"rsi_overbought\":70,\"macd_signal_period\":9}},\n"
        f"  \"regime_memory_notes\": \"...\",\n"
        f"  \"30d_pattern\": \"...\",\n"
        f"  \"morning_briefing\": \"...\"\n"
        f"}}"
    )


def _load_trades() -> list[dict]:
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _load_log_filenames() -> list[str]:
    if not os.path.exists(LOGS_DIR):
        return []
    return [f for f in os.listdir(LOGS_DIR) if f.endswith(".json")]


def _get_prev_regime_performance(trades: list[dict], regime: str) -> dict:
    """Find how the bot performed last time this regime was active."""
    regime_cache = get_regime()
    last_date    = regime_cache.get("last_regime_performance", {}).get(regime, {})
    if last_date:
        return last_date
    # Compute from trades
    regime_trades = [t for t in trades if t.get("regime") == regime]
    if not regime_trades:
        return {}
    wins = sum(1 for t in regime_trades if t.get("win"))
    return {
        "total": len(regime_trades),
        "win_rate": wins / max(len(regime_trades), 1),
        "total_pnl": sum(t.get("pnl_usd", 0) for t in regime_trades),
    }


class OvernightAnalyst:
    def __init__(self, claude: ClaudeClient, telegram: TelegramNotifier,
                 coingecko_key: str = ""):
        self.claude        = claude
        self.telegram      = telegram
        self.coingecko_key = coingecko_key

    async def run(self):
        logger.info("Overnight Analyst started — will run at 06:00 UTC daily")
        while not state.stop_command:
            try:
                now = datetime.now(timezone.utc)
                # Fire at 06:00 UTC if not already done today
                if now.hour == 6 and not regime_already_run_today():
                    await self._analyse()
                # Sleep 30 min then re-check
                await asyncio.sleep(1_800)
            except Exception as exc:
                logger.error("Overnight Analyst error: %s", exc)
                await asyncio.sleep(300)

    async def _analyse(self):
        logger.info("Overnight Analyst: starting morning analysis")
        trades = await asyncio.to_thread(_load_trades)
        log_files = await asyncio.to_thread(_load_log_filenames)

        # ── Step 1: Haiku pre-summarises trade logs ───────────────────────────
        trade_summary = {}
        if trades:
            haiku_prompt  = _haiku_summarise_prompt(trades, log_files)
            haiku_result  = await self.claude.overnight_haiku_call(haiku_prompt)
            if haiku_result and isinstance(haiku_result, dict):
                trade_summary = haiku_result.get("summary", haiku_result)
            logger.info("Haiku pre-summarisation complete")
        else:
            trade_summary = {"message": "No trades yet — first day running"}

        # ── Step 2: Fetch external context in parallel ────────────────────────
        fear_greed, btc_dom = await asyncio.gather(
            get_fear_and_greed(),
            get_btc_dominance(self.coingecko_key),
            return_exceptions=True,
        )
        if isinstance(fear_greed, Exception):
            fear_greed = {"value": 50, "classification": "Neutral"}
        if isinstance(btc_dom, Exception):
            btc_dom = 50.0

        # ── Step 3: Get params and previous regime performance ────────────────
        params = get_params()
        current_regime = get_regime().get("regime", "unknown")
        prev_perf = _get_prev_regime_performance(trades, current_regime)
        macro_context = _check_macro_context()

        # ── Step 4: Opus full analysis ────────────────────────────────────────
        opus_prompt = _opus_analysis_prompt(
            trade_summary, fear_greed, float(btc_dom),
            params, macro_context, prev_perf
        )
        opus_result = await self.claude.overnight_opus_call(opus_prompt)

        if not opus_result or not isinstance(opus_result, dict):
            logger.error("Opus analysis failed — using defaults")
            opus_result = {
                "regime": "sideways",
                "green_light": "yellow",
                "fear_greed": fear_greed.get("value", 50),
                "macro_events": [],
                "morning_briefing": "Analysis unavailable — proceed with caution",
            }

        # ── Step 5: Save regime cache ─────────────────────────────────────────
        regime_data = {
            "regime":        opus_result.get("regime", "sideways"),
            "green_light":   opus_result.get("green_light", "yellow"),
            "fear_greed":    opus_result.get("fear_greed", 50),
            "macro_events":  opus_result.get("macro_events", []),
            "notes":         opus_result.get("regime_memory_notes", ""),
            "last_regime_performance": {
                opus_result.get("regime", "sideways"): {
                    "notes": opus_result.get("regime_memory_notes", ""),
                }
            },
        }
        save_regime(regime_data)

        # Update state
        async with state._lock:
            state.market_regime = regime_data["regime"]

        # ── Step 6: Apply A/B parameter recommendation ────────────────────────
        best_variant = opus_result.get("best_variant", get_better_variant())
        rec_params   = opus_result.get("recommended_params", {})
        if rec_params:
            new_params = {**params, **rec_params, "ab_variant": best_variant}
            save_params(new_params)
            logger.info("Params updated: variant=%s rsi_oversold=%s",
                        best_variant, rec_params.get("rsi_oversold", params["rsi_oversold"]))

        # ── Step 7: Morning Telegram briefing ─────────────────────────────────
        briefing = _format_briefing(opus_result, trade_summary, state)
        await self.telegram.morning_briefing(briefing)
        logger.info("Morning briefing sent — regime=%s light=%s",
                    regime_data["regime"], regime_data["green_light"])


def _check_macro_context() -> str:
    """
    Very lightweight macro check — returns a short string.
    In production this could be expanded with economic calendar APIs.
    """
    now = datetime.now(timezone.utc)
    notes = []
    # Basic calendar placeholders — add real API calls here as needed
    if now.weekday() == 4:  # Friday
        notes.append("Friday: watch for weekend liquidity drop")
    if now.day <= 3:
        notes.append("Early month: potential macro data releases")
    return "; ".join(notes) if notes else "No notable macro events identified"


def _format_briefing(opus_result: dict, summary: dict, s) -> str:
    snap = {
        "portfolio": s.portfolio_value,
        "daily_pnl": s.daily_pnl,
        "open_positions": list(s.open_positions.keys()),
    }
    trades_24h = summary.get("trades_24h", summary.get("total", "N/A"))
    win_rate   = summary.get("win_rate", summary.get("win_rate_24h", "N/A"))
    pnl        = summary.get("pnl_usd", summary.get("total_pnl_usd", 0))

    lines = [
        f"Regime: {opus_result.get('regime','?').upper()} | Light: {opus_result.get('green_light','?').upper()}",
        f"Fear & Greed: {opus_result.get('fear_greed',50)}",
        f"Yesterday: {trades_24h} trades | Win rate: {win_rate if isinstance(win_rate, str) else f'{win_rate*100:.1f}%'} | P&L: ${pnl:.2f}" if not isinstance(win_rate, str) else f"Yesterday: {trades_24h} trades",
        f"Open positions: {snap['open_positions'] or 'None'}",
        f"Portfolio: ${snap['portfolio']:.2f}",
        "",
        f"Macro: {', '.join(opus_result.get('macro_events', ['None']))}"[:120],
        "",
        f"Insight: {opus_result.get('morning_briefing', '')}",
    ]
    return "\n".join(lines)
