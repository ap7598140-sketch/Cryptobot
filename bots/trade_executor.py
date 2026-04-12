"""
Bot 3 — Trade Executor (Sonnet)
Triggers on confirmed signals from Trend Analyzer.
  - Enforces 2:1 reward-to-risk
  - Avoids round numbers
  - Limit orders only
  - Scaled entry: 50 % first → 50 % confirm if filled within 5 min
  - Cancels unfilled orders after 5 min
  - Must have Risk Guard green before every order
"""
import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone

from core.alpaca_client import AlpacaClient
from core.claude_client import ClaudeClient
from core.state import TradingState, Position, state
from core.telegram_notifier import TelegramNotifier
from settings import (
    CASH_RESERVE_PCT, MAX_POSITIONS, MAX_RISK_PER_TRADE,
    HARD_STOP_PCT, TAKE_PROFIT_PCT, PARTIAL_EXIT_PCT,
    ENTRY_FIRST_FRAC, ENTRY_CONFIRM_FRAC, ORDER_TIMEOUT_S,
    ROUND_NUM_OFFSET_PCT, MIN_REWARD_RISK, LOGS_DIR,
    MAX_TOKENS_EXECUTOR,
)

logger = logging.getLogger(__name__)


def _avoid_round_number(price: float, direction: str) -> float:
    """
    Shift entry/stop slightly away from round numbers to avoid stop-hunting.
    For long entries: enter slightly above the round number.
    For short entries: enter slightly below.
    """
    magnitude = 10 ** math.floor(math.log10(max(price, 1)))
    remainder = price % magnitude
    is_round  = remainder < magnitude * 0.005 or remainder > magnitude * 0.995
    if not is_round:
        return round(price, 2)
    offset = price * ROUND_NUM_OFFSET_PCT
    if direction == "long":
        return round(price + offset, 2)
    return round(price - offset, 2)


def _calc_position_size(portfolio_value: float, entry_price: float,
                         stop_price: float, size_multiplier: float,
                         cash_available: float, n_open: int) -> float:
    """
    Risk-based position sizing:
      max_risk_$ = portfolio_value * MAX_RISK_PER_TRADE
      qty = max_risk_$ / |entry - stop| per unit
    Also bounded by:
      - 40 % cash reserve
      - max 30 % portfolio per position (to stay within 2-position limit)
    """
    risk_per_trade_usd = portfolio_value * MAX_RISK_PER_TRADE * size_multiplier
    price_risk = abs(entry_price - stop_price)
    if price_risk <= 0:
        return 0.0

    qty_by_risk = risk_per_trade_usd / price_risk

    # Max notional we can use
    reserve       = portfolio_value * CASH_RESERVE_PCT
    deployable    = max(cash_available - reserve, 0)
    max_per_trade = portfolio_value * 0.30   # 30 % per position
    max_notional  = min(deployable, max_per_trade)

    qty_by_capital = max_notional / entry_price
    qty = min(qty_by_risk, qty_by_capital)
    return round(qty, 6)


def _executor_prompt(sig: dict, portfolio_value: float, cash: float) -> str:
    rr = (sig['target'] - sig['entry']) / (sig['entry'] - sig['stop']) \
        if sig['entry'] != sig['stop'] else 0
    return (
        f"Validate & finalise trade parameters. {sig['coin']} {sig['direction'].upper()}\n"
        f"score={sig['final_score']} entry={sig['entry']:.2f} "
        f"stop={sig['stop']:.2f} target={sig['target']:.2f} "
        f"rr_ratio={rr:.2f} portfolio={portfolio_value:.2f} cash={cash:.2f}\n"
        f"Rules: min_rr={MIN_REWARD_RISK} max_risk={MAX_RISK_PER_TRADE*100:.1f}% "
        f"stop_pct={HARD_STOP_PCT*100:.0f}% target_pct={TAKE_PROFIT_PCT*100:.0f}%\n"
        f"Return: {{\"approved\":true,\"entry\":{sig['entry']:.2f},"
        f"\"stop\":{sig['stop']:.2f},\"target\":{sig['target']:.2f},"
        f"\"reason\":\"brief\"}}"
    )


class TradeExecutor:
    def __init__(self, claude: ClaudeClient, alpaca: AlpacaClient,
                 executor_queue: asyncio.Queue, telegram: TelegramNotifier):
        self.claude   = claude
        self.alpaca   = alpaca
        self.queue    = executor_queue
        self.telegram = telegram

    async def run(self):
        logger.info("Trade Executor started")
        while not state.stop_command:
            try:
                sig = await asyncio.wait_for(self.queue.get(), timeout=5.0)
                await self._execute(sig)
                self.queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error("Executor error: %s", exc)

    async def _execute(self, sig: dict):
        coin      = sig["coin"]
        direction = sig["direction"]

        # ── Risk Guard check ──────────────────────────────────────────────────
        if not state.risk_guard_green or await state.is_halted():
            logger.info("Risk Guard red/paused — skipping %s", coin)
            return

        # ── Max positions guard ───────────────────────────────────────────────
        async with state._lock:
            n_open = len(state.open_positions)
            if n_open >= MAX_POSITIONS:
                logger.info("Max positions reached — skipping %s", coin)
                return
            # Correlation block: never hold > 1 of BTC/ETH/SOL
            crypto_coins = {"BTC/USD", "ETH/USD", "SOL/USD"}
            if coin in crypto_coins and any(c in crypto_coins for c in state.open_positions):
                logger.info("Correlation block — already in a major crypto — skipping %s", coin)
                return

        # ── Enforce 2:1 R:R ───────────────────────────────────────────────────
        entry  = float(sig["entry"])
        stop   = float(sig["stop"])
        target = float(sig["target"])
        price_risk = abs(entry - stop)
        price_rew  = abs(target - entry)
        if price_risk == 0 or price_rew / price_risk < MIN_REWARD_RISK:
            logger.info("R:R %.2f below min %.1f — cancelling %s",
                        price_rew / max(price_risk, 0.001), MIN_REWARD_RISK, coin)
            return

        # ── Avoid round numbers ───────────────────────────────────────────────
        entry  = _avoid_round_number(entry, direction)
        stop   = _avoid_round_number(stop, "short" if direction == "long" else "long")
        target = _avoid_round_number(target, direction)

        # ── Get live account data ─────────────────────────────────────────────
        portfolio_value = await asyncio.to_thread(self.alpaca.get_portfolio_value)
        cash            = await asyncio.to_thread(self.alpaca.get_cash)
        await state.update_portfolio_value(portfolio_value)

        # ── Claude validation ─────────────────────────────────────────────────
        prompt     = _executor_prompt({**sig, "entry": entry, "stop": stop, "target": target},
                                      portfolio_value, cash)
        validation = await self.claude.executor_call(prompt)

        if not validation or not validation.get("approved"):
            logger.info("Executor Claude rejected trade: %s reason=%s",
                        coin, validation.get("reason") if validation else "no response")
            return

        # Use Claude's potentially adjusted levels if provided
        entry  = float(validation.get("entry", entry))
        stop   = float(validation.get("stop", stop))
        target = float(validation.get("target", target))

        # ── Position sizing ───────────────────────────────────────────────────
        async with state._lock:
            n_open       = len(state.open_positions)
            size_mult    = state.size_multiplier
        total_qty    = _calc_position_size(portfolio_value, entry, stop,
                                           size_mult, cash, n_open)
        if total_qty <= 0:
            logger.info("Position size zero — insufficient capital for %s", coin)
            return

        first_qty = round(total_qty * ENTRY_FIRST_FRAC, 6)
        if first_qty <= 0:
            return

        side = "buy" if direction == "long" else "sell"

        # ── Re-check Risk Guard immediately before order ──────────────────────
        if not state.risk_guard_green:
            logger.info("Risk Guard went red before order — aborting %s", coin)
            return

        # ── Place first limit order (50 %) ────────────────────────────────────
        order_id = await asyncio.to_thread(
            self.alpaca.place_limit_order, coin, side, first_qty, entry
        )
        if not order_id:
            logger.error("Failed to place first order for %s", coin)
            return

        # ── Register position in state ────────────────────────────────────────
        now = datetime.now(timezone.utc)
        position = Position(
            coin=coin, direction=direction,
            entry_price=entry, quantity=total_qty, filled_qty=0.0,
            stop_price=stop, target_price=target,
            score=sig["final_score"], opened_at=now,
            pending_order_id=order_id,
        )
        await state.add_position(position)

        notional = first_qty * entry
        await self.telegram.trade_opened(coin, direction, entry, first_qty,
                                         sig["final_score"], notional)
        logger.info("First order placed: %s %s %.6f @ %.2f", coin, direction, first_qty, entry)

        # ── Wait up to 5 min for fill, then place second half ─────────────────
        asyncio.create_task(self._manage_scaled_entry(
            coin, order_id, total_qty, entry, stop, target, direction
        ))

        _log_trade(sig, entry, stop, target, total_qty, order_id)

    async def _manage_scaled_entry(self, coin: str, first_order_id: str,
                                   total_qty: float, entry: float, stop: float,
                                   target: float, direction: str):
        """
        Wait ORDER_TIMEOUT_S for first fill.
        If filled, place second 50 % immediately.
        If not filled, cancel and remove position.
        """
        await asyncio.sleep(ORDER_TIMEOUT_S)

        status = await asyncio.to_thread(self.alpaca.get_order_status, first_order_id)
        pos    = state.open_positions.get(coin)
        if not pos:
            return

        if status not in ("filled", "partially_filled"):
            # Cancel and clean up
            await asyncio.to_thread(self.alpaca.cancel_order, first_order_id)
            await state.remove_position(coin)
            logger.info("First order unfilled after %ds — cancelled %s", ORDER_TIMEOUT_S, coin)
            return

        # Mark first fill
        async with state._lock:
            if coin in state.open_positions:
                state.open_positions[coin].filled_qty = total_qty * ENTRY_FIRST_FRAC
                state.open_positions[coin].pending_order_id = None

        # Place second 50 %
        second_qty = round(total_qty * ENTRY_CONFIRM_FRAC, 6)
        if second_qty <= 0 or not state.risk_guard_green:
            return

        side = "buy" if direction == "long" else "sell"
        second_id = await asyncio.to_thread(
            self.alpaca.place_limit_order, coin, side, second_qty, entry
        )
        if second_id:
            async with state._lock:
                if coin in state.open_positions:
                    state.open_positions[coin].pending_order_id = second_id
                    state.open_positions[coin].second_entry_placed = True
            logger.info("Second order placed: %s %.6f @ %.2f", coin, second_qty, entry)

            # Wait again for second fill
            await asyncio.sleep(ORDER_TIMEOUT_S)
            status2 = await asyncio.to_thread(self.alpaca.get_order_status, second_id)
            if status2 not in ("filled", "partially_filled"):
                await asyncio.to_thread(self.alpaca.cancel_order, second_id)
                logger.info("Second order unfilled — cancelled for %s", coin)
            else:
                async with state._lock:
                    if coin in state.open_positions:
                        state.open_positions[coin].filled_qty = total_qty
                        state.open_positions[coin].pending_order_id = None


def _log_trade(sig: dict, entry: float, stop: float, target: float,
               qty: float, order_id: str):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"trade_{sig['coin'].replace('/','_')}_{ts}.json")
    record = {
        "coin": sig["coin"], "direction": sig["direction"],
        "entry": entry, "stop": stop, "target": target, "qty": qty,
        "score": sig.get("final_score"), "order_id": order_id,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
    except Exception as e:
        logger.error("Failed to write trade log: %s", e)
