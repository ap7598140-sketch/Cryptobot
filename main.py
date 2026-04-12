"""
main.py — Crypto Trading System Orchestrator
Launches all 7 bots as async tasks in a single event loop.
/stop Telegram command halts everything gracefully.
"""
import asyncio
import logging
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ── Logging ─────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/system.log"),
    ],
)
logger = logging.getLogger("main")


# ── Env validation ────────────────────────────────────────────────────────────
def _require_env(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        logger.error("Missing required env var: %s", key)
        sys.exit(1)
    return val


ANTHROPIC_API_KEY = _require_env("ANTHROPIC_API_KEY")
ALPACA_API_KEY    = _require_env("ALPACA_API_KEY")
ALPACA_SECRET_KEY = _require_env("ALPACA_SECRET_KEY")
TELEGRAM_TOKEN    = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID  = _require_env("TELEGRAM_CHAT_ID")
COINGLASS_KEY     = os.getenv("COINGLASS_API_KEY", "")
COINGECKO_KEY     = os.getenv("COINGECKO_API_KEY", "")


# ── Core imports (after env validated) ───────────────────────────────────────
from core.alpaca_client     import AlpacaClient
from core.claude_client     import ClaudeClient
from core.state             import state
from core.telegram_notifier import TelegramNotifier

from bots.market_scanner    import MarketScanner
from bots.trend_analyzer    import TrendAnalyzer
from bots.trade_executor    import TradeExecutor
from bots.position_manager  import PositionManager
from bots.risk_guard        import RiskGuard
from bots.performance_tracker import PerformanceTracker
from bots.overnight_analyst import OvernightAnalyst


async def main():
    logger.info("=" * 60)
    logger.info("  Crypto Trading System — starting up")
    logger.info("=" * 60)

    # ── Instantiate core clients ──────────────────────────────────────────────
    claude   = ClaudeClient(ANTHROPIC_API_KEY)
    alpaca   = AlpacaClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    telegram = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

    await telegram.initialize()
    logger.info("Telegram bot online")

    # ── Inter-bot signal queues ───────────────────────────────────────────────
    # scanner → trend analyzer
    scanner_queue  = asyncio.Queue(maxsize=50)
    # trend analyzer → trade executor
    executor_queue = asyncio.Queue(maxsize=20)

    # ── Initialise portfolio value from Alpaca ─────────────────────────────────
    portfolio_value = await asyncio.to_thread(alpaca.get_portfolio_value)
    await state.update_portfolio_value(portfolio_value)
    state.portfolio_peak = portfolio_value
    logger.info("Portfolio value: $%.2f", portfolio_value)

    # ── Instantiate bots ──────────────────────────────────────────────────────
    scanner = MarketScanner(
        claude=claude, alpaca=alpaca,
        signal_queue=scanner_queue,
        coinglass_key=COINGLASS_KEY,
        coingecko_key=COINGECKO_KEY,
    )

    analyzer = TrendAnalyzer(
        claude=claude, alpaca=alpaca,
        signal_queue=scanner_queue,
        executor_queue=executor_queue,
    )

    executor = TradeExecutor(
        claude=claude, alpaca=alpaca,
        executor_queue=executor_queue,
        telegram=telegram,
    )

    position_mgr = PositionManager(
        alpaca=alpaca, telegram=telegram,
        signal_queue=scanner_queue,   # for re-entry signals
    )

    risk_guard = RiskGuard(
        alpaca=alpaca, telegram=telegram,
    )

    perf_tracker = PerformanceTracker(
        claude=claude, telegram=telegram,
    )

    overnight = OvernightAnalyst(
        claude=claude, telegram=telegram,
        coingecko_key=COINGECKO_KEY,
    )

    # ── Launch all 7 bots as concurrent async tasks ────────────────────────────
    tasks = [
        asyncio.create_task(scanner.run(),       name="MarketScanner"),
        asyncio.create_task(analyzer.run(),      name="TrendAnalyzer"),
        asyncio.create_task(executor.run(),      name="TradeExecutor"),
        asyncio.create_task(position_mgr.run(),  name="PositionManager"),
        asyncio.create_task(risk_guard.run(),    name="RiskGuard"),
        asyncio.create_task(perf_tracker.run(),  name="PerformanceTracker"),
        asyncio.create_task(overnight.run(),     name="OvernightAnalyst"),
    ]

    logger.info("All 7 bots launched")
    await telegram.send(
        "SYSTEM STARTED\n"
        "All 7 bots running.\n"
        "Send /stop to halt all bots."
    )

    # ── Monitor tasks — restart on unexpected crash ────────────────────────────
    try:
        while True:
            if state.stop_command:
                logger.info("Stop command received — shutting down")
                break

            # Check for crashed tasks and log them
            done = [t for t in tasks if t.done()]
            for t in done:
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    logger.critical("Bot %s crashed: %s", t.get_name(), exc)
                tasks.remove(t)

            await asyncio.sleep(5)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Interrupted — shutting down")

    finally:
        # ── Graceful shutdown ──────────────────────────────────────────────────
        logger.info("Cancelling all bot tasks")
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await telegram.system_stopped()
        await telegram.shutdown()
        logger.info("System shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
