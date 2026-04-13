"""
External market data fetching:
  - Technical indicators (RSI, MACD, ATR, CVD) from OHLCV bars
  - Asian session range
  - Binance funding rates (free, no auth)
  - Coinglass liquidation levels (free tier, optional key)
  - alternative.me Fear & Greed Index (free)
  - CoinGecko BTC dominance (free)
  - CoinGecko stablecoin market cap delta (free, optional)
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import pandas as pd
import numpy as np
import ta

logger = logging.getLogger(__name__)

# ── Symbol mapping helpers ────────────────────────────────────────────────────

def to_binance_symbol(coin: str) -> str:
    """'BTC/USD' → 'BTCUSDT'"""
    return coin.replace("/USD", "USDT")


def to_coingecko_id(coin: str) -> str:
    mapping = {"BTC/USD": "bitcoin", "ETH/USD": "ethereum", "SOL/USD": "solana"}
    return mapping.get(coin, coin.split("/")[0].lower())


# ── Technical indicators ──────────────────────────────────────────────────────

def bars_to_df(bars: list[dict]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}, inplace=True)
    df["open"]   = pd.to_numeric(df["open"])
    df["high"]   = pd.to_numeric(df["high"])
    df["low"]    = pd.to_numeric(df["low"])
    df["close"]  = pd.to_numeric(df["close"])
    df["volume"] = pd.to_numeric(df["volume"])
    return df


def calc_indicators(df: pd.DataFrame, rsi_oversold: int = 30, rsi_overbought: int = 70) -> dict:
    """Calculate RSI, MACD, ATR, and CVD from an OHLCV DataFrame."""
    if df.empty or len(df) < 3:
        return {}
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["volume"]

    # RSI
    rsi_ind = ta.momentum.RSIIndicator(close=close, window=min(14, len(df) - 1))
    rsi_val = float(rsi_ind.rsi().iloc[-1]) if len(df) > 2 else 50.0

    # MACD
    macd_ind  = ta.trend.MACD(close=close)
    macd_diff = float(macd_ind.macd_diff().iloc[-1]) if len(df) > 5 else 0.0
    macd_line = float(macd_ind.macd().iloc[-1]) if len(df) > 5 else 0.0

    # ATR
    atr_ind = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=min(14, len(df) - 1))
    atr_val = float(atr_ind.average_true_range().iloc[-1])

    # CVD — candle-by-candle: add vol if close > open, subtract otherwise
    direction = np.where(df["close"] >= df["open"], 1, -1)
    cvd_series = (df["volume"] * direction).cumsum()
    cvd_val = float(cvd_series.iloc[-1])
    cvd_delta = float((df["volume"] * direction).iloc[-1])  # single-candle delta

    current_price = float(close.iloc[-1])
    atr_pct = atr_val / current_price if current_price else 0.0

    # Divergence: price direction vs RSI direction over last 3 candles
    price_dir = float(close.iloc[-1]) - float(close.iloc[-3]) if len(df) >= 3 else 0
    rsi_series = rsi_ind.rsi()
    rsi_dir = float(rsi_series.iloc[-1]) - float(rsi_series.iloc[-3]) if len(rsi_series) >= 3 else 0
    divergence = "bearish" if price_dir > 0 and rsi_dir < 0 else "bullish" if price_dir < 0 and rsi_dir > 0 else "none"

    # Market structure: simple HH/HL vs LH/LL over last 4 candles
    structure = _market_structure(df)

    return {
        "rsi": round(rsi_val, 2),
        "rsi_signal": _rsi_signal(rsi_val, rsi_oversold, rsi_overbought),
        "macd_diff": round(macd_diff, 4),
        "macd_line": round(macd_line, 4),
        "macd_signal": "bullish" if macd_diff > 0 else "bearish",
        "atr": round(atr_val, 4),
        "atr_pct": round(atr_pct * 100, 3),
        "cvd": round(cvd_val, 2),
        "cvd_delta": round(cvd_delta, 2),
        "cvd_signal": "positive" if cvd_val > 0 else "negative",
        "divergence": divergence,
        "market_structure": structure,
        "current_price": current_price,
    }


def _rsi_signal(rsi: float, oversold: int, overbought: int) -> str:
    if rsi <= oversold:
        return "oversold"
    if rsi >= overbought:
        return "overbought"
    return "neutral"


def _market_structure(df: pd.DataFrame) -> str:
    if len(df) < 4:
        return "unknown"
    highs  = df["high"].values[-4:]
    lows   = df["low"].values[-4:]
    hh = highs[-1] > highs[-2] > highs[-3]
    hl = lows[-1] > lows[-2] > lows[-3]
    lh = highs[-1] < highs[-2] < highs[-3]
    ll = lows[-1] < lows[-2] < lows[-3]
    if hh and hl:
        return "uptrend"
    if lh and ll:
        return "downtrend"
    return "ranging"


def calc_asian_session_range(hourly_bars: list[dict]) -> dict:
    """
    Asian session ≈ 00:00–12:00 UTC (approximates 8pm–8am ET).
    Returns high, low, and whether current price is inside/above/below range.
    """
    if not hourly_bars:
        return {"asian_high": None, "asian_low": None, "position": "unknown"}
    df = bars_to_df(hourly_bars)
    # Filter today's Asian session hours (UTC 0-12)
    df["hour"] = pd.to_datetime(df["t"]).dt.hour
    asian = df[df["hour"] < 12]
    if asian.empty:
        return {"asian_high": None, "asian_low": None, "position": "unknown"}
    asian_high = float(asian["high"].max())
    asian_low  = float(asian["low"].min())
    current    = float(df["close"].iloc[-1])
    if current > asian_high:
        position = "above"
    elif current < asian_low:
        position = "below"
    else:
        position = "inside"
    return {
        "asian_high": round(asian_high, 2),
        "asian_low":  round(asian_low, 2),
        "position":   position,
    }


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _get(session: aiohttp.ClientSession, url: str, params: dict = None,
               headers: dict = None, timeout: int = 10) -> Optional[dict | list]:
    try:
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.warning("HTTP %d for %s", resp.status, url)
    except asyncio.TimeoutError:
        logger.warning("Timeout fetching %s", url)
    except Exception as e:
        logger.error("Error fetching %s: %s", url, e)
    return None


# ── Funding rate ──────────────────────────────────────────────────────────────
# Binance futures API returns HTTP 451 (geo-blocked in the US).
# Return a neutral 0.0 so downstream logic continues unaffected.

async def get_funding_rate(coin: str) -> Optional[float]:
    """Returns funding rate decimal. Currently returns neutral 0.0 (Binance geo-blocked)."""
    return 0.0


# ── Coinglass liquidation levels ──────────────────────────────────────────────

async def get_liquidation_levels(coin: str, api_key: str = "") -> Optional[dict]:
    """
    Returns approximate liquidation cluster data.
    Uses free public endpoint; with api_key uses authenticated endpoint.
    """
    symbol = coin.split("/")[0]  # "BTC"
    headers = {}
    if api_key:
        headers["coinglassSecret"] = api_key
        url = "https://open-api.coinglass.com/public/v2/liquidation_chart"
        params = {"symbol": symbol, "timeType": "h1"}
    else:
        # Public endpoint (limited)
        url = "https://open-api.coinglass.com/api/pro/v1/futures/liquidation_chart"
        params = {"symbol": symbol}
    async with aiohttp.ClientSession() as session:
        data = await _get(session, url, params=params, headers=headers)
    if not data:
        return None
    # Parse varies; return raw if non-empty
    return {"symbol": symbol, "data": data}


# ── alternative.me Fear & Greed ───────────────────────────────────────────────

async def get_fear_and_greed() -> dict:
    """Returns {value: int, classification: str}."""
    url = "https://api.alternative.me/fng/"
    async with aiohttp.ClientSession() as session:
        data = await _get(session, url, params={"limit": 1})
    if data and "data" in data and data["data"]:
        entry = data["data"][0]
        return {
            "value": int(entry.get("value", 50)),
            "classification": entry.get("value_classification", "Neutral"),
        }
    return {"value": 50, "classification": "Neutral"}


# ── CoinGecko BTC dominance ───────────────────────────────────────────────────

async def get_btc_dominance(api_key: str = "") -> float:
    """Returns BTC market dominance percentage."""
    url  = "https://api.coingecko.com/api/v3/global"
    headers = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    async with aiohttp.ClientSession() as session:
        data = await _get(session, url, headers=headers)
    if data and "data" in data:
        return float(data["data"].get("market_cap_percentage", {}).get("btc", 50.0))
    return 50.0


async def get_btc_dominance_trend(api_key: str = "") -> str:
    """
    Compares BTC dominance to 24h ago.
    Returns 'rising', 'falling', or 'stable'.
    """
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    params = {"vs_currency": "usd", "days": 2, "interval": "hourly"}
    headers = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    async with aiohttp.ClientSession() as session:
        data = await _get(session, url, params=params, headers=headers)
    if not data or "market_caps" not in data:
        return "stable"
    mc = data["market_caps"]
    if len(mc) < 2:
        return "stable"
    # total global mc not available here — proxy with BTC MC direction
    current_btc_mc = mc[-1][1]
    past_btc_mc    = mc[-24][1] if len(mc) >= 24 else mc[0][1]
    delta = (current_btc_mc - past_btc_mc) / past_btc_mc if past_btc_mc else 0
    if delta > 0.01:
        return "rising"
    if delta < -0.01:
        return "falling"
    return "stable"


# ── Stablecoin flow proxy ──────────────────────────────────────────────────────

async def get_stablecoin_flow(api_key: str = "") -> str:
    """
    Returns 'inflow', 'outflow', or 'neutral' based on combined USDT+USDC
    24h market cap change direction (positive = inflow to crypto ecosystem).
    """
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": "tether,usd-coin",
        "order": "market_cap_desc",
        "per_page": 2,
        "page": 1,
        "price_change_percentage": "24h",
    }
    headers = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    async with aiohttp.ClientSession() as session:
        data = await _get(session, url, params=params, headers=headers)
    if not data or not isinstance(data, list):
        return "neutral"
    avg_change = sum(c.get("market_cap_change_percentage_24h", 0) for c in data) / max(len(data), 1)
    if avg_change > 0.5:
        return "inflow"
    if avg_change < -0.5:
        return "outflow"
    return "neutral"


# ── Aggregate for scanner ─────────────────────────────────────────────────────

async def get_external_market_context(
    coins: list[str],
    coinglass_key: str = "",
    coingecko_key: str = "",
) -> dict:
    """
    Fetch all external data in parallel for a list of coins.
    Returns a dict keyed by coin with funding rates, plus global data.
    """
    # Parallel fetch of all external sources
    results = await asyncio.gather(
        *[get_funding_rate(c) for c in coins],
        get_fear_and_greed(),
        get_btc_dominance_trend(coingecko_key),
        get_stablecoin_flow(coingecko_key),
        return_exceptions=True,
    )

    n = len(coins)
    funding_rates = {}
    for i, coin in enumerate(coins):
        r = results[i]
        funding_rates[coin] = float(r) if isinstance(r, (int, float)) else 0.0

    fear_greed    = results[n] if not isinstance(results[n], Exception) else {"value": 50, "classification": "Neutral"}
    btc_dom_trend = results[n+1] if not isinstance(results[n+1], Exception) else "stable"
    stable_flow   = results[n+2] if not isinstance(results[n+2], Exception) else "neutral"

    return {
        "funding_rates":   funding_rates,
        "fear_greed":      fear_greed,
        "btc_dom_trend":   btc_dom_trend,
        "stablecoin_flow": stable_flow,
    }
