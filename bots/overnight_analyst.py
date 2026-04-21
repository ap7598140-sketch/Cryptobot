import asyncio, logging
from datetime import datetime, timezone
from core.alpaca_client import AlpacaClient, TF_4HR
from core.claude_client import ClaudeClient
from core.market_data import bars_to_df, calc_indicators, get_external_context
from core.regime_cache import save_regime, regime_already_run_today, get_params, save_params
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import ACTIVE_COINS, MAX_CANDLES

logger = logging.getLogger(__name__)

class OvernightAnalyst:
    def __init__(self, claude, alpaca, telegram, coingecko_key=""):
        self.claude=claude; self.alpaca=alpaca; self.telegram=telegram; self.coingecko_key=coingecko_key

    async def run(self):
        logger.info("Overnight Analyst started")
        while not state.stop_command:
            try: await self._maybe_run()
            except Exception as e: logger.error("Overnight Analyst error: %s", e)
            await asyncio.sleep(600)

    async def _maybe_run(self):
        now=datetime.now(timezone.utc)
        if now.hour!=6 or now.minute>10 or regime_already_run_today(): return
        await self._analyze()

    async def _analyze(self):
        logger.info("Running overnight analysis at 6am UTC")
        coins_data=[]
        for coin in ACTIVE_COINS:
            bars4h=await asyncio.to_thread(self.alpaca.get_bars,[coin],TF_4HR,MAX_CANDLES)
            df4h=bars_to_df(bars4h.get(coin,[])); ind4h=calc_indicators(df4h) if not df4h.empty else {}
            coins_data.append({"coin":coin,"price":ind4h.get("current_price",0),"ind_4h":ind4h})
        ctx=await get_external_context(ACTIVE_COINS,self.coingecko_key); params=get_params()
        fg=ctx.get("fear_greed",{})
        prompt=(f"Daily regime. fear_greed={fg.get('value',50)} btc_dom={ctx.get('btc_dom_trend','stable')} stable_flow={ctx.get('stablecoin_flow','neutral')}\n"
                +"\n".join(f"{cd['coin']}: price={cd['price']:.2f} rsi={cd['ind_4h'].get('rsi',50):.1f} macd={cd['ind_4h'].get('macd_signal','?')} struct={cd['ind_4h'].get('market_structure','?')}" for cd in coins_data)
                +'\nReturn: {"regime":"bull/bear/sideways","green_light":"green/yellow/red","fear_greed":50,"macro_events":[],"notes":"brief","rsi_oversold":30,"rsi_overbought":70}')
        result=await self.claude.opus(prompt,bot="overnight_analyst")
        if not result or not isinstance(result,dict):
            save_regime({"regime":"unknown","green_light":"yellow","fear_greed":fg.get("value",50),"macro_events":[],"notes":"Opus failed"}); return
        save_regime({"regime":result.get("regime","unknown"),"green_light":result.get("green_light","yellow"),"fear_greed":result.get("fear_greed",50),"macro_events":result.get("macro_events",[]),"notes":result.get("notes","")})
        new_os=int(result.get("rsi_oversold",params.get("rsi_oversold",30))); new_ob=int(result.get("rsi_overbought",params.get("rsi_overbought",70)))
        if new_os!=params.get("rsi_oversold") or new_ob!=params.get("rsi_overbought"):
            params["rsi_oversold"]=new_os; params["rsi_overbought"]=new_ob; save_params(params)
        await self.telegram.morning_briefing(f"Regime: {result.get('regime','?').upper()} | Signal: {result.get('green_light','?').upper()}\nF&G: {result.get('fear_greed',50)} | BTC dom: {ctx.get('btc_dom_trend','stable')}\n{result.get('notes','')}")
        logger.info("Overnight done: %s / %s",result.get("regime"),result.get("green_light"))
