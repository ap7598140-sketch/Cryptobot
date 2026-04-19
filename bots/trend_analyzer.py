"""
Bot 2 -- Trend Analyzer
Haiku first pass (60+ -> Sonnet second pass -> 65+ -> executor).
Handles both momentum (trending) and grid (ranging) strategies.
Error in AI response -> close any open position immediately.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from core.alpaca_client import AlpacaClient, TF_4HR
from core.claude_client import ClaudeClient
from core.market_data import bars_to_df, calc_indicators
from core.regime_cache import get_regime, get_params
from core.state import state
from settings import MIN_HAIKU_SCORE, MIN_SONNET_SCORE, LOGS_DIR, MAX_CANDLES

logger = logging.getLogger(__name__)


def _haiku_prompt(sig: dict) -> str:
    i15 = sig.get("ind_15m", {})
    i1h = sig.get("ind_1h", {})
    gr  = sig.get("grid", {})
    return (
        f"Multi-timeframe confluence. {sig['coin']} {sig['direction'].upper()} "
        f"scanner_score={sig['score']} market_type={sig.get('market_type','trending')}\n"
        f"15m: rsi={i15.get('rsi',50):.1f} macd={i15.get('macd_signal','?')} "
        f"struct={i15.get('market_structure','?')} div={i15.get('divergence','?')}\n"
        f"1h: rsi={i1h.get('rsi',50):.1f} macd={i1h.get('macd_signal','?')} "
        f"struct={i1h.get('market_structure','?')}\n"
        f"grid: support={gr.get('support','?')} resistance={gr.get('resistance','?')}\n"
        f"BTC_dom={sig.get('btc_dom_trend','stable')}\n"
        f"Return: {{\"score\":0-100,\"direction\":\"long/short/none\",\"reason\":\"brief\"}}"
    )


def _sonnet_prompt(sig: dict, ind_4h: dict, regime: dict) -> str:
    fg  = sig.get("fear_greed", {})
    i15 = sig.get("ind_15m", {})
    ar  = sig.get("asian", {})
    gr  = sig.get("grid", {})
    mt  = sig.get("market_type", "trending")
    hd  = sig.get("haiku_direction", sig["direction"])
    p   = sig["price"]

    if mt == "ranging":
        strategy_hint = (
            f"Grid strategy: buy near support={gr.get('support','?')}, "
            f"sell near resistance={gr.get('resistance','?')}. "
            f"Entry near support for long, near resistance for short."
        )
    else:
        strategy_hint = "Momentum strategy: buy breakouts, trail stops as trend extends."

    return (
        f"Final trade analysis. {sig['coin']} {hd.upper()} "
        f"haiku_score={sig.get('haiku_score',0)} market_type={mt}\n"
        f"4h: rsi={ind_4h.get('rsi',50):.1f} macd={ind_4h.get('macd_signal','?')} "
        f"struct={ind_4h.get('market_structure','?')}\n"
        f"regime={regime.get('regime','unknown')} light={regime.get('green_light','yellow')}\n"
        f"fear_greed={fg.get('value',50)} btc_dom={sig.get('btc_dom_trend','stable')} "
        f"stable_flow={sig.get('stablecoin_flow','neutral')}\n"
        f"divergence_15m={i15.get('divergence','none')} asian={ar.get('position','?')}\n"
        f"price={p:.2f}\n"
        f"{strategy_hint}\n"
        f"Rules: min_score=65 set entry/stop/target based on strategy.\n"
        f"Return: {{\"trade\":true/false,\"coin\":\"{sig['coin']}\","
        f"\"direction\":\"{hd}\",\"score\":0-100,"
        f"\"strategy\":\"momentum/grid\","
        f"\"entry\":{p:.2f},\"stop\":0.0,\"target\":0.0,\"reason\":\"brief\"}}"
    )


class TrendAnalyzer:
    def __init__(self, claude: ClaudeClient, alpaca: AlpacaClient,
                 signal_queue: asyncio.Queue, executor_queue: asyncio.Queue):
        self.claude         = claude
        self.alpaca         = alpaca
        self.signal_queue   = signal_queue
        self.executor_queue = executor_queue

    async def run(self):
        logger.info("Trend Analyzer started")
        while not state.stop_command:
            try:
                sig = await asyncio.wait_for(self.signal_queue.get(), timeout=5.0)
                await self._analyze(sig)
                self.signal_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Analyzer error: %s", e)

    async def _analyze(self, sig: dict):
        if await state.is_halted():
            return

        coin = sig["coin"]

        h_result = await self.claude.haiku(_haiku_prompt(sig), bot="analyzer_haiku")
        if not h_result or not isinstance(h_result, dict):
            logger.warning("Haiku failed for %s -- closing any open position", coin)
            await self._emergency_close(coin, "haiku_error")
            return

        h_score = int(h_result.get("score", 0))
        h_dir   = h_result.get("direction", "none")
        _log("haiku", sig, h_result)
        logger.info("Haiku %s: score=%d dir=%s", coin, h_score, h_dir)

        if h_score < MIN_HAIKU_SCORE or h_dir == "none":
            return

        bars_4h = await asyncio.to_thread(self.alpaca.get_bars, [coin], TF_4HR, MAX_CANDLES)
        df_4h   = bars_to_df(bars_4h.get(coin, []))
        params  = get_params()
        ind_4h  = calc_indicators(df_4h,
                                  rsi_oversold=params.get("rsi_oversold", 30),
                                  rsi_overbought=params.get("rsi_overbought", 70)
                                  ) if not df_4h.empty else {}
        regime = get_regime()

        if regime.get("green_light") == "red":
            logger.info("Regime RED -- blocking %s", coin)
            return

        sig["haiku_score"]     = h_score
        sig["haiku_direction"] = h_dir

        s_result = await self.claude.sonnet(
            _sonnet_prompt(sig, ind_4h, regime), bot="analyzer_sonnet"
        )
        if not s_result or not isinstance(s_result, dict):
            logger.warning("Sonnet failed for %s -- closing any open position", coin)
            await self._emergency_close(coin, "sonnet_error")
            return

        s_score = int(s_result.get("score", 0))
        trade   = bool(s_result.get("trade", False))
        _log("sonnet", sig, s_result)
        logger.info("Sonnet %s: score=%d trade=%s", coin, s_score, trade)

        if not trade or s_score < MIN_SONNET_SCORE:
            return

        entry  = float(s_result.get("entry", sig["price"]))
        stop   = float(s_result.get("stop", 0))
        target = float(s_result.get("target", 0))
        if stop <= 0 or target <= 0:
            logger.warning("Invalid stop/target from Sonnet for %s", coin)
            return

        await self.executor_queue.put({
            **sig,
            "entry":        entry,
            "stop":         stop,
            "target":       target,
            "final_score":  s_score,
            "strategy":     s_result.get("strategy", sig.get("market_type", "momentum")),
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Confirmed: %s %s @ %.2f [%s]",
                    coin, h_dir, entry, s_result.get("strategy"))

    async def _emergency_close(self, coin: str, reason: str):
        if coin in state.open_positions:
            await asyncio.to_thread(self.alpaca.close_position, coin)
            await state.remove_position(coin)
            logger.warning("Emergency close %s: %s", coin, reason)


def _log(stage: str, sig: dict, result: dict):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"analysis_{stage}_{sig.get('coin','').replace('/','_')}_{ts}.json")
    try:
        with open(path, "w") as f:
            json.dump({"input": sig, "result": result}, f, indent=2, default=str)
    except Exception as e:
        logger.error("Analysis log error: %s", e)
