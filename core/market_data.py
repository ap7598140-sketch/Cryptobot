import asyncio, logging
from typing import Optional
import aiohttp, pandas as pd, numpy as np, ta

logger = logging.getLogger(__name__)

def bars_to_df(bars):
    if not bars: return pd.DataFrame()
    df = pd.DataFrame(bars)
    df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)
    for col in ["open","high","low","close","volume"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close"])

def calc_indicators(df, rsi_oversold=30, rsi_overbought=70):
    if df.empty or len(df) < 3: return {}
    close,high,low,vol = df["close"],df["high"],df["low"],df["volume"]
    rsi_ind = ta.momentum.RSIIndicator(close=close, window=min(14,len(df)-1))
    rsi_val = float(rsi_ind.rsi().iloc[-1])
    macd_ind = ta.trend.MACD(close=close)
    macd_diff = float(macd_ind.macd_diff().iloc[-1]) if len(df)>5 else 0.0
    atr_ind = ta.volatility.AverageTrueRange(high=high,low=low,close=close,window=min(14,len(df)-1))
    atr_val = float(atr_ind.average_true_range().iloc[-1])
    direction = np.where(df["close"]>=df["open"],1,-1)
    cvd = float((vol*direction).cumsum().iloc[-1])
    current_price = float(close.iloc[-1])
    atr_pct = atr_val/current_price if current_price else 0.0
    price_dir = float(close.iloc[-1])-float(close.iloc[-3]) if len(df)>=3 else 0
    rsi_s = rsi_ind.rsi()
    rsi_dir = float(rsi_s.iloc[-1])-float(rsi_s.iloc[-3]) if len(rsi_s)>=3 else 0
    divergence = "bearish" if price_dir>0 and rsi_dir<0 else "bullish" if price_dir<0 and rsi_dir>0 else "none"
    return {"rsi":round(rsi_val,2),"rsi_signal":"oversold" if rsi_val<=rsi_oversold else "overbought" if rsi_val>=rsi_overbought else "neutral","macd_diff":round(macd_diff,4),"macd_signal":"bullish" if macd_diff>0 else "bearish","atr":round(atr_val,4),"atr_pct":round(atr_pct*100,3),"cvd":round(cvd,2),"cvd_signal":"positive" if cvd>0 else "negative","divergence":divergence,"market_structure":_market_structure(df),"current_price":current_price}

def _market_structure(df):
    if len(df)<4: return "unknown"
    highs,lows = df["high"].values[-4:],df["low"].values[-4:]
    if highs[-1]>highs[-2]>highs[-3] and lows[-1]>lows[-2]>lows[-3]: return "uptrend"
    if highs[-1]<highs[-2]<highs[-3] and lows[-1]<lows[-2]<lows[-3]: return "downtrend"
    return "ranging"

def detect_grid_range(df):
    if df.empty or len(df)<6: return {"market_type":"trending","support":None,"resistance":None}
    rh=float(df["high"].tail(6).max()); rl=float(df["low"].tail(6).min())
    avg=(rh+rl)/2; pr=(rh-rl)/avg if avg else 0
    atr=float(ta.volatility.AverageTrueRange(high=df["high"],low=df["low"],close=df["close"],window=min(14,len(df)-1)).average_true_range().iloc[-1])
    atr_pct=atr/avg if avg else 0
    return {"market_type":"ranging" if atr_pct<0.008 and pr<0.04 else "trending","support":round(rl,2),"resistance":round(rh,2),"range_pct":round(pr*100,2)}

def calc_asian_session_range(hourly_bars):
    if not hourly_bars: return {"asian_high":None,"asian_low":None,"position":"unknown"}
    df=bars_to_df(hourly_bars)
    if df.empty: return {"asian_high":None,"asian_low":None,"position":"unknown"}
    df["hour"]=pd.to_datetime(df["t"]).dt.hour; asian=df[df["hour"]<12]
    if asian.empty: return {"asian_high":None,"asian_low":None,"position":"unknown"}
    ah,al=float(asian["high"].max()),float(asian["low"].min()); current=float(df["close"].iloc[-1])
    return {"asian_high":round(ah,2),"asian_low":round(al,2),"position":"above" if current>ah else "below" if current<al else "inside"}

async def get_funding_rate(coin): return 0.0

async def _get(session, url, params=None, headers=None):
    try:
        async with session.get(url,params=params,headers=headers,timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json() if r.status==200 else None
    except Exception as e: logger.error("HTTP error %s: %s", url, e); return None

async def get_fear_and_greed():
    async with aiohttp.ClientSession() as s:
        data = await _get(s,"https://api.alternative.me/fng/",params={"limit":1})
    if data and "data" in data and data["data"]:
        e=data["data"][0]; return {"value":int(e.get("value",50)),"classification":e.get("value_classification","Neutral")}
    return {"value":50,"classification":"Neutral"}

async def get_btc_dominance_trend(api_key=""):
    headers={"x-cg-demo-api-key":api_key} if api_key else {}
    async with aiohttp.ClientSession() as s:
        data=await _get(s,"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",params={"vs_currency":"usd","days":2,"interval":"hourly"},headers=headers)
    if not data or "market_caps" not in data: return "stable"
    mc=data["market_caps"]
    if len(mc)<2: return "stable"
    cur,past=mc[-1][1],mc[-24][1] if len(mc)>=24 else mc[0][1]
    delta=(cur-past)/past if past else 0
    return "rising" if delta>0.01 else "falling" if delta<-0.01 else "stable"

async def get_stablecoin_flow(api_key=""):
    headers={"x-cg-demo-api-key":api_key} if api_key else {}
    async with aiohttp.ClientSession() as s:
        data=await _get(s,"https://api.coingecko.com/api/v3/coins/markets",params={"vs_currency":"usd","ids":"tether,usd-coin","price_change_percentage":"24h"},headers=headers)
    if not data or not isinstance(data,list): return "neutral"
    avg=sum(c.get("market_cap_change_percentage_24h",0) for c in data)/max(len(data),1)
    return "inflow" if avg>0.5 else "outflow" if avg<-0.5 else "neutral"

async def get_external_context(coins, coingecko_key=""):
    results=await asyncio.gather(get_fear_and_greed(),get_btc_dominance_trend(coingecko_key),get_stablecoin_flow(coingecko_key),return_exceptions=True)
    return {"fear_greed":results[0] if not isinstance(results[0],Exception) else {"value":50,"classification":"Neutral"},"btc_dom_trend":results[1] if not isinstance(results[1],Exception) else "stable","stablecoin_flow":results[2] if not isinstance(results[2],Exception) else "neutral","funding_rates":{c:0.0 for c in coins}}
