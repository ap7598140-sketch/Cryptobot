"""
Market regime cache -- written at 6am UTC by Overnight Analyst,
read by all bots throughout the day. Also holds A/B test params.
"""
import json
import logging
import os
from datetime import datetime, timezone

from settings import REGIME_CACHE_FILE, PARAMS_FILE

logger = logging.getLogger(__name__)

DEFAULT_PARAMS = {
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "macd_signal_period": 9,
    "ab_variant": "A",
    "variant_A_wins": 0, "variant_A_trades": 0,
    "variant_B_wins": 0, "variant_B_trades": 0,
}


def _load(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to load %s: %s", path, e)
    return {}


def _save(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.error("Failed to save %s: %s", path, e)


def get_regime() -> dict:
    data  = _load(REGIME_CACHE_FILE)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") == today:
        return data
    return {
        "regime": "unknown", "date": today, "generated_at": None,
        "fear_greed": 50, "macro_events": [],
        "green_light": "yellow",
        "notes": "Regime not classified yet today",
    }


def save_regime(data: dict):
    data["date"]         = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    _save(REGIME_CACHE_FILE, data)
    logger.info("Regime cached: %s / %s", data.get("regime"), data.get("green_light"))


def regime_already_run_today() -> bool:
    return _load(REGIME_CACHE_FILE).get("date") == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_params() -> dict:
    return {**DEFAULT_PARAMS, **_load(PARAMS_FILE)}


def save_params(params: dict):
    _save(PARAMS_FILE, params)


def record_trade_result(win: bool):
    p = get_params()
    v = p.get("ab_variant", "A")
    p[f"variant_{v}_trades"] = p.get(f"variant_{v}_trades", 0) + 1
    if win:
        p[f"variant_{v}_wins"] = p.get(f"variant_{v}_wins", 0) + 1
    save_params(p)


def get_better_variant() -> str:
    p = get_params()
    at, bt = p.get("variant_A_trades", 0), p.get("variant_B_trades", 0)
    aw, bw = p.get("variant_A_wins",  0), p.get("variant_B_wins",  0)
    if at < 5: return "B" if bt >= 5 else "A"
    if bt < 5: return "A"
    return "A" if (aw / at) >= (bw / bt) else "B"
