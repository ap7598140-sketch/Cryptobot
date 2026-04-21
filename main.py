"""
Crypto Trading Bot -- main entry point.
"""
import asyncio
import logging
import os
import sys
from dotenv import load_dotenv

load_dotenv()
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("logs/bot.log")],
)
logger = logging.getLogger(__name__)

REQUIRED_ENV = ["ALPACA_API_KEY","ALPACA_SECRET_KEY","ANTHROPIC_API_KEY","TELEGRAM_TOKEN","TELEGRAM_CHAT_ID"]
def _check_env():
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        logger.error("Missing environment variables: %s", missing)
        sys.exit(1)
_check_env()

from core.alpaca_client import AlpacaClient
from core.claude_client import ClaudeClient
from core.state import state
from core.telegram_notifier import TelegramNotifier
from bots.market_scanner import MarketScanner
from bots.trend_analyzer import TrendAnalyzer
from bots.trade_executor import TradeExecutor
from bots.position_manager import PositionManager
from bots.risk_guard import RiskGuard
from bots.performance_tracker import PerformanceTracker
from bots.overnight_analyst import OvernightAnalyst

async def main():
    alpaca   = AlpacaClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    claude   = ClaudeClient(os.environ["ANTHROPIC_API_KEY"])
    telegram = TelegramNotifier(os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    coingecko_key = os.getenv("COINGECKO_API_KEY", "")

    await telegram.initialize()
    await telegram.send("Crypto Bot starting up...")

    signal_queue   = asyncio.Queue()
    executor_queue = asyncio.Queue()

    tasks = [
        asyncio.create_task(MarketScanner(claude, alpaca, signal_queue, coingecko_key).run(), name="scanner"),
        asyncio.create_task(TrendAnalyzer(claude, alpaca, signal_queue, executor_queue).run(), name="analyzer"),
        asyncio.create_task(TradeExecutor(claude, alpaca, executor_queue, telegram).run(), name="executor"),
        asyncio.create_task(PositionManager(claude, alpaca, telegram).run(), name="position_mgr"),
        asyncio.create_task(RiskGuard(claude, alpaca, telegram).run(), name="risk_guard"),
        asyncio.create_task(PerformanceTracker(claude, telegram).run(), name="perf_tracker"),
        asyncio.create_task(OvernightAnalyst(claude, alpaca, telegram, coingecko_key).run(), name="overnight"),
    ]
    logger.info("All 7 bots running")
    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, Exception) as e:
        if not isinstance(e, asyncio.CancelledError):
            logger.error("Fatal error: %s", e)
    finally:
        for t in tasks: t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await telegram.system_stopped()
        await telegram.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt -- shutting down")
