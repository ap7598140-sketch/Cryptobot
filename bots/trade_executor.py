"""
Bot 3 -- Trade Executor (Sonnet).
Receives confirmed signals from Trend Analyzer, sizes and places orders,
then hands position off to Position Manager via state.
"""
import asyncio
import logging
from datetime import datetime, timezone

from core.alpaca_client import AlpacaClient
from core.claude_client import ClaudeClient
from core.regime_cache import get_params
from core.state import state, Position
from core.telegram_notifier import TelegramNotifier
from settings import (
    PORTFOLIO_VALUE, CASH_RESERVE_PCT, MAX_POSITIONS, MAX_RISK_PER_TRADE,
    MIN_REWARD_RISK, ENTRY_FIRST_FRAC, ENTRY_CONFIRM_FRAC, ORDER_TIMEOUT_S,
    ROUND_NUM_OFFSET_PCT, DRAWDOWN_SIZE_THRESHOLD, DRAWDOWN_SIZE_REDUCTION,
    DECAY_LOOKBACK, DECAY_WIN_RATE_FLOOR, DECAY_SIZE_REDUCTION,
)

logger = logging.getLogger(__name__)


def _size_prompt(sig: dict, account_cash: float, params: dict) -> str:
    return (
        f"Size trade for {sig['coin']} {sig['direction'].upper()} "
        f"strategy={sig.get('strategy','momentum')} "
        f"score={sig.get('final_score',0)}\n"
        f"entry={sig['entry']:.2f} stop={sig['stop']:.2f} "
        f"target={sig['target']:.2f}\n"
        f"account_cash={account_cash:.2f} max_risk_pct={MAX_RISK_PER_TRADE}\n"
        f"Calc position_size_usd, qty (to 4dp), risk/reward ratio.\n"
        f"Return: {{\"qty\":0.0,\"usd\":0.0,\"rr_ratio\":0.0,\"approved\":true/false,\"reason\":\"brief\"}}"
    )


class TradeExecutor:
    def __init__(self, claude: ClaudeClient, alpaca: AlpacaClient,
                 executor_queue: asyncio.Queue, telegram: TelegramNotifier):
        self.claude         = claude
        self.alpaca         = alpaca
        self.executor_queue = executor_queue
        self.telegram       = telegram

    async def run(self):
        logger.info("Trade Executor started")
        while not state.stop_command:
            try:
                sig = await asyncio.wait_for(self.executor_queue.get(), timeout=5.0)
                await self._execute(sig)
                self.executor_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Executor error: %s", e)

    async def _execute(self, sig: dict):
        if await state.is_halted():
            return

        coin = sig["coin"]

        if len(state.open_positions) >= MAX_POSITIONS:
            logger.info("Max positions reached -- skipping %s", coin)
            return

        if coin in state.open_positions:
            logger.info("Already in position for %s -- skipping", coin)
            return

        entry, stop, target = sig["entry"], sig["stop"], sig["target"]
        risk   = abs(entry - stop)
        reward = abs(target - entry)
        rr     = reward / risk if risk > 0 else 0
        if rr < MIN_REWARD_RISK:
            logger.info("RR %.2f < %.2f for %s -- skipping", rr, MIN_REWARD_RISK, coin)
            return

        account    = await asyncio.to_thread(self.alpaca.get_account)
        cash       = float(account.cash) if account else PORTFOLIO_VALUE * (1 - CASH_RESERVE_PCT)
        deployable = cash - PORTFOLIO_VALUE * CASH_RESERVE_PCT
        if deployable <= 0:
            logger.info("Cash reserve hit -- skipping %s", coin)
            return

        params   = get_params()
        s_result = await self.claude.sonnet(_size_prompt(sig, deployable, params),
                                            bot="executor_sonnet")
        if not s_result or not isinstance(s_result, dict):
            logger.warning("Executor Sonnet failed for %s -- closing any open position", coin)
            if coin in state.open_positions:
                await asyncio.to_thread(self.alpaca.close_position, coin)
                await state.remove_position(coin)
            return

        if not s_result.get("approved", False):
            logger.info("Sizing rejected for %s: %s", coin, s_result.get("reason"))
            return

        qty = float(s_result.get("qty", 0))
        usd = float(s_result.get("usd", 0))
        if qty <= 0 or usd <= 0:
            logger.warning("Invalid qty/usd for %s", coin)
            return

        dd = await state.get_drawdown()
        if dd >= DRAWDOWN_SIZE_THRESHOLD:
            qty = round(qty * (1 - DRAWDOWN_SIZE_REDUCTION), 4)
            usd = round(usd * (1 - DRAWDOWN_SIZE_REDUCTION), 2)
            logger.info("Drawdown %.1f%% -> reduced size to %.4f", dd * 100, qty)

        recent = state.recent_trades[-DECAY_LOOKBACK:] if state.recent_trades else []
        if len(recent) >= DECAY_LOOKBACK:
            wins = sum(1 for t in recent if t.get("win"))
            if wins / len(recent) < DECAY_WIN_RATE_FLOOR:
                qty = round(qty * (1 - DECAY_SIZE_REDUCTION), 4)
                usd = round(usd * (1 - DECAY_SIZE_REDUCTION), 2)
                logger.info("Strategy decay detected -> reduced size to %.4f", qty)

        first_qty = round(qty * ENTRY_FIRST_FRAC, 4)
        order = await asyncio.to_thread(self.alpaca.place_order,
                                        coin, first_qty, sig["direction"])
        if not order:
            logger.error("Order failed for %s", coin)
            return

        await asyncio.sleep(2)
        offset      = entry * ROUND_NUM_OFFSET_PCT
        limit_price = round(entry - offset if sig["direction"] == "long" else entry + offset, 2)
        second_qty  = round(qty * ENTRY_CONFIRM_FRAC, 4)
        await asyncio.to_thread(self.alpaca.place_limit_order,
                                 coin, second_qty, sig["direction"], limit_price)

        pos = Position(
            coin         = coin,
            direction    = sig["direction"],
            strategy     = sig.get("strategy", "momentum"),
            entry_price  = entry,
            stop_price   = stop,
            target_price = target,
            qty          = qty,
            usd_value    = usd,
            score        = sig.get("final_score", 0),
            opened_at    = datetime.now(timezone.utc).isoformat(),
            peak_price   = entry,
        )
        await state.add_position(coin, pos)

        await self.telegram.trade_opened(
            coin, sig["direction"], sig.get("strategy", "momentum"),
            entry, qty, sig.get("final_score", 0), usd,
        )
        logger.info("Executed: %s %s %.4f @ %.2f [%s]",
                    coin, sig["direction"], qty, entry, sig.get("strategy"))
