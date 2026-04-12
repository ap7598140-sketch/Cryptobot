"""
Central configuration for the crypto trading system.
Toggle ROLLOUT_PHASE to control which coins are active:
  1 = BTC/USD only   (first 2 weeks)
  2 = BTC/USD + ETH/USD
  3 = BTC/USD + ETH/USD + SOL/USD
"""

# ── Phased coin rollout ──────────────────────────────────────────────────────
ROLLOUT_PHASE = 1  # Toggle: 1 | 2 | 3

COINS_BY_PHASE = {
    1: ["BTC/USD"],
    2: ["BTC/USD", "ETH/USD"],
    3: ["BTC/USD", "ETH/USD", "SOL/USD"],
}
ACTIVE_COINS: list[str] = COINS_BY_PHASE[ROLLOUT_PHASE]

# ── Portfolio & risk ─────────────────────────────────────────────────────────
PORTFOLIO_VALUE        = 1_000.0   # paper money starting value
CASH_RESERVE_PCT       = 0.40      # 40 % minimum cash at all times
MAX_POSITIONS          = 2
MAX_RISK_PER_TRADE     = 0.015     # 1.5 % of portfolio per trade
HARD_STOP_PCT          = 0.03      # 3 % hard stop (NO AI — immediate)
TAKE_PROFIT_PCT        = 0.06      # 6 % full take-profit
PARTIAL_EXIT_PCT       = 0.04      # 4 % partial exit (50 % of position)
TRAILING_TIER1_PROFIT  = 0.03      # at 3 % profit → trail 2 %
TRAILING_TIER1_TRAIL   = 0.02
TRAILING_TIER2_PROFIT  = 0.06      # at 6 % profit → trail 4 %
TRAILING_TIER2_TRAIL   = 0.04

DAILY_LOSS_LIMIT       = 0.05      # 5 % daily loss → all trading stops
MAX_DRAWDOWN           = 0.15      # 15 % drawdown from peak → system pause
MAX_CONSECUTIVE_LOSSES = 2
CONSEC_LOSS_PAUSE_HRS  = 2

# ── Scoring thresholds ───────────────────────────────────────────────────────
MIN_SCANNER_SCORE      = 70        # scanner must score ≥ 70 to forward
MIN_HAIKU_SCORE        = 70        # haiku analyzer must score ≥ 70 for Sonnet
MIN_SONNET_SCORE       = 75        # sonnet must score ≥ 75 to execute
MIN_REWARD_RISK        = 2.0       # 2 : 1 minimum R:R

# ── Trade execution ──────────────────────────────────────────────────────────
ENTRY_FIRST_FRAC       = 0.50      # 50 % position on first entry
ENTRY_CONFIRM_FRAC     = 0.50      # 50 % on confirmation
ORDER_TIMEOUT_S        = 300       # cancel unfilled orders after 5 min
ROUND_NUM_OFFSET_PCT   = 0.0003    # adjust entries ±0.03 % from round numbers

# ── Position management ──────────────────────────────────────────────────────
POSITION_TIME_LIMIT_HRS = 4        # exit if no movement in 4 hours

# ── Drawdown-based & decay-based sizing ──────────────────────────────────────
DRAWDOWN_SIZE_THRESHOLD = 0.08     # if portfolio down 8 % → reduce size
DRAWDOWN_SIZE_REDUCTION = 0.30     # reduce by 30 %
DECAY_LOOKBACK          = 20       # last N trades for decay check
DECAY_WIN_RATE_FLOOR    = 0.45     # below 45 % win rate → decay warning
DECAY_SIZE_REDUCTION    = 0.50     # reduce size 50 % on decay

# ── Bot run intervals ────────────────────────────────────────────────────────
SCANNER_INTERVAL_S      = 60
POSITION_MGR_INTERVAL_S = 30
RISK_GUARD_INTERVAL_S   = 30
PERF_TRACKER_INTERVAL_S = 7_200    # 2 hours
RECONCILE_INTERVAL_S    = 300      # 5 minutes

# ── Claude models ────────────────────────────────────────────────────────────
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS   = "claude-opus-4-6"

MAX_TOKENS_SCANNER  = 150
MAX_TOKENS_ANALYZER = 200
MAX_TOKENS_EXECUTOR = 250
MAX_TOKENS_DEFAULT  = 150

# ── Candle history ────────────────────────────────────────────────────────────
MAX_CANDLES = 12

# ── File paths ────────────────────────────────────────────────────────────────
REGIME_CACHE_FILE = "data/regime_cache.json"
TRADES_FILE       = "data/trades.json"
PARAMS_FILE       = "data/params.json"
LOGS_DIR          = "logs"
