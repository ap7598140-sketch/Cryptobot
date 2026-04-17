"""
Claude API wrapper.
- Forces pure JSON via system prompt
- Saves every AI response to logs/ai_responses/
- Single MAX_TOKENS budget for all bots
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import anthropic

from settings import MODEL_HAIKU, MODEL_SONNET, MODEL_OPUS, MAX_TOKENS, AI_RESPONSES_DIR

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a crypto trading analysis AI. "
    "RESPOND WITH PURE JSON ONLY. "
    "No explanation, no markdown, no code blocks, no extra text. "
    "Output only valid, parseable JSON."
)


def _save_response(bot: str, prompt: str, raw: str):
    os.makedirs(AI_RESPONSES_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(AI_RESPONSES_DIR, f"{bot}_{ts}.json")
    try:
        with open(path, "w") as f:
            json.dump({"bot": bot, "prompt": prompt, "response": raw,
                       "ts": datetime.now(timezone.utc).isoformat()}, f, indent=2)
    except Exception as e:
        logger.error("Failed to save AI response: %s", e)


class ClaudeClient:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def call(self, model: str, prompt: str, bot: str = "unknown",
                   retries: int = 3) -> Optional[dict | list]:
        for attempt in range(retries):
            try:
                resp = await self.client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=_SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = resp.content[0].text.strip()
                if raw.startswith("```"):
                    parts = raw.split("```")
                    raw = parts[1][4:].strip() if parts[1].startswith("json") else parts[1].strip()
                _save_response(bot, prompt, raw)
                return json.loads(raw)
            except json.JSONDecodeError as e:
                logger.error("[%s] JSON parse error attempt %d: %s", bot, attempt + 1, e)
                if attempt == retries - 1:
                    return None
            except anthropic.RateLimitError:
                wait = 2 ** (attempt + 2)
                logger.warning("[%s] Rate limited -- sleeping %ds", bot, wait)
                await asyncio.sleep(wait)
            except Exception as e:
                logger.error("[%s] API error attempt %d: %s", bot, attempt + 1, e)
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None
        return None

    async def haiku(self, prompt: str, bot: str = "haiku") -> Optional[dict | list]:
        return await self.call(MODEL_HAIKU, prompt, bot)

    async def sonnet(self, prompt: str, bot: str = "sonnet") -> Optional[dict | list]:
        return await self.call(MODEL_SONNET, prompt, bot)

    async def opus(self, prompt: str, bot: str = "opus") -> Optional[dict | list]:
        return await self.call(MODEL_OPUS, prompt, bot)
