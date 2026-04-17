"""
Telegram notifications + /stop command listener.
"""
import logging
from typing import Optional

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError

from core.state import state

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self._bot: Optional[Bot] = None
        self._app: Optional[Application] = None

    async def initialize(self):
        self._bot = Bot(token=self.token)
        self._app = Application.builder().token(self.token).build()
        self._app.add_handler(CommandHandler("stop", self._handle_stop))
        await self._app.initialize()
        await self._app.start()
        if self._app.updater:
            await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot online")

    async def shutdown(self):
        if self._app:
            if self._app.updater:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def _handle_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state.stop_command = True
        await update.message.reply_text("STOP received -- halting all bots.")
        logger.warning("/stop command received")

    async def send(self, message: str):
        if not self._bot:
            return
        try:
            await self._bot.send_message(chat_id=self.chat_id, text=message)
        except TelegramError as e:
            logger.error("Telegram send error: %s", e)

    async def trade_opened(self, coin: str, direction: str, strategy: str,
                           price: float, qty: float, score: int, usd: float):
        await self.send(
            f"TRADE OPENED\n"
            f"  {coin} {direction.upper()} [{strategy}]\n"
            f"  Entry: ${price:,.2f}  Qty: {qty:.4f}  (${usd:,.2f})\n"
            f"  Score: {score}/100"
        )

    async def take_profit(self, coin: str, gain_pct: float, gain_usd: float):
        await self.send(f"TAKE PROFIT\n  {coin}\n  +{gain_pct:.2f}%  (+${gain_usd:,.2f})")

    async def stop_loss_fired(self, coin: str, loss_pct: float, loss_usd: float):
        await self.send(f"STOP LOSS\n  {coin}\n  -{loss_pct:.2f}%  (-${loss_usd:,.2f})")

    async def trailing_stop_triggered(self, coin: str, profit_pct: float):
        await self.send(f"TRAILING STOP\n  {coin}\n  Locked: +{profit_pct:.2f}%")

    async def partial_exit(self, coin: str, sold_qty: float, remaining: float,
                           price: float, gain_pct: float):
        await self.send(
            f"PARTIAL EXIT\n  {coin}\n"
            f"  Sold {sold_qty:.4f} @ ${price:,.2f}  (+{gain_pct:.2f}%)\n"
            f"  Remaining: {remaining:.4f}"
        )

    async def daily_loss_limit(self, loss_pct: float):
        await self.send(f"DAILY LOSS LIMIT\n  -{loss_pct:.2f}% -- paused until midnight UTC")

    async def drawdown_circuit_breaker(self, dd_pct: float):
        await self.send(f"DRAWDOWN CIRCUIT BREAKER\n  -{dd_pct:.2f}% from peak -- fully paused")

    async def consecutive_loss_pause(self, resume_time: str):
        await self.send(f"CONSECUTIVE LOSS PAUSE\n  Resuming at {resume_time} UTC")

    async def reconciliation_mismatch(self, details: str):
        await self.send(f"RECONCILIATION MISMATCH\n  {details}\n  Trading paused")

    async def strategy_decay_warning(self, win_rate: float, action: str):
        await self.send(f"STRATEGY DECAY\n  Win rate: {win_rate:.1f}%\n  {action}")

    async def volatility_spike_block(self, coin: str):
        await self.send(f"VOLATILITY SPIKE\n  {coin} -- new entries paused")

    async def morning_briefing(self, text: str):
        await self.send(f"MORNING BRIEFING\n\n{text}")

    async def system_stopped(self):
        await self.send("SYSTEM STOPPED -- all bots halted")
