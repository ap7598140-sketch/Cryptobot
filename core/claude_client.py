"""
Claude API wrapper.
- All responses are forced to pure JSON via system prompt
- Retry logic with exponential back-off
- Per-bot token budgets enforced here
"""
import asyncio
import json
import logging
from typing import Optional

import anthropic

from settings import (
    MODEL_HAIKU, MODEL_SONNET, MODEL_OPUS,
    MAX_TOKENS_SCANNER, MAX_TOKENS_ANALYZER,
    MAX_TOKENS_EXECUTOR, MAX_TOKENS_DEFAULT,
)

logger = logging.getLogger(__name__)

_JSON_SYSTEM = (
    "You are a crypto trading analysis AI. "
    "RESPOND WITH PURE JSON ONLY. "
    "No explanation, no markdown, no code blocks, no preamble, no extra text. "
    "Output only valid, parseable JSON and nothing else."
)


class ClaudeClient:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    # ── low-level call ────────────────────────────────────────────────────────

    async def call(
        self,
        model: str,
        prompt: str,
        max_tokens: int = MAX_TOKENS_DEFAULT,
        retries: int = 3,
    ) -> Optional[dict | list]:
        for attempt in range(retries):
            try:
                response = await self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=_JSON_SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = response.content[0].text.strip()
                # Strip any accidental markdown fences
                if raw.startswith("```"):
                    parts = raw.split("```")
                    raw = parts[1] if len(parts) > 1 else raw
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.error("JSON parse error (attempt %d/%d): %s", attempt + 1, retries, exc)
                if attempt == retries - 1:
                    return None
            except anthropic.RateLimitError:
                wait = 2 ** (attempt + 2)
                logger.warning("Rate limited — waiting %ds", wait)
                await asyncio.sleep(wait)
            except anthropic.APIStatusError as exc:
                logger.error("API status error (attempt %d): %s", attempt + 1, exc)
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None
            except Exception as exc:
                logger.error("Unexpected error (attempt %d): %s", attempt + 1, exc)
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None
        return None

    # ── model shortcuts ───────────────────────────────────────────────────────

    async def haiku(self, prompt: str, max_tokens: int = MAX_TOKENS_DEFAULT) -> Optional[dict | list]:
        return await self.call(MODEL_HAIKU, prompt, max_tokens)

    async def sonnet(self, prompt: str, max_tokens: int = MAX_TOKENS_DEFAULT) -> Optional[dict | list]:
        return await self.call(MODEL_SONNET, prompt, max_tokens)

    async def opus(self, prompt: str, max_tokens: int = MAX_TOKENS_DEFAULT) -> Optional[dict | list]:
        return await self.call(MODEL_OPUS, prompt, max_tokens)

    # ── named-budget shortcuts (signal intent at call-site) ───────────────────

    async def scanner_call(self, prompt: str) -> Optional[dict | list]:
        return await self.haiku(prompt, MAX_TOKENS_SCANNER)

    async def analyzer_haiku_call(self, prompt: str) -> Optional[dict]:
        return await self.haiku(prompt, MAX_TOKENS_ANALYZER)

    async def analyzer_sonnet_call(self, prompt: str) -> Optional[dict]:
        return await self.sonnet(prompt, MAX_TOKENS_ANALYZER)

    async def executor_call(self, prompt: str) -> Optional[dict]:
        return await self.sonnet(prompt, MAX_TOKENS_EXECUTOR)

    async def overnight_haiku_call(self, prompt: str) -> Optional[dict]:
        return await self.haiku(prompt, MAX_TOKENS_DEFAULT)

    async def overnight_opus_call(self, prompt: str) -> Optional[dict]:
        return await self.opus(prompt, 800)
