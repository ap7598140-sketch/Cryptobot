"""
Crypto Trading Bot -- main entry point.
Starts 7 async bots as concurrent asyncio tasks.
"""
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# -- Logging setup -------------------------------------------------------------
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log"),
    ],
)
logger = logging.getLogger(__name__)

# -- Config validation ---------------------------------------------------------
REQUIRED_ENV = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY",
                "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]

def _check_env():
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        logger.error("Missing environment variables: %s", missing)
        sys.exit(1)

_check_env()

# -- Imports -------------------------------------------------------------------
from core.alpaca_client import AlpacaClient
from core.claude_client  import ClaudeClient
from core.state          import state
from core.telegram_notifier import TelegramNotifier

from bots.market_scanner      import MarketScanner
from bots.trend_analyzer      import TrendAnalyzer
from bots.trade_executor      import TradeExecutor
from bots.position_manager    import PositionManager
from bots.risk_guard          import RiskGuard
from bots.performance_tracker import PerformanceTracker
from bots.overnight_analyst   import OvernightAnalyst


async def main():
    # -- Clients ---------------------------------------------------------------
    alpaca   = AlpacaClient(os.environ["ALPACA_API_KEY"],
                             os.environ["ALPACA_SECRET_KEY"])
    claude   = ClaudeClient(os.environ["ANTHROPIC_API_KEY"])
    telegram = TelegramNotifier(os.environ["TELEGRAM_TOKEN"],
                                 os.environ["TELEGRAM_CHAT_ID"])

    coingecko_key = os.getenv("COINGECKO_API_KEY", "")

    await telegram.initialize()
    await telegram.send("Crypto Bot starting up...")

    # -- Queues ----------------------------------------------------------------
    signal_queue   = asyncio.Queue()
    executor_queue = asyncio.Queue()

    # -- Bots ------------------------------------------------------------------
    scanner   = MarketScanner(claude, alpaca, signal_queue, coingecko_key)
    analyzer  = TrendAnalyzer(claude, alpaca, signal_queue, executor_queue)
    executor  = TradeExecutor(claude, alpaca, executor_queue, telegram)
    pos_mgr   = PositionManager(claude, alpaca, telegram)
    risk      = RiskGuard(claude, alpaca, telegram)
    perf      = PerformanceTracker(claude, telegram)
    overnight = OvernightAnalyst(claude, alpaca, telegram, coingecko_key)

    tasks = [
        asyncio.create_task(scanner.run(),   name="scanner"),
        asyncio.create_task(analyzer.run(),  name="analyzer"),
        asyncio.create_task(executor.run(),  name="executor"),
        asyncio.create_task(pos_mgr.run(),   name="position_mgr"),
        asyncio.create_task(risk.run(),      name="risk_guard"),
        asyncio.create_task(perf.run(),      name="perf_tracker"),
        asyncio.create_task(overnight.run(), name="overnight"),
    ]

    logger.info("All 7 bots running")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Fatal error: %s", e)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await telegram.system_stopped()
        await telegram.shutdown()
        logger.info("All bots stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt -- shutting down")
