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

def _tf_to_minutes(tf):
    return tf.amount * {TimeFrameUnit.Minute:1, TimeFrameUnit.Hour:60, TimeFrameUnit.Day:1440}.get(tf.unit, 60)

class AlpacaClient:
    def __init__(self, api_key, secret_key):
        self.trading = TradingClient(api_key, secret_key, paper=True)
        self.data    = CryptoHistoricalDataClient(api_key, secret_key)

    def get_account(self):
        try: return self.trading.get_account()
        except Exception as e: logger.error("get_account: %s", e); return None

    def get_portfolio_value(self):
        try: return float(self.trading.get_account().portfolio_value)
        except Exception as e: logger.error("get_portfolio_value: %s", e); return 0.0

    def get_positions(self): return self.get_all_positions()

    def get_all_positions(self):
        try:
            return [{"symbol":p.symbol,"qty":float(p.qty),"avg_entry":float(p.avg_entry_price),"current_price":float(p.current_price),"unrealized_pnl":float(p.unrealized_pl),"market_value":float(p.market_value),"side":p.side.value} for p in self.trading.get_all_positions()]
        except Exception as e: logger.error("get_all_positions: %s", e); return []

    def get_latest_price(self, coin):
        try: return float(self.trading.get_open_position(coin).current_price)
        except Exception: pass
        try:
            from alpaca.data.requests import CryptoLatestQuoteRequest
            req = CryptoLatestQuoteRequest(symbol_or_symbols=[coin])
            data = self.data.get_crypto_latest_quote(req)
            if coin in data:
                q = data[coin]; return float((q.ask_price + q.bid_price) / 2)
        except Exception as e: logger.error("get_latest_price %s: %s", coin, e)
        return None

    def place_order(self, coin, qty, direction):
        try:
            order = self.trading.submit_order(MarketOrderRequest(symbol=coin, qty=qty, side=OrderSide.BUY if direction=="long" else OrderSide.SELL, time_in_force=TimeInForce.GTC))
            return str(order.id)
        except Exception as e: logger.error("place_order %s: %s", coin, e); return None

    def place_market_order(self, coin, qty, side):
        try:
            order = self.trading.submit_order(MarketOrderRequest(symbol=coin, qty=qty, side=OrderSide.BUY if side=="buy" else OrderSide.SELL, time_in_force=TimeInForce.GTC))
            return str(order.id)
        except Exception as e: logger.error("place_market_order %s: %s", coin, e); return None

    def place_limit_order(self, coin, qty, direction, limit_price):
        try:
            order = self.trading.submit_order(LimitOrderRequest(symbol=coin, qty=qty, side=OrderSide.BUY if direction=="long" else OrderSide.SELL, type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, limit_price=round(limit_price,2)))
            return str(order.id)
        except Exception as e: logger.error("place_limit_order %s: %s", coin, e); return None

    def close_position(self, coin):
        try: self.trading.close_position(coin); return True
        except Exception as e: logger.error("close_position %s: %s", coin, e); return False

    def get_bars(self, symbols, timeframe, limit=12):
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(minutes=_tf_to_minutes(timeframe)*(limit+5))
            bars = self.data.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=symbols, timeframe=timeframe, start=start, end=end, limit=limit))
            result = {}; bars_df = bars.df
            for sym in symbols:
                try:
                    if sym in bars_df.index.get_level_values(0):
                        sym_df = bars_df.loc[sym].reset_index()
                        result[sym] = [{"t":str(row.get("timestamp","")),"o":float(row.get("open",0)),"h":float(row.get("high",0)),"l":float(row.get("low",0)),"c":float(row.get("close",0)),"v":float(row.get("volume",0))} for _,row in sym_df.iterrows()]
                    else: result[sym] = []
                except Exception as ex: logger.warning("No bars for %s: %s", sym, ex); result[sym] = []
            return result
        except Exception as e: logger.error("get_bars error: %s", e); return {sym:[] for sym in symbols}
