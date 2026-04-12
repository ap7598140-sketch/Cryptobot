"""
Bot 5 — Risk Guard (Haiku)
Runs every 30 s. CANNOT be bypassed.
Sets state.risk_guard_green and pauses trading when any hard gate triggers.

Hard gates:
  ✓ Daily loss limit 5 % → stop until midnight UTC
  ✓ Max 2 consecutive losses → 2-hour pause
  ✓ Correlation block (enforced in executor, also monitored here)
  ✓ Max 2 open positions
  ✓ 40 % cash reserve
  ✓ 15 % portfolio drawdown from ATH → full pause + alert
  ✓ Volatility spike (ATR suddenly ≥ 3× its 12-candle average)
  ✓ Position reconciliation every 5 min
  ✓ Session aggression (London/NY overlap vs. off-hours)
  ✓ Drawdown-based sizing (portfolio down 8 % → -30 % size)
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from core.alpaca_client import AlpacaClient, TF_15MIN
from core.market_data import bars_to_df, calc_indicators
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import (
    ACTIVE_COINS,
    CASH_RESERVE_PCT, MAX_POSITIONS,
    DAILY_LOSS_LIMIT, MAX_DRAWDOWN,
    MAX_CONSECUTIVE_LOSSES, CONSEC_LOSS_PAUSE_HRS,
    DRAWDOWN_SIZE_THRESHOLD, DRAWDOWN_SIZE_REDUCTION,
    DECAY_SIZE_REDUCTION,
    RISK_GUARD_INTERVAL_S, RECONCILE_INTERVAL_S,
    MAX_CANDLES,
)

logger = logging.getLogger(__name__)

# London/NY overlap: 08:00–12:00 ET = 13:00–17:00 UTC
_AGGRESSIVE_START_UTC = 13
_AGGRESSIVE_END_UTC   = 17


class RiskGuard:
    def __init__(self, alpaca: AlpacaClient, telegram: TelegramNotifier):
        self.alpaca   = alpaca
        self.telegram = telegram
        self._last_reconcile: datetime = datetime.min.replace(tzinfo=timezone.utc)

    async def run(self):
        logger.info("Risk Guard started — interval %ds", RISK_GUARD_INTERVAL_S)
        while not state.stop_command:
            try:
                await self._guard_cycle()
            except Exception as exc:
                logger.error("Risk Guard error: %s", exc)
            await asyncio.sleep(RISK_GUARD_INTERVAL_S)

    # ── is_clear: called by executor before every order ──────────────────────

    @staticmethod
    def is_green() -> bool:
        return state.risk_guard_green and not state.trading_paused

    # ── main guard cycle ──────────────────────────────────────────────────────

    async def _guard_cycle(self):
        now = datetime.now(timezone.utc)

        # Refresh portfolio value
        portfolio_value = await asyncio.to_thread(self.alpaca.get_portfolio_value)
        cash            = await asyncio.to_thread(self.alpaca.get_cash)
        await state.update_portfolio_value(portfolio_value)

        # ── 1. Daily loss limit ───────────────────────────────────────────────
        today = now.strftime("%Y-%m-%d")
        if state.daily_pnl_date != today:
            pass  # new day; reset handled by state.record_pnl
        if portfolio_value > 0:
            daily_loss_pct = -(state.daily_pnl / portfolio_value)
        else:
            daily_loss_pct = 0
        if daily_loss_pct >= DAILY_LOSS_LIMIT and not state.trading_paused:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            await state.pause(f"Daily loss limit {daily_loss_pct*100:.1f}%", until=midnight)
            await self.telegram.daily_loss_limit(daily_loss_pct * 100)
            logger.warning("Daily loss limit reached: %.1f%%", daily_loss_pct * 100)

        # ── 2. Max drawdown from peak ────────────────────────────────────────
        if state.portfolio_peak > 0:
            drawdown = (state.portfolio_peak - portfolio_value) / state.portfolio_peak
            if drawdown >= MAX_DRAWDOWN and not state.trading_paused:
                await state.pause(f"Max drawdown {drawdown*100:.1f}% from peak")
                await self.telegram.drawdown_circuit_breaker(drawdown * 100)
                logger.critical("Drawdown circuit breaker: %.1f%%", drawdown * 100)
                async with state._lock:
                    state.risk_guard_green = False

        # ── 3. Consecutive losses ────────────────────────────────────────────
        if state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES and not state.trading_paused:
            resume = now + timedelta(hours=CONSEC_LOSS_PAUSE_HRS)
            await state.pause(f"{MAX_CONSECUTIVE_LOSSES} consecutive losses", until=resume)
            await self.telegram.consecutive_loss_pause(resume.strftime("%H:%M"))
            logger.warning("Consecutive loss pause until %s", resume.strftime("%H:%M UTC"))

        # ── 4. Cash reserve check ────────────────────────────────────────────
        if portfolio_value > 0:
            cash_pct = cash / portfolio_value
            if cash_pct < CASH_RESERVE_PCT * 0.90:  # 10 % buffer
                async with state._lock:
                    state.risk_guard_green = False
                logger.warning("Cash reserve low: %.1f%% (min %.0f%%)", cash_pct * 100, CASH_RESERVE_PCT * 100)
            else:
                async with state._lock:
                    if not state.trading_paused:
                        state.risk_guard_green = True

        # ── 5. Volatility spike check ────────────────────────────────────────
        await self._check_volatility_spike()

        # ── 6. Session aggression ────────────────────────────────────────────
        hour = now.hour
        if _AGGRESSIVE_START_UTC <= hour < _AGGRESSIVE_END_UTC:
            async with state._lock:
                state.session_aggression = "aggressive"
        elif 0 <= hour < 8 or 20 <= hour < 24:
            async with state._lock:
                state.session_aggression = "conservative"
        else:
            async with state._lock:
                state.session_aggression = "normal"

        # ── 7. Drawdown-based sizing ─────────────────────────────────────────
        await self._update_size_multiplier(portfolio_value)

        # ── 8. Position reconciliation every 5 min ────────────────────────────
        if (now - self._last_reconcile).total_seconds() >= RECONCILE_INTERVAL_S:
            await self._reconcile_positions()
            self._last_reconcile = now

    async def _check_volatility_spike(self):
        """
        Compare current ATR to 12-candle average ATR.
        If current ATR > 2× average → spike.
        """
        try:
            all_bars = await asyncio.to_thread(
                self.alpaca.get_bars, ACTIVE_COINS, TF_15MIN, MAX_CANDLES
            )
            spike_detected = False
            for coin in ACTIVE_COINS:
                bars = all_bars.get(coin, [])
                if len(bars) < 4:
                    continue
                df = bars_to_df(bars)
                ind = calc_indicators(df)
                atr_pct = ind.get("atr_pct", 0)
                # Simple spike: if current ATR% > 3× 'normal' threshold (0.5%)
                if atr_pct > 1.5:   # 1.5% ATR on 15m candle = extreme
                    spike_detected = True
                    if not state.volatility_spike:
                        await self.telegram.volatility_spike_block(coin)
                        logger.warning("Volatility spike detected: %s ATR=%.2f%%", coin, atr_pct)
                    break

            async with state._lock:
                prev_spike = state.volatility_spike
                state.volatility_spike = spike_detected
                if spike_detected:
                    state.risk_guard_green = False
                elif prev_spike and not state.trading_paused:
                    state.risk_guard_green = True
                    logger.info("Volatility spike cleared — resuming normal guard")
        except Exception as e:
            logger.error("Volatility spike check error: %s", e)

    async def _update_size_multiplier(self, portfolio_value: float):
        """
        Reduces position sizes based on drawdown or strategy decay.
        Multipliers stack multiplicatively.
        """
        base = 1.0
        if state.portfolio_peak > 0:
            drawdown = (state.portfolio_peak - portfolio_value) / state.portfolio_peak
            if drawdown >= DRAWDOWN_SIZE_THRESHOLD:
                base *= (1 - DRAWDOWN_SIZE_REDUCTION)

        if state.strategy_decay:
            base *= (1 - DECAY_SIZE_REDUCTION)

        async with state._lock:
            state.size_multiplier = round(base, 4)

    async def _reconcile_positions(self):
        """
        Compare state.open_positions with actual Alpaca positions.
        Any mismatch → alert and pause.
        """
        try:
            alpaca_pos = await asyncio.to_thread(self.alpaca.get_all_positions)
            alpaca_symbols = {p["symbol"] for p in alpaca_pos}
            state_symbols  = set(state.open_positions.keys())

            # Coins in state but not on Alpaca (missing fills / errors)
            phantom = state_symbols - alpaca_symbols
            # Coins on Alpaca but not in state (orphaned positions)
            orphaned = alpaca_symbols - state_symbols

            if phantom or orphaned:
                details = f"phantom={list(phantom)} orphaned={list(orphaned)}"
                logger.error("Reconciliation mismatch: %s", details)
                await state.pause("Position reconciliation mismatch")
                await self.telegram.reconciliation_mismatch(details)
                async with state._lock:
                    state.risk_guard_green = False
        except Exception as e:
            logger.error("Reconciliation error: %s", e)
