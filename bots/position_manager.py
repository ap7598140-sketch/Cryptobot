import asyncio, logging
from datetime import datetime, timezone
from core.alpaca_client import AlpacaClient
from core.claude_client import ClaudeClient
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import PARTIAL_EXIT_PCT,TRAILING_TIER1_PROFIT,TRAILING_TIER1_TRAIL,TRAILING_TIER2_PROFIT,TRAILING_TIER2_TRAIL,POSITION_TIME_LIMIT_HRS,POSITION_MGR_INTERVAL_S

logger = logging.getLogger(__name__)

class PositionManager:
    def __init__(self, claude, alpaca, telegram):
        self.claude=claude; self.alpaca=alpaca; self.telegram=telegram

    async def run(self):
        logger.info("Position Manager started")
        while not state.stop_command:
            try:
                if not await state.is_halted():
                    for coin,pos in list(state.open_positions.items()):
                        try: await self._manage_one(coin,pos)
                        except Exception as e: logger.error("Error managing %s: %s",coin,e)
            except Exception as e: logger.error("Position Manager error: %s", e)
            await asyncio.sleep(POSITION_MGR_INTERVAL_S)

    async def _manage_one(self, coin, pos):
        current=await asyncio.to_thread(self.alpaca.get_latest_price,coin)
        if not current: return
        current=float(current)
        if pos.direction=="long" and current>pos.peak_price: pos.peak_price=current; await state.update_position(coin,pos)
        elif pos.direction=="short" and current<pos.peak_price: pos.peak_price=current; await state.update_position(coin,pos)
        pnl_pct=(current-pos.entry_price)/pos.entry_price*(1 if pos.direction=="long" else -1)
        if (pos.direction=="long" and current<=pos.stop_price) or (pos.direction=="short" and current>=pos.stop_price):
            await self._close(coin,pos,current,"hard_stop"); return
        elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(pos.opened_at)).total_seconds()/3600
        if elapsed>=POSITION_TIME_LIMIT_HRS: await self._close(coin,pos,current,"time_limit"); return
        if pos.strategy=="grid":
            if (pos.direction=="long" and current>=pos.target_price) or (pos.direction=="short" and current<=pos.target_price):
                await self._close(coin,pos,current,"grid_take_profit"); return
        else:
            peak_pnl=abs(pos.peak_price-pos.entry_price)/pos.entry_price
            for tier_profit,tier_trail in [(TRAILING_TIER2_PROFIT,TRAILING_TIER2_TRAIL),(TRAILING_TIER1_PROFIT,TRAILING_TIER1_TRAIL)]:
                if peak_pnl>=tier_profit:
                    trail=(pos.peak_price*(1-tier_trail) if pos.direction=="long" else pos.peak_price*(1+tier_trail))
                    if (pos.direction=="long" and current<=trail) or (pos.direction=="short" and current>=trail):
                        await self._close(coin,pos,current,f"trailing_{tier_profit}"); await self.telegram.trailing_stop_triggered(coin,pnl_pct*100); return
                    break
            if pnl_pct>=PARTIAL_EXIT_PCT and not pos.partial_taken:
                pqty=round(pos.qty*0.5,4)
                await asyncio.to_thread(self.alpaca.place_market_order,coin,pqty,"sell" if pos.direction=="long" else "buy")
                pos.partial_taken=True; pos.qty-=pqty; await state.update_position(coin,pos)
                await self.telegram.partial_exit(coin,pqty,pos.qty,current,pnl_pct*100); return
        from core.regime_cache import get_regime
        h=await self.claude.haiku(f"Manage {pos.coin} {pos.direction} [{pos.strategy}] entry={pos.entry_price:.2f} current={current:.2f} pnl={pnl_pct*100:.2f}% regime={get_regime().get('green_light','yellow')}\nReturn: {{\"action\":\"hold/exit/adjust_stop\",\"new_stop\":0.0,\"reason\":\"brief\"}}",bot="position_mgr")
        if not h or not isinstance(h,dict): return
        if h.get("action")=="exit": await self._close(coin,pos,current,f"ai_exit")
        elif h.get("action")=="adjust_stop" and float(h.get("new_stop",0))>0:
            pos.stop_price=float(h["new_stop"]); await state.update_position(coin,pos)

    async def _close(self, coin, pos, current, reason):
        await asyncio.to_thread(self.alpaca.close_position,coin)
        pnl_pct=(current-pos.entry_price)/pos.entry_price*100*(1 if pos.direction=="long" else -1)
        pnl_usd=pnl_pct/100*pos.usd_value; win=pnl_pct>0
        await state.remove_position(coin,win=win,pnl_pct=pnl_pct,pnl_usd=pnl_usd); await state.record_daily_pnl(pnl_usd)
        if "stop" in reason: await self.telegram.stop_loss_fired(coin,abs(pnl_pct),abs(pnl_usd))
        elif "profit" in reason or "take_profit" in reason: await self.telegram.take_profit(coin,pnl_pct,pnl_usd)
        logger.info("Closed %s: %s pnl=%.2f%%",coin,reason,pnl_pct)
