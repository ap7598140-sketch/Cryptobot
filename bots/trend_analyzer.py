"""
Bot 2 — Trend Analyzer
Haiku first pass → Sonnet second pass (only when Haiku scores ≥ 70).
Confirms trades with score ≥ 75 and forwards to executor queue.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from core.alpaca_client import AlpacaClient, TF_1HR, TF_4HR
from core.claude_client import ClaudeClient
from core.market_data import bars_to_df, calc_indicators
from core.regime_cache import get_regime, get_params
from core.state import state
from settings import (
    MIN_HAIKU_SCORE, MIN_SONNET_SCORE, LOGS_DIR, MAX_CANDLES,
)

logger = logging.getLogger(__name__)


def _haiku_prompt(sig: dict) -> str:
    i15 = sig.get("ind_15m", {})
    i1h = sig.get("ind_1h", {})
    dom = sig.get("btc_dom_trend", "stable")
    return (
        f"Multi-timeframe confluence check. Signal: {sig['coin']} {sig['direction'].upper()} "
        f"scanner_score={sig['score']}\n"
        f"15m: rsi={i15.get('rsi',50):.1f} macd={i15.get('macd_signal','?')} "
        f"struct={i15.get('market_structure','?')} div={i15.get('divergence','?')}\n"
        f"1h: rsi={i1h.get('rsi',50):.1f} macd={i1h.get('macd_signal','?')} "
        f"struct={i1h.get('market_structure','?')} div={i1h.get('divergence','?')}\n"
        f"BTC_dom_trend={dom}\n"
        f"Return: {{\"score\":0-100,\"direction\":\"long/short/none\",\"reason\":\"brief\"}}"
    )


def _sonnet_prompt(sig: dict, ind_4h: dict, regime: dict, params: dict) -> str:
    fg = sig.get("fear_greed", {})
    fr = sig.get("funding_rate", 0)
    ar = sig.get("asian_range", {})
    i15 = sig.get("ind_15m", {})
    haiku_dir = sig.get("haiku_direction", sig["direction"])
    return (
        f"Final trade confirmation. {sig['coin']} {haiku_dir.upper()} "
        f"haiku_score={sig.get('haiku_score',0)}\n"
        f"4h: rsi={ind_4h.get('rsi',50):.1f} macd={ind_4h.get('macd_signal','?')} "
        f"struct={ind_4h.get('market_structure','?')} div={ind_4h.get('divergence','?')}\n"
        f"regime={regime.get('regime','unknown')} green_light={regime.get('green_light','yellow')}\n"
        f"fear_greed={fg.get('value',50)}({fg.get('classification','Neutral')})\n"
        f"funding_rate={fr:.6f} btc_dom={sig.get('btc_dom_trend','stable')} "
        f"stablecoin_flow={sig.get('stablecoin_flow','neutral')}\n"
        f"divergence_15m={i15.get('divergence','none')} "
        f"asian_range={ar.get('position','?')}\n"
        f"price={sig['price']:.2f}\n"
        f"Return: {{\"trade\":true,\"coin\":\"{sig['coin']}\",\"direction\":\"{haiku_dir}\","
        f"\"score\":0-100,\"entry\":0.0,\"stop\":0.0,\"target\":0.0,\"reason\":\"brief\"}}"
    )


class TrendAnalyzer:
    def __init__(self, claude: ClaudeClient, alpaca: AlpacaClient,
                 signal_queue: asyncio.Queue, executor_queue: asyncio.Queue):
        self.claude = claude
        self.alpaca = alpaca
        self.signal_queue = signal_queue
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
            except Exception as exc:
                logger.error("Analyzer error: %s", exc)

    async def _analyze(self, sig: dict):
        if await state.is_halted():
            return

        coin = sig["coin"]

        # ── Haiku first pass ──────────────────────────────────────────────────
        h_prompt  = _haiku_prompt(sig)
        h_result  = await self.claude.analyzer_haiku_call(h_prompt)

        if not h_result or not isinstance(h_result, dict):
            logger.warning("Haiku analysis failed for %s", coin)
            return

        h_score = int(h_result.get("score", 0))
        h_dir   = h_result.get("direction", "none")
        logger.info("Haiku analysis %s: score=%d direction=%s", coin, h_score, h_dir)

        _log_decision("haiku", sig, h_result)

        if h_score < MIN_HAIKU_SCORE or h_dir == "none":
            logger.info("Haiku score %d below threshold — skipping %s", h_score, coin)
            return

        # ── Sonnet second pass ────────────────────────────────────────────────
        # Fetch 4hr bars (not available from scanner)
        bars_4h = await asyncio.to_thread(
            self.alpaca.get_bars, [coin], TF_4HR, MAX_CANDLES
        )
        df_4h  = bars_to_df(bars_4h.get(coin, []))
        params = get_params()
        ind_4h = calc_indicators(
            df_4h,
            rsi_oversold=params.get("rsi_oversold", 30),
            rsi_overbought=params.get("rsi_overbought", 70),
        ) if not df_4h.empty else {}

        regime = get_regime()

        sig["haiku_score"]     = h_score
        sig["haiku_direction"] = h_dir

        # Block if regime is red-light
        if regime.get("green_light") == "red":
            logger.info("Regime is RED — blocking %s", coin)
            return

        # Block crowded longs: if funding extremely positive, block long
        fr = sig.get("funding_rate", 0)
        if h_dir == "long" and fr > 0.0005:   # > 0.05 % per 8h = crowded
            logger.info("Funding rate crowded (%s) — blocking long on %s", fr, coin)
            return

        # Reduce confidence for ETH/SOL longs if BTC dominance rising
        if h_dir == "long" and coin != "BTC/USD" and sig.get("btc_dom_trend") == "rising":
            sig["btc_dom_penalty"] = True

        s_prompt = _sonnet_prompt(sig, ind_4h, regime, params)
        s_result = await self.claude.analyzer_sonnet_call(s_prompt)

        if not s_result or not isinstance(s_result, dict):
            logger.warning("Sonnet analysis failed for %s", coin)
            return

        s_score = int(s_result.get("score", 0))
        trade   = bool(s_result.get("trade", False))
        _log_decision("sonnet", sig, s_result)

        logger.info("Sonnet analysis %s: score=%d trade=%s", coin, s_score, trade)

        if not trade or s_score < MIN_SONNET_SCORE:
            logger.info("Sonnet score %d below threshold — skipping %s", s_score, coin)
            return

        # Check entry/stop/target sanity
        entry  = float(s_result.get("entry", sig["price"]))
        stop   = float(s_result.get("stop", 0))
        target = float(s_result.get("target", 0))

        if stop <= 0 or target <= 0:
            logger.warning("Invalid stop/target from Sonnet for %s", coin)
            return

        # Forward confirmed signal
        confirmed = {
            **sig,
            "entry":        entry,
            "stop":         stop,
            "target":       target,
            "final_score":  s_score,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.executor_queue.put(confirmed)
        logger.info("Confirmed trade queued: %s %s @ %.2f", coin, h_dir, entry)


def _log_decision(stage: str, sig: dict, result: dict):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"analysis_{stage}_{sig.get('coin','?').replace('/','_')}_{ts}.json")
    try:
        with open(path, "w") as f:
            json.dump({"input": sig, "result": result}, f, indent=2, default=str)
    except Exception as e:
        logger.error("Failed to write analysis log: %s", e)
