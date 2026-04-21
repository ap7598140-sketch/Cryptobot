import asyncio, csv, logging, os
from core.claude_client import ClaudeClient
from core.regime_cache import get_params, save_params, get_better_variant
from core.state import state
from core.telegram_notifier import TelegramNotifier
from settings import TRADES_CSV, PERF_TRACKER_INTERVAL_S, DECAY_LOOKBACK, DECAY_WIN_RATE_FLOOR

logger = logging.getLogger(__name__)
HEADERS=["timestamp","coin","direction","strategy","entry","exit","qty","usd_value","pnl_pct","pnl_usd","win","score","ab_variant"]

def _ensure_csv():
    os.makedirs(os.path.dirname(TRADES_CSV), exist_ok=True)
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV,"w",newline="") as f: csv.DictWriter(f,fieldnames=HEADERS).writeheader()

def append_trade(trade):
    _ensure_csv()
    with open(TRADES_CSV,"a",newline="") as f: csv.DictWriter(f,fieldnames=HEADERS).writerow({h:trade.get(h,"") for h in HEADERS})

def _load_recent(n):
    if not os.path.exists(TRADES_CSV): return []
    with open(TRADES_CSV,newline="") as f: rows=list(csv.DictReader(f))
    return rows[-n:]

class PerformanceTracker:
    def __init__(self, claude, telegram): self.claude=claude; self.telegram=telegram; _ensure_csv()

    async def run(self):
        logger.info("Performance Tracker started")
        while not state.stop_command:
            try: await self._track()
            except Exception as e: logger.error("Performance Tracker error: %s", e)
            await asyncio.sleep(PERF_TRACKER_INTERVAL_S)

    async def _track(self):
        recent=_load_recent(DECAY_LOOKBACK)
        if not recent: return
        params=get_params(); total=len(recent); wins=sum(1 for r in recent if r.get("win")=="True")
        win_rate=wins/total*100 if total else 0; avg_pnl=sum(float(r.get("pnl_pct",0)) for r in recent)/max(total,1)
        s=await self.claude.sonnet(f"Performance: {total} trades win_rate={win_rate:.1f}% avg_pnl={avg_pnl:.2f}% params={params}\nReturn: {{\"decay\":true/false,\"action\":\"brief\",\"rsi_oversold\":30,\"rsi_overbought\":70,\"switch_variant\":true/false}}",bot="perf_tracker")
        if not s or not isinstance(s,dict): return
        if s.get("decay"): await self.telegram.strategy_decay_warning(win_rate, s.get("action",""))
        if s.get("switch_variant"): params["ab_variant"]=get_better_variant()
        params["rsi_oversold"]=int(s.get("rsi_oversold",params.get("rsi_oversold",30)))
        params["rsi_overbought"]=int(s.get("rsi_overbought",params.get("rsi_overbought",70)))
        save_params(params)
