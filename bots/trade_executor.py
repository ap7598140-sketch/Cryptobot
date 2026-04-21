import asyncio, logging
from datetime import datetime, timezone
from core.alpaca_client import AlpacaClient
from core.claude_client import ClaudeClient
from core.regime_cache import get_params
from core.state import state, Position
from core.telegram_notifier import TelegramNotifier
from settings import PORTFOLIO_VALUE,CASH_RESERVE_PCT,MAX_POSITIONS,MAX_RISK_PER_TRADE,MIN_REWARD_RISK,ENTRY_FIRST_FRAC,ENTRY_CONFIRM_FRAC,ROUND_NUM_OFFSET_PCT,DRAWDOWN_SIZE_THRESHOLD,DRAWDOWN_SIZE_REDUCTION,DECAY_LOOKBACK,DECAY_WIN_RATE_FLOOR,DECAY_SIZE_REDUCTION

logger = logging.getLogger(__name__)

class TradeExecutor:
    def __init__(self, claude, alpaca, executor_queue, telegram):
        self.claude=claude; self.alpaca=alpaca; self.executor_queue=executor_queue; self.telegram=telegram

    async def run(self):
        logger.info("Trade Executor started")
        while not state.stop_command:
            try:
                sig=await asyncio.wait_for(self.executor_queue.get(),timeout=5.0)
                await self._execute(sig); self.executor_queue.task_done()
            except asyncio.TimeoutError: continue
            except Exception as e: logger.error("Executor error: %s", e)

    async def _execute(self, sig):
        if await state.is_halted(): return
        coin=sig["coin"]
        if len(state.open_positions)>=MAX_POSITIONS: return
        if coin in state.open_positions: return
        entry,stop,target=sig["entry"],sig["stop"],sig["target"]
        rr=abs(target-entry)/abs(entry-stop) if abs(entry-stop)>0 else 0
        if rr<MIN_REWARD_RISK: return
        account=await asyncio.to_thread(self.alpaca.get_account)
        cash=float(account.cash) if account else PORTFOLIO_VALUE*(1-CASH_RESERVE_PCT)
        deployable=cash-PORTFOLIO_VALUE*CASH_RESERVE_PCT
        if deployable<=0: return
        params=get_params()
        s_result=await self.claude.sonnet(f"Size trade for {coin} {sig['direction'].upper()} strategy={sig.get('strategy','momentum')} score={sig.get('final_score',0)}\nentry={entry:.2f} stop={stop:.2f} target={target:.2f}\naccount_cash={deployable:.2f} max_risk_pct={MAX_RISK_PER_TRADE}\nReturn: {{\"qty\":0.0,\"usd\":0.0,\"rr_ratio\":0.0,\"approved\":true/false,\"reason\":\"brief\"}}",bot="executor_sonnet")
        if not s_result or not isinstance(s_result,dict):
            if coin in state.open_positions: await asyncio.to_thread(self.alpaca.close_position,coin); await state.remove_position(coin)
            return
        if not s_result.get("approved",False): return
        qty=float(s_result.get("qty",0)); usd=float(s_result.get("usd",0))
        if qty<=0 or usd<=0: return
        dd=await state.get_drawdown()
        if dd>=DRAWDOWN_SIZE_THRESHOLD: qty=round(qty*(1-DRAWDOWN_SIZE_REDUCTION),4); usd=round(usd*(1-DRAWDOWN_SIZE_REDUCTION),2)
        recent=state.recent_trades[-DECAY_LOOKBACK:] if state.recent_trades else []
        if len(recent)>=DECAY_LOOKBACK and sum(1 for t in recent if t.get("win"))/len(recent)<DECAY_WIN_RATE_FLOOR:
            qty=round(qty*(1-DECAY_SIZE_REDUCTION),4); usd=round(usd*(1-DECAY_SIZE_REDUCTION),2)
        order=await asyncio.to_thread(self.alpaca.place_order,coin,round(qty*ENTRY_FIRST_FRAC,4),sig["direction"])
        if not order: return
        await asyncio.sleep(2)
        offset=entry*ROUND_NUM_OFFSET_PCT
        await asyncio.to_thread(self.alpaca.place_limit_order,coin,round(qty*ENTRY_CONFIRM_FRAC,4),sig["direction"],round(entry-offset if sig["direction"]=="long" else entry+offset,2))
        await state.add_position(coin,Position(coin=coin,direction=sig["direction"],strategy=sig.get("strategy","momentum"),entry_price=entry,stop_price=stop,target_price=target,qty=qty,usd_value=usd,score=sig.get("final_score",0),opened_at=datetime.now(timezone.utc).isoformat(),peak_price=entry))
        await self.telegram.trade_opened(coin,sig["direction"],sig.get("strategy","momentum"),entry,qty,sig.get("final_score",0),usd)
        logger.info("Executed: %s %s %.4f @ %.2f",coin,sig["direction"],qty,entry)
