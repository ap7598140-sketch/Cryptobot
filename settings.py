"""
Central configuration. Edit ROLLOUT_PHASE to control active coins.
  1 = BTC/USD only  |  2 = + ETH/USD  |  3 = + SOL/USD
"""

# -- Phased coin rollout -------------------------------------------------------
ROLLOUT_PHASE = 3

COINS_BY_PHASE = {
    1: ["BTC/USD"],
    2: ["BTC/USD", "ETH/USD"],
    3: ["BTC/USD", "ETH/USD", "SOL/USD"],
}
ACTIVE_COINS: list[str] = COINS_BY_PHASE[ROLLOUT_PHASE]

# -- Portfolio & risk ----------------------------------------------------------
PORTFOLIO_VALUE        = 1_000.0
CASH_RESERVE_PCT       = 0.40
MAX_POSITIONS          = 2
MAX_RISK_PER_TRADE     = 0.015      # 1.5%
HARD_STOP_PCT          = 0.03       # 3% -- NO AI, immediate
TAKE_PROFIT_PCT        = 0.06       # 6%
PARTIAL_EXIT_PCT       = 0.04       # 4%
TRAILING_TIER1_PROFIT  = 0.03
TRAILING_TIER1_TRAIL   = 0.02
TRAILING_TIER2_PROFIT  = 0.06
TRAILING_TIER2_TRAIL   = 0.04
DAILY_LOSS_LIMIT       = 0.05
MAX_DRAWDOWN           = 0.15
MAX_CONSECUTIVE_LOSSES = 2
CONSEC_LOSS_PAUSE_HRS  = 2

# -- Scoring thresholds --------------------------------------------------------
MIN_SCANNER_SCORE   = 60
MIN_HAIKU_SCORE     = 60
MIN_SONNET_SCORE    = 65
MIN_REWARD_RISK     = 2.0

# -- Trade execution -----------------------------------------------------------
ENTRY_FIRST_FRAC     = 0.50
ENTRY_CONFIRM_FRAC   = 0.50
ORDER_TIMEOUT_S      = 300
ROUND_NUM_OFFSET_PCT = 0.0003

# -- Position management -------------------------------------------------------
POSITION_TIME_LIMIT_HRS = 4

# -- Sizing adjustments --------------------------------------------------------
DRAWDOWN_SIZE_THRESHOLD = 0.08
DRAWDOWN_SIZE_REDUCTION = 0.30
DECAY_LOOKBACK          = 20
DECAY_WIN_RATE_FLOOR    = 0.45
DECAY_SIZE_REDUCTION    = 0.50

# -- Bot intervals -------------------------------------------------------------
SCANNER_INTERVAL_S      = 60
POSITION_MGR_INTERVAL_S = 30
RISK_GUARD_INTERVAL_S   = 30
PERF_TRACKER_INTERVAL_S = 7_200
RECONCILE_INTERVAL_S    = 300

# -- Claude models & tokens ----------------------------------------------------
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS   = "claude-opus-4-6"
MAX_TOKENS   = 1000          # single budget for all Claude calls

# -- Candle history ------------------------------------------------------------
MAX_CANDLES = 12

# -- File paths ----------------------------------------------------------------
REGIME_CACHE_FILE  = "data/regime_cache.json"
PARAMS_FILE        = "data/params.json"
TRADES_CSV         = "data/trades.csv"
LOGS_DIR           = "logs"
AI_RESPONSES_DIR   = "logs/ai_responses"
