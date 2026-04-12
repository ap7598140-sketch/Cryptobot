"""
Market regime cache — written once at 6 am UTC by the Overnight Analyst,
read by every bot throughout the day.  Also stores A/B parameter variants.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from settings import REGIME_CACHE_FILE, PARAMS_FILE

logger = logging.getLogger(__name__)

# Default trading parameters (A/B tested by overnight analyst)
DEFAULT_PARAMS = {
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "macd_signal_period": 9,
    "ab_variant": "A",          # which parameter set is currently active
    "variant_A_wins": 0,
    "variant_A_trades": 0,
    "variant_B_wins": 0,
    "variant_B_trades": 0,
}


def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load {path}: {e}")
    return {}


def _save_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to save {path}: {e}")


# ── Regime ────────────────────────────────────────────────────────────────────

def get_regime() -> dict:
    """
    Returns today's regime dict, or a default if not yet generated.
    Schema: {
        "regime": "bull"|"bear"|"sideways",
        "date": "YYYY-MM-DD",
        "generated_at": ISO-string,
        "fear_greed": int,
        "macro_events": [...],
        "green_light": "green"|"yellow"|"red",
        "notes": "...",
        "last_regime_performance": {...},   # how bot did last time in this regime
    }
    """
    data = _load_json(REGIME_CACHE_FILE)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") == today:
        return data
    # Cache is stale or missing — return safe default
    return {
        "regime": "unknown",
        "date": today,
        "generated_at": None,
        "fear_greed": 50,
        "macro_events": [],
        "green_light": "yellow",
        "notes": "Regime not yet classified today",
        "last_regime_performance": {},
    }


def save_regime(regime_data: dict):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    regime_data["date"] = today
    regime_data["generated_at"] = datetime.now(timezone.utc).isoformat()
    _save_json(REGIME_CACHE_FILE, regime_data)
    logger.info(f"Regime cached: {regime_data.get('regime')} / {regime_data.get('green_light')}")


def regime_already_run_today() -> bool:
    data = _load_json(REGIME_CACHE_FILE)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return data.get("date") == today


# ── Trading parameters (A/B tested) ──────────────────────────────────────────

def get_params() -> dict:
    data = _load_json(PARAMS_FILE)
    return {**DEFAULT_PARAMS, **data}


def save_params(params: dict):
    _save_json(PARAMS_FILE, params)
    logger.info(f"Params saved: variant={params.get('ab_variant')}, "
                f"rsi_oversold={params.get('rsi_oversold')}")


def record_trade_result(win: bool):
    """Called by performance tracker after each closed trade."""
    params = get_params()
    variant = params.get("ab_variant", "A")
    if variant == "A":
        params["variant_A_trades"] = params.get("variant_A_trades", 0) + 1
        if win:
            params["variant_A_wins"] = params.get("variant_A_wins", 0) + 1
    else:
        params["variant_B_trades"] = params.get("variant_B_trades", 0) + 1
        if win:
            params["variant_B_wins"] = params.get("variant_B_wins", 0) + 1
    save_params(params)


def get_better_variant() -> str:
    """Returns 'A' or 'B', whichever has the higher win rate (min 5 trades)."""
    params = get_params()
    a_trades = params.get("variant_A_trades", 0)
    b_trades = params.get("variant_B_trades", 0)
    a_wins = params.get("variant_A_wins", 0)
    b_wins = params.get("variant_B_wins", 0)
    if a_trades < 5:
        return "B" if b_trades >= 5 else "A"
    if b_trades < 5:
        return "A"
    a_rate = a_wins / a_trades
    b_rate = b_wins / b_trades
    return "A" if a_rate >= b_rate else "B"
