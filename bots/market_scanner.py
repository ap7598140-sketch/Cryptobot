"""
Bot 1 — Market Scanner (Haiku)
Runs every 60 s, batches all active coins into ONE Claude call.
Only forwards signals that score ≥ 70 to the Trend Analyzer queue.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from core.alpaca_client import AlpacaClient, TF_15MIN, TF_1HR
from core.claude_client import ClaudeClient
from core.market_data import (
    bars_to_df, calc_indicators, calc_asian_session_range,
    get_external_market_context,
)
from core.state import state
from settings import (
    ACTIVE_COINS, SCANNER_INTERVAL_S, MIN_SCANNER_SCORE,
    MAX_CANDLES, LOGS_DIR,
)

logger = logging.getLogger(__name__)


def _build_scanner_prompt(coins_data: list[dict], context: dict) -> str:
    """
    Builds a compact batched prompt — all coins in one call.
    Keeps tokens minimal: no verbose descriptions, just structured data.
    """
    fg = context.get("fear_greed", {})
    header = (
        f"Scan crypto signals. Fear&Greed:{fg.get('value',50)} "
        f"BTC_dom:{context.get('btc_dom_trend','stable')} "
        f"StablecoinFlow:{context.get('stablecoin_flow','neutral')}\n"
    )
    coin_lines = []
    for cd in coins_data:
        coin = cd["coin"]
        fr = context["funding_rates"].get(coin, 0)
        i15 = cd.get("ind_15m", {})
        i1h = cd.get("ind_1h", {})
        ar  = cd.get("asian_range", {})
        line = (
            f"{coin}: price={cd.get('price', 0):.2f} "
            f"15m[rsi={i15.get('rsi',50):.1f} macd={i15.get('macd_signal','?')} "
            f"atr_pct={i15.get('atr_pct',0):.2f} cvd={i15.get('cvd_signal','?')}] "
            f"1h[rsi={i1h.get('rsi',50):.1f} macd={i1h.get('macd_signal','?')} "
            f"cvd={i1h.get('cvd_signal','?')}] "
            f"asian[hi={ar.get('asian_high','?')} lo={ar.get('asian_low','?')} pos={ar.get('position','?')}] "
            f"fund_rate={fr:.6f}"
        )
        coin_lines.append(line)

    tail = (
        '\nReturn JSON array (one object per coin):\n'
        '[{"coin":"BTC/USD","signal":true,"score":0,"direction":"long/short/none","reason":"brief"}]'
    )
    return header + "\n".join(coin_lines) + tail


class MarketScanner:
    def __init__(self, claude: ClaudeClient, alpaca: AlpacaClient,
                 signal_queue: asyncio.Queue,
                 coinglass_key: str = "", coingecko_key: str = ""):
        self.claude = claude
        self.alpaca = alpaca
        self.signal_queue = signal_queue
        self.coinglass_key = coinglass_key
        self.coingecko_key = coingecko_key

    async def run(self):
        logger.info("Market Scanner started — interval %ds", SCANNER_INTERVAL_S)
        while not state.stop_command:
            try:
                await self._scan_cycle()
            except Exception as exc:
                logger.error("Scanner cycle error: %s", exc)
            await asyncio.sleep(SCANNER_INTERVAL_S)

    async def _scan_cycle(self):
        if await state.is_halted():
            return

        # ── 1. Fetch all coin bars in parallel ───────────────────────────────
        bars_15m, bars_1h = await asyncio.gather(
            asyncio.to_thread(self.alpaca.get_bars, ACTIVE_COINS, TF_15MIN, MAX_CANDLES),
            asyncio.to_thread(self.alpaca.get_bars, ACTIVE_COINS, TF_1HR, MAX_CANDLES),
        )

        # ── 2. Get external context (funding, F&G, BTC dom, stable flow) ─────
        ext = await get_external_market_context(
            ACTIVE_COINS, self.coinglass_key, self.coingecko_key
        )

        # ── 3. Build per-coin indicator dictionaries ──────────────────────────
        coins_data = []
        for coin in ACTIVE_COINS:
            bars15 = bars_15m.get(coin, [])
            bars1h = bars_1h.get(coin, [])
            df15   = bars_to_df(bars15)
            df1h   = bars_to_df(bars1h)

            ind_15m = calc_indicators(df15) if not df15.empty else {}
            ind_1h  = calc_indicators(df1h)  if not df1h.empty else {}
            asian   = calc_asian_session_range(bars1h)

            price = ind_15m.get("current_price") or ind_1h.get("current_price", 0)
            coins_data.append({
                "coin": coin,
                "price": price,
                "ind_15m": ind_15m,
                "ind_1h":  ind_1h,
                "asian_range": asian,
                "bars_15m": bars15,   # kept for logging only
            })

        # ── 4. Single batched Claude call ─────────────────────────────────────
        prompt   = _build_scanner_prompt(coins_data, ext)
        response = await self.claude.scanner_call(prompt)

        if response is None:
            logger.warning("Scanner: no response from Claude")
            return

        signals = response if isinstance(response, list) else [response]

        # ── 5. Filter & forward ───────────────────────────────────────────────
        forwarded = 0
        for sig in signals:
            if not isinstance(sig, dict):
                continue
            coin    = sig.get("coin", "")
            score   = int(sig.get("score", 0))
            signal  = bool(sig.get("signal", False))
            direction = sig.get("direction", "none")

            if not signal or score < MIN_SCANNER_SCORE or direction == "none":
                continue

            payload = {
                "coin":      coin,
                "score":     score,
                "direction": direction,
                "reason":    sig.get("reason", ""),
                "price":     next((c["price"] for c in coins_data if c["coin"] == coin), 0),
                "ind_15m":   next((c["ind_15m"] for c in coins_data if c["coin"] == coin), {}),
                "ind_1h":    next((c["ind_1h"] for c in coins_data if c["coin"] == coin), {}),
                "asian_range": next((c["asian_range"] for c in coins_data if c["coin"] == coin), {}),
                "funding_rate": ext["funding_rates"].get(coin, 0),
                "fear_greed":   ext["fear_greed"],
                "btc_dom_trend": ext["btc_dom_trend"],
                "stablecoin_flow": ext["stablecoin_flow"],
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            }
            await self.signal_queue.put(payload)
            forwarded += 1
            logger.info("Scanner signal: %s %s score=%d", coin, direction, score)

        # ── 6. Log scan ───────────────────────────────────────────────────────
        _log_scan(signals, ext)

    # end _scan_cycle


def _log_scan(signals: list, ext: dict):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"scan_{ts}.json")
    try:
        with open(path, "w") as f:
            json.dump({"signals": signals, "context": {
                "fear_greed": ext.get("fear_greed"),
                "btc_dom_trend": ext.get("btc_dom_trend"),
                "stablecoin_flow": ext.get("stablecoin_flow"),
            }}, f, indent=2, default=str)
    except Exception as e:
        logger.error("Failed to write scan log: %s", e)
