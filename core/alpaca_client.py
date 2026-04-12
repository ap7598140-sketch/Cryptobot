"""
Alpaca paper-trading wrapper for crypto.
All order logic lives here; bots never call alpaca-py directly.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderStatus
from alpaca.trading.requests import LimitOrderRequest, GetOrdersRequest
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

logger = logging.getLogger(__name__)


def _alpaca_symbol(coin: str) -> str:
    """'BTC/USD' → 'BTC/USD' (Alpaca already uses this format)."""
    return coin


class AlpacaClient:
    def __init__(self, api_key: str, secret_key: str):
        self.trading = TradingClient(api_key, secret_key, paper=True)
        self.data = CryptoHistoricalDataClient(api_key, secret_key)

    # ── account ───────────────────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        try:
            acct = self.trading.get_account()
            return float(acct.portfolio_value)
        except Exception as e:
            logger.error("get_portfolio_value error: %s", e)
            return 0.0

    def get_cash(self) -> float:
        try:
            acct = self.trading.get_account()
            return float(acct.cash)
        except Exception as e:
            logger.error("get_cash error: %s", e)
            return 0.0

    # ── positions ─────────────────────────────────────────────────────────────

    def get_all_positions(self) -> list[dict]:
        try:
            positions = self.trading.get_all_positions()
            return [
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "avg_entry": float(p.avg_entry_price),
                    "current_price": float(p.current_price),
                    "unrealized_pnl": float(p.unrealized_pl),
                    "unrealized_pnl_pct": float(p.unrealized_plpc),
                    "market_value": float(p.market_value),
                    "side": p.side.value,
                }
                for p in positions
            ]
        except Exception as e:
            logger.error("get_all_positions error: %s", e)
            return []

    def get_position(self, coin: str) -> Optional[dict]:
        try:
            p = self.trading.get_open_position(_alpaca_symbol(coin))
            return {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pnl": float(p.unrealized_pl),
                "unrealized_pnl_pct": float(p.unrealized_plpc),
                "market_value": float(p.market_value),
                "side": p.side.value,
            }
        except Exception:
            return None

    # ── orders ────────────────────────────────────────────────────────────────

    def place_limit_order(
        self,
        coin: str,
        side: str,          # "buy" | "sell"
        qty: float,
        limit_price: float,
    ) -> Optional[str]:
        """Places a limit order; returns order_id or None on failure."""
        try:
            req = LimitOrderRequest(
                symbol=_alpaca_symbol(coin),
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                limit_price=round(limit_price, 2),
            )
            order = self.trading.submit_order(req)
            logger.info("Limit order placed: %s %s %.4f @ %.2f → id=%s",
                        side, coin, qty, limit_price, order.id)
            return str(order.id)
        except Exception as e:
            logger.error("place_limit_order error for %s: %s", coin, e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.trading.cancel_order_by_id(order_id)
            logger.info("Order %s cancelled", order_id)
            return True
        except Exception as e:
            logger.error("cancel_order error %s: %s", order_id, e)
            return False

    def get_order_status(self, order_id: str) -> Optional[str]:
        try:
            order = self.trading.get_order_by_id(order_id)
            return order.status.value
        except Exception as e:
            logger.error("get_order_status error %s: %s", order_id, e)
            return None

    def close_position(self, coin: str) -> bool:
        """Market close of entire position — used ONLY for hard stops."""
        try:
            self.trading.close_position(_alpaca_symbol(coin))
            logger.info("Position closed (market): %s", coin)
            return True
        except Exception as e:
            logger.error("close_position error for %s: %s", coin, e)
            return False

    def get_open_orders(self) -> list[dict]:
        try:
            req = GetOrdersRequest(status="open")
            orders = self.trading.get_orders(req)
            return [
                {
                    "id": str(o.id),
                    "symbol": o.symbol,
                    "side": o.side.value,
                    "qty": float(o.qty or 0),
                    "filled_qty": float(o.filled_qty or 0),
                    "limit_price": float(o.limit_price or 0),
                    "status": o.status.value,
                }
                for o in orders
            ]
        except Exception as e:
            logger.error("get_open_orders error: %s", e)
            return []

    # ── market data ───────────────────────────────────────────────────────────

    def get_bars(
        self,
        symbols: list[str],
        timeframe: TimeFrame,
        limit: int = 12,
    ) -> dict[str, list[dict]]:
        """
        Returns {symbol: [{"t":..., "o":..., "h":..., "l":..., "c":..., "v":...}, ...]}
        sorted oldest → newest.
        """
        try:
            end = datetime.now(timezone.utc)
            # Compute rough start based on timeframe + limit + buffer
            tf_minutes = _tf_to_minutes(timeframe)
            start = end - timedelta(minutes=tf_minutes * (limit + 5))
            req = CryptoBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=timeframe,
                start=start,
                end=end,
                limit=limit,
            )
            bars = self.data.get_crypto_bars(req)
            result: dict[str, list[dict]] = {}
            for sym in symbols:
                sym_bars = bars.get(sym, [])
                result[sym] = [
                    {
                        "t": b.timestamp.isoformat(),
                        "o": float(b.open),
                        "h": float(b.high),
                        "l": float(b.low),
                        "c": float(b.close),
                        "v": float(b.volume),
                    }
                    for b in sym_bars
                ]
            return result
        except Exception as e:
            logger.error("get_bars error: %s", e)
            return {sym: [] for sym in symbols}


def _tf_to_minutes(tf: TimeFrame) -> int:
    unit = tf.unit
    amount = tf.amount
    mapping = {
        TimeFrameUnit.Minute: 1,
        TimeFrameUnit.Hour: 60,
        TimeFrameUnit.Day: 1_440,
    }
    return amount * mapping.get(unit, 60)


# Convenient timeframe constants
TF_15MIN = TimeFrame(15, TimeFrameUnit.Minute)
TF_1HR   = TimeFrame(1,  TimeFrameUnit.Hour)
TF_4HR   = TimeFrame(4,  TimeFrameUnit.Hour)
