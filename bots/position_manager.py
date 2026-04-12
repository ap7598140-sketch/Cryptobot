"""
Bot 4 — Position Manager (Haiku)
Runs every 30 s, monitors all open positions.

Hard stop-loss at 3 % — NO AI, immediate execution.
Tiered trailing stop: 3 % profit → trail 2 %; 6 % profit → trail 4 %.
Partial exit: sell 50 % at 4 % profit.
Time-based stop: exit full position after 4 hours of no meaningful movement.
Re-entry signal: if exited a winner and trend still strong, flags to scanner queue.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from core.alpaca_client import AlpacaClient
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import (
    HARD_STOP_PCT, PARTIAL_EXIT_PCT, TAKE_PROFIT_PCT,
    TRAILING_TIER1_PROFIT, TRAILING_TIER1_TRAIL,
    TRAILING_TIER2_PROFIT, TRAILING_TIER2_TRAIL,
    POSITION_MGR_INTERVAL_S, POSITION_TIME_LIMIT_HRS,
    LOGS_DIR,
)

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, alpaca: AlpacaClient, telegram: TelegramNotifier,
                 signal_queue: asyncio.Queue):
        self.alpaca       = alpaca
        self.telegram     = telegram
        self.signal_queue = signal_queue   # for re-entry flagging

    async def run(self):
        logger.info("Position Manager started — interval %ds", POSITION_MGR_INTERVAL_S)
        while not state.stop_command:
            try:
                await self._check_positions()
            except Exception as exc:
                logger.error("Position manager error: %s", exc)
            await asyncio.sleep(POSITION_MGR_INTERVAL_S)

    async def _check_positions(self):
        coins = list(state.open_positions.keys())
        if not coins:
            return

        # Get current prices from Alpaca in one call
        alpaca_positions = await asyncio.to_thread(self.alpaca.get_all_positions)
        price_map = {p["symbol"]: p["current_price"] for p in alpaca_positions}

        for coin in coins:
            pos = state.open_positions.get(coin)
            if not pos or pos.filled_qty <= 0:
                continue

            current_price = price_map.get(coin)
            if not current_price:
                continue

            await self._manage_position(pos, current_price)

    async def _manage_position(self, pos, current_price: float):
        coin      = pos.coin
        direction = pos.direction
        entry     = pos.entry_price

        if direction == "long":
            pnl_pct = (current_price - entry) / entry
        else:
            pnl_pct = (entry - current_price) / entry

        # ── 1. HARD STOP — no AI, no override ────────────────────────────────
        if pnl_pct <= -HARD_STOP_PCT:
            logger.warning("HARD STOP triggered for %s at %.2f (%.2f%%)",
                           coin, current_price, pnl_pct * 100)
            await self._close_position(pos, current_price, "hard_stop")
            loss_usd = abs(pnl_pct) * pos.filled_qty * entry
            await self.telegram.stop_loss_fired(coin, abs(pnl_pct) * 100, loss_usd)
            return

        # ── 2. Update trailing stop level ────────────────────────────────────
        pos.max_profit_pct = max(pos.max_profit_pct, pnl_pct)

        if pos.max_profit_pct >= TRAILING_TIER2_PROFIT:
            trail_pct = TRAILING_TIER2_TRAIL
        elif pos.max_profit_pct >= TRAILING_TIER1_PROFIT:
            trail_pct = TRAILING_TIER1_TRAIL
        else:
            trail_pct = None

        if trail_pct is not None:
            if direction == "long":
                new_trail_stop = current_price * (1 - trail_pct)
                if pos.trailing_stop_price is None or new_trail_stop > pos.trailing_stop_price:
                    pos.trailing_stop_price = new_trail_stop
            else:
                new_trail_stop = current_price * (1 + trail_pct)
                if pos.trailing_stop_price is None or new_trail_stop < pos.trailing_stop_price:
                    pos.trailing_stop_price = new_trail_stop

        # ── 3. Check trailing stop breach ─────────────────────────────────────
        if pos.trailing_stop_price is not None:
            hit = (direction == "long" and current_price <= pos.trailing_stop_price) or \
                  (direction == "short" and current_price >= pos.trailing_stop_price)
            if hit:
                logger.info("Trailing stop hit for %s @ %.2f", coin, current_price)
                await self._close_position(pos, current_price, "trailing_stop")
                await self.telegram.trailing_stop_triggered(coin, pos.max_profit_pct * 100)
                return

        # ── 4. Partial exit at 4 % ────────────────────────────────────────────
        if not pos.partial_exit_done and pnl_pct >= PARTIAL_EXIT_PCT:
            half_qty = round(pos.filled_qty * 0.50, 6)
            if half_qty > 0:
                side = "sell" if direction == "long" else "buy"
                order_id = await asyncio.to_thread(
                    self.alpaca.place_limit_order, coin, side, half_qty, current_price
                )
                if order_id:
                    pos.partial_exit_done = True
                    pos.filled_qty        = round(pos.filled_qty - half_qty, 6)
                    gain_pct              = pnl_pct * 100
                    await self.telegram.partial_exit(
                        coin, half_qty, pos.filled_qty, current_price, gain_pct
                    )
                    logger.info("Partial exit: %s sold %.6f @ %.2f", coin, half_qty, current_price)
                    _log_event(coin, "partial_exit", current_price, pnl_pct)
            return

        # ── 5. Full take-profit ───────────────────────────────────────────────
        if pnl_pct >= TAKE_PROFIT_PCT:
            logger.info("Take profit hit for %s @ %.2f (%.2f%%)",
                        coin, current_price, pnl_pct * 100)
            await self._close_position(pos, current_price, "take_profit")
            gain_usd = pnl_pct * pos.filled_qty * entry
            await self.telegram.take_profit(coin, pnl_pct * 100, gain_usd)
            # Check re-entry potential
            await self._check_reentry(pos, current_price, pnl_pct)
            return

        # ── 6. Time-based stop: 4 hours no meaningful move ───────────────────
        age_hours = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600
        if age_hours >= POSITION_TIME_LIMIT_HRS and abs(pnl_pct) < 0.01:
            logger.info("Time-based stop for %s — %dh no movement", coin, POSITION_TIME_LIMIT_HRS)
            await self._close_position(pos, current_price, "time_stop")
            pnl_usd = pnl_pct * pos.filled_qty * entry
            if pnl_pct >= 0:
                await self.telegram.take_profit(coin, pnl_pct * 100, pnl_usd)
            else:
                await self.telegram.stop_loss_fired(coin, abs(pnl_pct) * 100, abs(pnl_usd))

    async def _close_position(self, pos, current_price: float, reason: str):
        """Close entire position — market order through Alpaca."""
        coin      = pos.coin
        direction = pos.direction
        entry     = pos.entry_price
        filled    = pos.filled_qty

        pnl_pct = ((current_price - entry) / entry) if direction == "long" \
                  else ((entry - current_price) / entry)
        pnl_usd = pnl_pct * filled * entry

        await asyncio.to_thread(self.alpaca.close_position, coin)
        await state.remove_position(coin)
        await state.record_pnl(pnl_usd)

        _log_event(coin, reason, current_price, pnl_pct, pnl_usd)
        _append_trade_record(pos, current_price, pnl_pct, pnl_usd, reason)
        logger.info("Position closed: %s reason=%s pnl=%.2f%%", coin, reason, pnl_pct * 100)

    async def _check_reentry(self, pos, current_price: float, pnl_pct: float):
        """If we just exited a winning trade and trend still looks strong, re-flag."""
        if pnl_pct < 0.03:   # only re-entry flag if meaningful win
            return
        # We create a minimal re-entry signal for the scanner queue
        reentry = {
            "coin": pos.coin,
            "direction": pos.direction,
            "score": max(70, pos.score - 5),  # slightly lower confidence
            "reason": f"re-entry after profitable exit (pnl={pnl_pct:.2f}%)",
            "price": current_price,
            "ind_15m": {}, "ind_1h": {}, "asian_range": {},
            "funding_rate": 0, "fear_greed": {}, "btc_dom_trend": "stable",
            "stablecoin_flow": "neutral",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "is_reentry": True,
        }
        try:
            self.signal_queue.put_nowait(reentry)
            logger.info("Re-entry signal queued for %s", pos.coin)
        except asyncio.QueueFull:
            pass


def _log_event(coin: str, event: str, price: float, pnl_pct: float, pnl_usd: float = 0):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"position_{coin.replace('/','_')}_{event}_{ts}.json")
    try:
        with open(path, "w") as f:
            json.dump({
                "coin": coin, "event": event, "price": price,
                "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2)
    except Exception as e:
        logger.error("Failed to write position log: %s", e)


def _append_trade_record(pos, exit_price: float, pnl_pct: float, pnl_usd: float, reason: str):
    """Append closed trade to data/trades.json for performance tracker."""
    import json, os
    path = "data/trades.json"
    os.makedirs("data", exist_ok=True)
    records = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                records = json.load(f)
        except Exception:
            records = []
    records.append({
        "coin":        pos.coin,
        "direction":   pos.direction,
        "entry_price": pos.entry_price,
        "exit_price":  exit_price,
        "qty":         pos.filled_qty,
        "pnl_pct":     pnl_pct,
        "pnl_usd":     pnl_usd,
        "score":       pos.score,
        "reason":      reason,
        "opened_at":   pos.opened_at.isoformat(),
        "closed_at":   datetime.now(timezone.utc).isoformat(),
        "win":         pnl_usd > 0,
        "hour_utc":    datetime.now(timezone.utc).hour,
    })
    try:
        with open(path, "w") as f:
            json.dump(records, f, indent=2, default=str)
    except Exception as e:
        logger.error("Failed to append trade record: %s", e)
