"""
Shared trading state — single source of truth for all bots.
All mutations go through async-safe helper methods.
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
    entry_price: float
    quantity: float         # total planned quantity
    filled_qty: float       # how much is actually filled so far
    stop_price: float
    target_price: float
    score: int
    opened_at: datetime
    partial_exit_done: bool = False
    max_profit_pct: float = 0.0
    trailing_stop_price: Optional[float] = None
    pending_order_id: Optional[str] = None
    second_entry_placed: bool = False


class TradingState:
    def __init__(self):
        self._lock = asyncio.Lock()

        # ── bot control ──────────────────────────────────────────────────────
        self.stop_command: bool = False
        self.trading_paused: bool = False
        self.pause_reason: str = ""
        self.pause_until: Optional[datetime] = None   # timed pause

        # ── risk guard ───────────────────────────────────────────────────────
        self.risk_guard_green: bool = True
        self.volatility_spike: bool = False

        # ── positions & P&L ──────────────────────────────────────────────────
        self.open_positions: dict[str, Position] = {}    # coin → Position
        self.consecutive_losses: int = 0
        self.daily_pnl: float = 0.0
        self.daily_pnl_date: Optional[str] = None        # "YYYY-MM-DD" UTC
        self.portfolio_peak: float = 1_000.0
        self.portfolio_value: float = 1_000.0

        # ── market context ────────────────────────────────────────────────────
        self.market_regime: str = "unknown"              # bull/bear/sideways
        self.fear_greed_index: int = 50
        self.btc_dominance_trend: str = "stable"         # rising/falling/stable

        # ── performance / adaptations ─────────────────────────────────────────
        self.strategy_decay: bool = False
        self.size_multiplier: float = 1.0                # shrinks on decay/drawdown
        self.session_aggression: str = "normal"          # aggressive/normal/conservative
        self.best_hours: list[int] = []                  # fed back from perf tracker
        self.worst_hours: list[int] = []

        # ── last reconciliation ───────────────────────────────────────────────
        self.last_reconciliation: Optional[datetime] = None

    # ── helpers ──────────────────────────────────────────────────────────────

    async def is_halted(self) -> bool:
        """True when no new trades should be opened."""
        async with self._lock:
            if self.stop_command:
                return True
            if self.trading_paused:
                # Check if timed pause has expired
                if self.pause_until and datetime.now(timezone.utc) >= self.pause_until:
                    self.trading_paused = False
                    self.pause_until = None
                    self.pause_reason = ""
                    logger.info("Timed pause expired — trading resumed")
                    return False
                return True
            return False

    async def pause(self, reason: str, until: Optional[datetime] = None):
        async with self._lock:
            self.trading_paused = True
            self.pause_reason = reason
            self.pause_until = until
            self.risk_guard_green = False
            logger.warning(f"Trading PAUSED: {reason}")

    async def resume(self):
        async with self._lock:
            self.trading_paused = False
            self.pause_reason = ""
            self.pause_until = None
            self.risk_guard_green = True
            logger.info("Trading RESUMED")

    async def add_position(self, position: Position):
        async with self._lock:
            self.open_positions[position.coin] = position

    async def remove_position(self, coin: str):
        async with self._lock:
            self.open_positions.pop(coin, None)

    async def update_portfolio_value(self, value: float):
        async with self._lock:
            self.portfolio_value = value
            if value > self.portfolio_peak:
                self.portfolio_peak = value

    async def record_pnl(self, pnl: float):
        """Record a closed trade's P&L; resets daily counter at UTC midnight."""
        async with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self.daily_pnl_date != today:
                self.daily_pnl = 0.0
                self.daily_pnl_date = today
            self.daily_pnl += pnl
            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0

    async def snapshot(self) -> dict:
        """Return a safe JSON-serialisable snapshot for logging/Telegram."""
        async with self._lock:
            return {
                "portfolio_value": self.portfolio_value,
                "portfolio_peak": self.portfolio_peak,
                "daily_pnl": self.daily_pnl,
                "open_positions": list(self.open_positions.keys()),
                "consecutive_losses": self.consecutive_losses,
                "trading_paused": self.trading_paused,
                "pause_reason": self.pause_reason,
                "risk_guard_green": self.risk_guard_green,
                "market_regime": self.market_regime,
                "size_multiplier": self.size_multiplier,
                "strategy_decay": self.strategy_decay,
            }


# Module-level singleton — all bots import this
state = TradingState()
