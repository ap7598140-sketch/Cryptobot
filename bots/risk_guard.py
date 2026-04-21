import asyncio, logging, time
from datetime import datetime, timezone, timedelta
from core.alpaca_client import AlpacaClient
from core.claude_client import ClaudeClient
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import DAILY_LOSS_LIMIT,MAX_DRAWDOWN,MAX_CONSECUTIVE_LOSSES,CONSEC_LOSS_PAUSE_HRS,PORTFOLIO_VALUE,RISK_GUARD_INTERVAL_S,RECONCILE_INTERVAL_S

logger = logging.getLogger(__name__)
_last_reconcile = 0.0

class RiskGuard:
    def __init__(self, claude, alpaca, telegram):
        self.claude=claude; self.alpaca=alpaca; self.telegram=telegram

    async def run(self):
        logger.info("Risk Guard started")
        while not state.stop_command:
            try: await self._check()
            except Exception as e: logger.error("Risk Guard error: %s", e)
            await asyncio.sleep(RISK_GUARD_INTERVAL_S)

    async def _check(self):
        dd=await state.get_drawdown()
        if dd>=MAX_DRAWDOWN: await state.halt(f"drawdown_{dd:.2%}"); await self.telegram.drawdown_circuit_breaker(dd*100); return
        daily_loss=await state.get_daily_loss_pct()
        if daily_loss>=DAILY_LOSS_LIMIT:
            now=datetime.now(timezone.utc); midnight=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            await state.halt_until(midnight.isoformat()); await self.telegram.daily_loss_limit(daily_loss*100); return
        if state.consecutive_losses>=MAX_CONSECUTIVE_LOSSES:
            resume=(datetime.now(timezone.utc)+timedelta(hours=CONSEC_LOSS_PAUSE_HRS)).isoformat()
            await state.halt_until(resume); await self.telegram.consecutive_loss_pause(resume); return
        account=await asyncio.to_thread(self.alpaca.get_account)
        cash=float(account.cash) if account else PORTFOLIO_VALUE
        h=await self.claude.haiku(f"Assess risk. daily_pnl_pct={daily_loss:.2f} drawdown_pct={dd:.2f} open={len(state.open_positions)} consec_losses={state.consecutive_losses} cash_ratio={cash/PORTFOLIO_VALUE:.2f}\nReturn: {{\"halt\":true/false,\"reason\":\"brief\"}}",bot="risk_guard")
        if h and isinstance(h,dict) and h.get("halt"): await state.halt(h.get("reason","ai_risk"))
        global _last_reconcile
        if time.monotonic()-_last_reconcile>=RECONCILE_INTERVAL_S: _last_reconcile=time.monotonic(); await self._reconcile()

    async def _reconcile(self):
        try:
            alpaca_syms={p["symbol"] for p in await asyncio.to_thread(self.alpaca.get_positions) if "/" in p["symbol"]}
            state_syms=set(state.open_positions.keys())
            if alpaca_syms-state_syms or state_syms-alpaca_syms:
                msg=f"missing={alpaca_syms-state_syms} phantom={state_syms-alpaca_syms}"
                await self.telegram.reconciliation_mismatch(msg); await state.halt("reconciliation_mismatch")
        except Exception as e: logger.error("Reconciliation error: %s", e)
