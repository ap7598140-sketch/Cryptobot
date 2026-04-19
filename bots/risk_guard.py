"""
Bot 5 -- Risk Guard (Haiku).
Hard gates: daily loss, drawdown, consecutive losses, cash reserve.
Reconciles open positions with Alpaca every 5 min (crypto only).
"""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from core.alpaca_client import AlpacaClient
from core.claude_client import ClaudeClient
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import (
    DAILY_LOSS_LIMIT, MAX_DRAWDOWN, MAX_CONSECUTIVE_LOSSES,
    CONSEC_LOSS_PAUSE_HRS, PORTFOLIO_VALUE, CASH_RESERVE_PCT,
    RISK_GUARD_INTERVAL_S, RECONCILE_INTERVAL_S,
)

logger = logging.getLogger(__name__)

_last_reconcile: float = 0.0


def _risk_prompt(metrics: dict) -> str:
    return (
        f"Assess portfolio risk.\n"
        f"daily_pnl_pct={metrics['daily_pnl_pct']:.2f} "
        f"drawdown_pct={metrics['drawdown_pct']:.2f} "
        f"open_positions={metrics['open_positions']} "
        f"consecutive_losses={metrics['consecutive_losses']}\n"
        f"cash_ratio={metrics['cash_ratio']:.2f} "
        f"total_exposure_usd={metrics['exposure_usd']:.2f}\n"
        f"Return: {{\"halt\":true/false,\"reason\":\"brief\",\"severity\":\"low/medium/high\"}}"
    )


class RiskGuard:
    def __init__(self, claude: ClaudeClient, alpaca: AlpacaClient,
                 telegram: TelegramNotifier):
        self.claude   = claude
        self.alpaca   = alpaca
        self.telegram = telegram

    async def run(self):
        logger.info("Risk Guard started -- interval %ds", RISK_GUARD_INTERVAL_S)
        while not state.stop_command:
            try:
                await self._check()
            except Exception as e:
                logger.error("Risk Guard error: %s", e)
            await asyncio.sleep(RISK_GUARD_INTERVAL_S)

    async def _check(self):
        dd = await state.get_drawdown()
        if dd >= MAX_DRAWDOWN:
            await state.halt(f"drawdown_{dd:.2%}")
            await self.telegram.drawdown_circuit_breaker(dd * 100)
            logger.warning("CIRCUIT BREAKER: drawdown %.1f%%", dd * 100)
            return

        daily_loss = await state.get_daily_loss_pct()
        if daily_loss >= DAILY_LOSS_LIMIT:
            now      = datetime.now(timezone.utc)
            midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            await state.halt_until(midnight.isoformat())
            await self.telegram.daily_loss_limit(daily_loss * 100)
            logger.warning("Daily loss limit hit: %.1f%%", daily_loss * 100)
            return

        consec = state.consecutive_losses
        if consec >= MAX_CONSECUTIVE_LOSSES:
            resume = (datetime.now(timezone.utc) +
                      timedelta(hours=CONSEC_LOSS_PAUSE_HRS)).isoformat()
            await state.halt_until(resume)
            await self.telegram.consecutive_loss_pause(resume)
            logger.warning("Consecutive losses %d -- pausing %dh", consec, CONSEC_LOSS_PAUSE_HRS)
            return

        account  = await asyncio.to_thread(self.alpaca.get_account)
        cash     = float(account.cash) if account else PORTFOLIO_VALUE
        exposure = sum(p.usd_value for p in state.open_positions.values())
        metrics  = {
            "daily_pnl_pct":      daily_loss,
            "drawdown_pct":       dd,
            "open_positions":     len(state.open_positions),
            "consecutive_losses": consec,
            "cash_ratio":         cash / PORTFOLIO_VALUE if PORTFOLIO_VALUE else 0,
            "exposure_usd":       exposure,
        }

        h_result = await self.claude.haiku(_risk_prompt(metrics), bot="risk_guard")
        if h_result and isinstance(h_result, dict) and h_result.get("halt"):
            reason = h_result.get("reason", "ai_risk")
            await state.halt(reason)
            logger.warning("AI risk halt: %s", reason)

        global _last_reconcile
        now_ts = time.monotonic()
        if now_ts - _last_reconcile >= RECONCILE_INTERVAL_S:
            _last_reconcile = now_ts
            await self._reconcile()

    async def _reconcile(self):
        try:
            alpaca_positions = await asyncio.to_thread(self.alpaca.get_positions)
            alpaca_symbols = {p["symbol"] for p in alpaca_positions if "/" in p["symbol"]}
            state_symbols  = set(state.open_positions.keys())

            missing = alpaca_symbols - state_symbols
            phantom = state_symbols - alpaca_symbols

            if missing or phantom:
                msg = f"missing={missing} phantom={phantom}"
                await self.telegram.reconciliation_mismatch(msg)
                await state.halt("reconciliation_mismatch")
                logger.warning("Reconciliation mismatch: %s", msg)
            else:
                logger.debug("Reconciliation OK -- %d positions", len(state_symbols))
        except Exception as e:
            logger.error("Reconciliation error: %s", e)
