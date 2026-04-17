"""
Alpaca paper-trading wrapper for crypto.
Uses bars.df to parse BarSet responses correctly.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, GetOrdersRequest
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

logger = logging.getLogger(__name__)

TF_15MIN = TimeFrame(15, TimeFrameUnit.Minute)
TF_1HR   = TimeFrame(1,  TimeFrameUnit.Hour)
TF_4HR   = TimeFrame(4,  TimeFrameUnit.Hour)


def _tf_to_minutes(tf: TimeFrame) -> int:
    mapping = {TimeFrameUnit.Minute: 1, TimeFrameUnit.Hour: 60, TimeFrameUnit.Day: 1_440}
    return tf.amount * mapping.get(tf.unit, 60)


class AlpacaClient:
    def __init__(self, api_key: str, secret_key: str):
        self.trading = TradingClient(api_key, secret_key, paper=True)
        self.data    = CryptoHistoricalDataClient(api_key, secret_key)

    def get_account(self):
        try:
            return self.trading.get_account()
        except Exception as e:
            logger.error("get_account: %s", e)
            return None

    def get_portfolio_value(self) -> float:
        try:
            return float(self.trading.get_account().portfolio_value)
        except Exception as e:
            logger.error("get_portfolio_value: %s", e)
            return 0.0

    def get_cash(self) -> float:
        try:
            return float(self.trading.get_account().cash)
        except Exception as e:
            logger.error("get_cash: %s", e)
            return 0.0

    def get_positions(self) -> list[dict]:
        return self.get_all_positions()

    def get_all_positions(self) -> list[dict]:
        try:
            return [
                {
                    "symbol":             p.symbol,
                    "qty":                float(p.qty),
                    "avg_entry":          float(p.avg_entry_price),
                    "current_price":      float(p.current_price),
                    "unrealized_pnl":     float(p.unrealized_pl),
                    "unrealized_pnl_pct": float(p.unrealized_plpc),
                    "market_value":       float(p.market_value),
                    "side":               p.side.value,
                }
                for p in self.trading.get_all_positions()
            ]
        except Exception as e:
            logger.error("get_all_positions: %s", e)
            return []

    def get_position(self, coin: str) -> Optional[dict]:
        try:
            p = self.trading.get_open_position(coin)
            return {
                "symbol":         p.symbol,
                "qty":            float(p.qty),
                "avg_entry":      float(p.avg_entry_price),
                "current_price":  float(p.current_price),
                "unrealized_pnl": float(p.unrealized_pl),
                "market_value":   float(p.market_value),
                "side":           p.side.value,
            }
        except Exception:
            return None

    def get_latest_price(self, coin: str) -> Optional[float]:
        try:
            pos = self.trading.get_open_position(coin)
            return float(pos.current_price)
        except Exception:
            pass
        try:
            from alpaca.data.requests import CryptoLatestQuoteRequest
            req  = CryptoLatestQuoteRequest(symbol_or_symbols=[coin])
            data = self.data.get_crypto_latest_quote(req)
            if coin in data:
                q = data[coin]
                return float((q.ask_price + q.bid_price) / 2)
        except Exception as e:
            logger.error("get_latest_price %s: %s", coin, e)
        return None

    def place_order(self, coin: str, qty: float, direction: str) -> Optional[str]:
        side = OrderSide.BUY if direction == "long" else OrderSide.SELL
        try:
            req   = MarketOrderRequest(symbol=coin, qty=qty, side=side,
                                       time_in_force=TimeInForce.GTC)
            order = self.trading.submit_order(req)
            logger.info("Market order: %s %s %.6f id=%s", direction, coin, qty, order.id)
            return str(order.id)
        except Exception as e:
            logger.error("place_order %s: %s", coin, e)
            return None

    def place_market_order(self, coin: str, qty: float, side: str) -> Optional[str]:
        try:
            req   = MarketOrderRequest(
                symbol=coin, qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
            )
            order = self.trading.submit_order(req)
            logger.info("Market order: %s %s %.6f id=%s", side, coin, qty, order.id)
            return str(order.id)
        except Exception as e:
            logger.error("place_market_order %s: %s", coin, e)
            return None

    def place_limit_order(self, coin: str, qty: float, direction: str,
                          limit_price: float) -> Optional[str]:
        side = OrderSide.BUY if direction == "long" else OrderSide.SELL
        try:
            req = LimitOrderRequest(
                symbol=coin, qty=qty, side=side,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                limit_price=round(limit_price, 2),
            )
            order = self.trading.submit_order(req)
            logger.info("Limit order: %s %s %.6f @ %.2f id=%s",
                        direction, coin, qty, limit_price, order.id)
            return str(order.id)
        except Exception as e:
            logger.error("place_limit_order %s: %s", coin, e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.trading.cancel_order_by_id(order_id)
            return True
        except Exception as e:
            logger.error("cancel_order %s: %s", order_id, e)
            return False

    def get_order_status(self, order_id: str) -> Optional[str]:
        try:
            return self.trading.get_order_by_id(order_id).status.value
        except Exception as e:
            logger.error("get_order_status %s: %s", order_id, e)
            return None

    def close_position(self, coin: str) -> bool:
        try:
            self.trading.close_position(coin)
            logger.info("Closed position: %s", coin)
            return True
        except Exception as e:
            logger.error("close_position %s: %s", coin, e)
            return False

    def get_open_orders(self) -> list[dict]:
        try:
            return [
                {
                    "id":          str(o.id),
                    "symbol":      o.symbol,
                    "side":        o.side.value,
                    "qty":         float(o.qty or 0),
                    "filled_qty":  float(o.filled_qty or 0),
                    "limit_price": float(o.limit_price or 0),
                    "status":      o.status.value,
                }
                for o in self.trading.get_orders(GetOrdersRequest(status="open"))
            ]
        except Exception as e:
            logger.error("get_open_orders: %s", e)
            return []

    def get_bars(self, symbols: list[str], timeframe: TimeFrame,
                 limit: int = 12) -> dict[str, list[dict]]:
        """
        Returns {symbol: [{"t":..., "o":..., "h":..., "l":..., "c":..., "v":...}]}
        Uses bars.df (MultiIndex DataFrame) to parse the BarSet correctly.
        """
        try:
            end   = datetime.now(timezone.utc)
            start = end - timedelta(minutes=_tf_to_minutes(timeframe) * (limit + 5))
            req   = CryptoBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=timeframe,
                start=start,
                end=end,
                limit=limit,
            )
            bars    = self.data.get_crypto_bars(req)
            result: dict[str, list[dict]] = {}
            bars_df = bars.df
            for sym in symbols:
                try:
                    if sym in bars_df.index.get_level_values(0):
                        sym_df = bars_df.loc[sym].reset_index()
                        result[sym] = [
                            {
                                "t": str(row.get("timestamp", "")),
                                "o": float(row.get("open",   0)),
                                "h": float(row.get("high",   0)),
                                "l": float(row.get("low",    0)),
                                "c": float(row.get("close",  0)),
                                "v": float(row.get("volume", 0)),
                            }
                            for _, row in sym_df.iterrows()
                        ]
                    else:
                        result[sym] = []
                except Exception as ex:
                    logger.warning("No bars for %s: %s", sym, ex)
                    result[sym] = []
            return result
        except Exception as e:
            logger.error("get_bars error: %s", e)
            return {sym: [] for sym in symbols}
