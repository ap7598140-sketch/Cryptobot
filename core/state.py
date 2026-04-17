"""
Shared trading state -- single source of truth for all 7 bots.
All mutations go through async-safe helpers.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Position:
    coin: str
    direction: str          # "long" | "short"
    strategy: str           # "momentum" | "grid"
    entry_price: float
    stop_price: float
    target_price: float
    qty: float
    usd_value: float
    score: int
    opened_at: str          # ISO-format string
    peak_price: float       # highest (long) or lowest (short) price seen
    partial_taken: bool = False


class TradingState:
    def __init__(self):
        self._lock = asyncio.Lock()

        # control
        self.stop_command: bool = False
        self._halted: bool = False
        self._halt_reason: str = ""
        self._halt_until: Optional[str] = None   # ISO string

        # positions
        self.open_positions: dict[str, Position] = {}

        # risk counters
        self.consecutive_losses: int = 0
        self.daily_pnl: float = 0.0
        self.daily_pnl_date: Optional[str] = None
        self.portfolio_peak: float = 1_000.0
        self.portfolio_value: float = 1_000.0

        # trade history (in-memory, last N)
        self.recent_trades: list[dict] = []

    # -- Halt / resume ---------------------------------------------------------

    async def is_halted(self) -> bool:
        async with self._lock:
            if self.stop_command:
                return True
            if self._halted and self._halt_until:
                if datetime.now(timezone.utc).isoformat() >= self._halt_until:
                    self._halted = False
                    self._halt_reason = ""
                    self._halt_until = None
                    logger.info("Timed halt expired -- resuming")
                    return False
            return self._halted

    async def halt(self, reason: str):
        async with self._lock:
            self._halted = True
            self._halt_reason = reason
            logger.warning("Trading HALTED: %s", reason)

    async def halt_until(self, iso_str: str):
        async with self._lock:
            self._halted = True
            self._halt_until = iso_str
            logger.warning("Trading halted until %s", iso_str)

    async def resume(self):
        async with self._lock:
            self._halted = False
            self._halt_reason = ""
            self._halt_until = None

    # -- Positions -------------------------------------------------------------

    async def add_position(self, coin: str, pos: Position):
        async with self._lock:
            self.open_positions[coin] = pos

    async def update_position(self, coin: str, pos: Position):
        async with self._lock:
            if coin in self.open_positions:
                self.open_positions[coin] = pos

    async def remove_position(self, coin: str, win: bool = False,
                               pnl_pct: float = 0.0, pnl_usd: float = 0.0):
        async with self._lock:
            pos = self.open_positions.pop(coin, None)
            if pos is None:
                return
            trade_rec = {
                "coin":      coin,
                "direction": pos.direction,
                "strategy":  pos.strategy,
                "entry":     pos.entry_price,
                "pnl_pct":   pnl_pct,
                "pnl_usd":   pnl_usd,
                "win":       win,
                "score":     pos.score,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.recent_trades.append(trade_rec)
            if len(self.recent_trades) > 200:
                self.recent_trades = self.recent_trades[-200:]

            if win:
                self.consecutive_losses = 0
            else:
                self.consecutive_losses += 1

    # -- P&L / risk metrics ----------------------------------------------------

    async def record_daily_pnl(self, pnl_usd: float):
        async with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self.daily_pnl_date != today:
                self.daily_pnl = 0.0
                self.daily_pnl_date = today
            self.daily_pnl += pnl_usd

    async def get_daily_loss_pct(self) -> float:
        async with self._lock:
            if self.portfolio_value <= 0:
                return 0.0
            loss = -self.daily_pnl / self.portfolio_value
            return max(loss, 0.0)

    async def get_drawdown(self) -> float:
        async with self._lock:
            if self.portfolio_peak <= 0:
                return 0.0
            dd = (self.portfolio_peak - self.portfolio_value) / self.portfolio_peak
            return max(dd, 0.0)

    async def update_portfolio_value(self, value: float):
        async with self._lock:
            self.portfolio_value = value
            if value > self.portfolio_peak:
                self.portfolio_peak = value


state = TradingState()
