"""
Bot 4 -- Position Manager (Haiku).
Checks every 30s: hard stop, take profit, trailing stop, time limit.
Grid strategy: take profit at resistance.
Trending strategy: tiered trailing stops.
"""
import asyncio
import logging
from datetime import datetime, timezone

from core.alpaca_client import AlpacaClient
from core.claude_client import ClaudeClient
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import (
    HARD_STOP_PCT, TAKE_PROFIT_PCT, PARTIAL_EXIT_PCT,
    TRAILING_TIER1_PROFIT, TRAILING_TIER1_TRAIL,
    TRAILING_TIER2_PROFIT, TRAILING_TIER2_TRAIL,
    POSITION_TIME_LIMIT_HRS, POSITION_MGR_INTERVAL_S,
)

logger = logging.getLogger(__name__)


def _exit_prompt(pos, current_price: float, regime: dict) -> str:
    elapsed_hrs = (
        (datetime.now(timezone.utc) -
         datetime.fromisoformat(pos.opened_at)).total_seconds() / 3600
    )
    pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
    if pos.direction == "short":
        pnl_pct = -pnl_pct
    return (
        f"Manage position: {pos.coin} {pos.direction.upper()} [{pos.strategy}]\n"
        f"entry={pos.entry_price:.2f} current={current_price:.2f} "
        f"stop={pos.stop_price:.2f} target={pos.target_price:.2f}\n"
        f"pnl={pnl_pct:.2f}% elapsed_hrs={elapsed_hrs:.1f}\n"
        f"regime={regime.get('regime','unknown')} green_light={regime.get('green_light','yellow')}\n"
        f"Action options: hold/exit/adjust_stop\n"
        f"Return: {{\"action\":\"hold/exit/adjust_stop\",\"new_stop\":0.0,\"reason\":\"brief\"}}"
    )


class PositionManager:
    def __init__(self, claude: ClaudeClient, alpaca: AlpacaClient,
                 telegram: TelegramNotifier):
        self.claude   = claude
        self.alpaca   = alpaca
        self.telegram = telegram

    async def run(self):
        logger.info("Position Manager started -- interval %ds", POSITION_MGR_INTERVAL_S)
        while not state.stop_command:
            try:
                await self._manage_all()
            except Exception as e:
                logger.error("Position Manager error: %s", e)
            await asyncio.sleep(POSITION_MGR_INTERVAL_S)

    async def _manage_all(self):
        if await state.is_halted():
            return
        for coin, pos in list(state.open_positions.items()):
            try:
                await self._manage_one(coin, pos)
            except Exception as e:
                logger.error("Error managing %s: %s", coin, e)

    async def _manage_one(self, coin: str, pos):
        price_data = await asyncio.to_thread(self.alpaca.get_latest_price, coin)
        if not price_data:
            return
        current = float(price_data)

        if pos.direction == "long" and current > pos.peak_price:
            pos.peak_price = current
            await state.update_position(coin, pos)
        elif pos.direction == "short" and current < pos.peak_price:
            pos.peak_price = current
            await state.update_position(coin, pos)

        pnl_pct = (current - pos.entry_price) / pos.entry_price
        if pos.direction == "short":
            pnl_pct = -pnl_pct

        if pos.direction == "long" and current <= pos.stop_price:
            await self._close(coin, pos, current, "hard_stop")
            return
        if pos.direction == "short" and current >= pos.stop_price:
            await self._close(coin, pos, current, "hard_stop")
            return

        elapsed_hrs = (
            datetime.now(timezone.utc) -
            datetime.fromisoformat(pos.opened_at)
        ).total_seconds() / 3600
        if elapsed_hrs >= POSITION_TIME_LIMIT_HRS:
            await self._close(coin, pos, current, "time_limit")
            return

        if pos.strategy == "grid":
            if pos.direction == "long" and current >= pos.target_price:
                await self._close(coin, pos, current, "grid_take_profit")
                return
            if pos.direction == "short" and current <= pos.target_price:
                await self._close(coin, pos, current, "grid_take_profit")
                return

        if pos.strategy != "grid":
            peak_pnl = abs(pos.peak_price - pos.entry_price) / pos.entry_price

            if peak_pnl >= TRAILING_TIER2_PROFIT:
                trail_price = (
                    pos.peak_price * (1 - TRAILING_TIER2_TRAIL)
                    if pos.direction == "long"
                    else pos.peak_price * (1 + TRAILING_TIER2_TRAIL)
                )
                hit = (pos.direction == "long" and current <= trail_price) or \
                      (pos.direction == "short" and current >= trail_price)
                if hit:
                    await self._close(coin, pos, current, "trailing_tier2")
                    await self.telegram.trailing_stop_triggered(coin, pnl_pct * 100)
                    return

            elif peak_pnl >= TRAILING_TIER1_PROFIT:
                trail_price = (
                    pos.peak_price * (1 - TRAILING_TIER1_TRAIL)
                    if pos.direction == "long"
                    else pos.peak_price * (1 + TRAILING_TIER1_TRAIL)
                )
                hit = (pos.direction == "long" and current <= trail_price) or \
                      (pos.direction == "short" and current >= trail_price)
                if hit:
                    await self._close(coin, pos, current, "trailing_tier1")
                    await self.telegram.trailing_stop_triggered(coin, pnl_pct * 100)
                    return

            if pnl_pct >= PARTIAL_EXIT_PCT and not pos.partial_taken:
                partial_qty = round(pos.qty * 0.5, 4)
                await asyncio.to_thread(self.alpaca.place_market_order,
                                         coin, partial_qty,
                                         "sell" if pos.direction == "long" else "buy")
                pos.partial_taken = True
                pos.qty -= partial_qty
                await state.update_position(coin, pos)
                await self.telegram.partial_exit(
                    coin, partial_qty, pos.qty, current, pnl_pct * 100)
                return

        from core.regime_cache import get_regime
        regime = get_regime()
        h_result = await self.claude.haiku(_exit_prompt(pos, current, regime),
                                           bot="position_mgr")
        if not h_result or not isinstance(h_result, dict):
            logger.warning("Position Haiku failed for %s -- holding", coin)
            return

        action = h_result.get("action", "hold")
        if action == "exit":
            await self._close(coin, pos, current, f"ai_exit: {h_result.get('reason','')}")
        elif action == "adjust_stop":
            new_stop = float(h_result.get("new_stop", pos.stop_price))
            if new_stop > 0:
                pos.stop_price = new_stop
                await state.update_position(coin, pos)
                logger.info("Adjusted stop for %s to %.2f", coin, new_stop)

    async def _close(self, coin: str, pos, current: float, reason: str):
        await asyncio.to_thread(self.alpaca.close_position, coin)
        pnl_pct = (current - pos.entry_price) / pos.entry_price * 100
        if pos.direction == "short":
            pnl_pct = -pnl_pct
        pnl_usd = pnl_pct / 100 * pos.usd_value

        win = pnl_pct > 0
        await state.remove_position(coin, win=win, pnl_pct=pnl_pct, pnl_usd=pnl_usd)
        await state.record_daily_pnl(pnl_usd)

        if "stop" in reason:
            await self.telegram.stop_loss_fired(coin, abs(pnl_pct), abs(pnl_usd))
        elif "profit" in reason or "take_profit" in reason:
            await self.telegram.take_profit(coin, pnl_pct, pnl_usd)

        logger.info("Closed %s: reason=%s pnl=%.2f%%", coin, reason, pnl_pct)
