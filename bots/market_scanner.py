import asyncio, json, logging, os
from datetime import datetime, timezone
from core.alpaca_client import AlpacaClient, TF_15MIN, TF_1HR
from core.claude_client import ClaudeClient
from core.market_data import bars_to_df, calc_indicators, calc_asian_session_range, detect_grid_range, get_external_context
from core.state import state
from settings import ACTIVE_COINS, SCANNER_INTERVAL_S, MIN_SCANNER_SCORE, MAX_CANDLES, LOGS_DIR

logger = logging.getLogger(__name__)

def _build_prompt(coins_data, ctx):
    fg = ctx.get("fear_greed",{})
    lines = [f"Scan crypto signals. Fear&Greed:{fg.get('value',50)} BTC_dom:{ctx.get('btc_dom_trend','stable')} StableFlow:{ctx.get('stablecoin_flow','neutral')}"]
    for cd in coins_data:
        i15=cd.get("ind_15m",{}); i1h=cd.get("ind_1h",{}); ar=cd.get("asian",{}); gr=cd.get("grid",{})
        lines.append(f"{cd['coin']}: price={cd['price']:.2f} 15m[rsi={i15.get('rsi',50):.1f} macd={i15.get('macd_signal','?')} atr%={i15.get('atr_pct',0):.2f} cvd={i15.get('cvd_signal','?')} struct={i15.get('market_structure','?')}] 1h[rsi={i1h.get('rsi',50):.1f} macd={i1h.get('macd_signal','?')} struct={i1h.get('market_structure','?')}] asian_pos={ar.get('position','?')} market_type={gr.get('market_type','trending')} support={gr.get('support','?')} resistance={gr.get('resistance','?')}")
    lines.append('[{"coin":"BTC/USD","signal":true,"score":0-100,"direction":"long/short/none","market_type":"trending/ranging","reason":"brief"}]')
    return "\n".join(lines)

class MarketScanner:
    def __init__(self, claude, alpaca, signal_queue, coingecko_key=""):
        self.claude=claude; self.alpaca=alpaca; self.signal_queue=signal_queue; self.coingecko_key=coingecko_key

    async def run(self):
        logger.info("Market Scanner started -- interval %ds", SCANNER_INTERVAL_S)
        while not state.stop_command:
            try: await self._scan()
            except Exception as e: logger.error("Scanner error: %s", e)
            await asyncio.sleep(SCANNER_INTERVAL_S)

    async def _scan(self):
        if await state.is_halted(): return
        coins_data = []
        for coin in ACTIVE_COINS:
            bars15=await asyncio.to_thread(self.alpaca.get_bars,[coin],TF_15MIN,MAX_CANDLES)
            bars1h=await asyncio.to_thread(self.alpaca.get_bars,[coin],TF_1HR,MAX_CANDLES)
            df15=bars_to_df(bars15.get(coin,[])); df1h=bars_to_df(bars1h.get(coin,[]))
            i15=calc_indicators(df15) if not df15.empty else {}
            i1h=calc_indicators(df1h) if not df1h.empty else {}
            ar=calc_asian_session_range(bars1h.get(coin,[]))
            gr=detect_grid_range(df15) if not df15.empty else {"market_type":"trending"}
            price=i15.get("current_price") or i1h.get("current_price",0)
            coins_data.append({"coin":coin,"price":price,"ind_15m":i15,"ind_1h":i1h,"asian":ar,"grid":gr,"bars_1h":bars1h.get(coin,[])})
        ctx=await get_external_context(ACTIVE_COINS,self.coingecko_key)
        response=await self.claude.haiku(_build_prompt(coins_data,ctx),bot="scanner")
        if response is None: logger.warning("Scanner: no Claude response"); return
        for sig in (response if isinstance(response,list) else [response]):
            if not isinstance(sig,dict): continue
            coin=sig.get("coin",""); score=int(sig.get("score",0)); direction=sig.get("direction","none")
            if not sig.get("signal") or score<MIN_SCANNER_SCORE or direction=="none": continue
            cd=next((c for c in coins_data if c["coin"]==coin),{})
            await self.signal_queue.put({"coin":coin,"score":score,"direction":direction,"market_type":sig.get("market_type","trending"),"reason":sig.get("reason",""),"price":cd.get("price",0),"ind_15m":cd.get("ind_15m",{}),"ind_1h":cd.get("ind_1h",{}),"asian":cd.get("asian",{}),"grid":cd.get("grid",{}),"bars_1h":cd.get("bars_1h",[]),"fear_greed":ctx["fear_greed"],"btc_dom_trend":ctx["btc_dom_trend"],"stablecoin_flow":ctx["stablecoin_flow"],"funding_rate":0.0,"scanned_at":datetime.now(timezone.utc).isoformat()})
            logger.info("Scanner signal: %s %s score=%d", coin, direction, score)
        _log(response if isinstance(response,list) else [response], ctx)

def _log(signals, ctx):
    os.makedirs(LOGS_DIR, exist_ok=True)
    try:
        with open(os.path.join(LOGS_DIR, f"scan_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"),"w") as f:
            json.dump({"signals":signals,"fg":ctx.get("fear_greed"),"btc_dom":ctx.get("btc_dom_trend")},f,indent=2,default=str)
    except Exception as e: logger.error("scan log error: %s", e)
