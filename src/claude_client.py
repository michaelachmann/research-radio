"""
Claude Client - Anthropic SDK wrapper for text generation and structured output.
"""

import time
from typing import Optional

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL


class ClaudeClient:
    """Thin wrapper around the Anthropic client with retry logic."""

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 2  # seconds

    def __init__(self, api_key: str = None, model: str = None):
        self.client = anthropic.Anthropic(api_key=api_key or ANTHROPIC_API_KEY)
        self.model = model or CLAUDE_MODEL

    def _should_retry(self, error: Exception, attempt: int) -> bool:
        if attempt >= self.MAX_RETRIES - 1:
            return False
        if isinstance(error, anthropic.RateLimitError):
            return True
        if isinstance(error, anthropic.APIStatusError):
            return error.status_code in (500, 503, 529)
        return False

    def generate(
        self,
        prompt: str,
        system: str = None,
        max_tokens: int = 8192,
        temperature: float = 0.7,
    ) -> Optional[str]:
        """
        Generate text from a prompt.

        Returns the response text, or None on failure.
        """
        for attempt in range(self.MAX_RETRIES):
            try:
                kwargs = dict(
                    model=self.model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                if system:
                    kwargs["system"] = system
                response = self.client.messages.create(**kwargs)
                return response.content[0].text
            except Exception as e:
                if self._should_retry(e, attempt):
                    delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"  Claude API error, retrying in {delay}s: {e}")
                    time.sleep(delay)
                    continue
                print(f"  Claude API error: {e}")
                return None
        return None

    def generate_json(
        self,
        prompt: str,
        schema: dict,
        tool_name: str = "extract",
        system: str = None,
    ) -> Optional[dict]:
        """
        Force structured JSON output using tool_use.

        Defines a single tool with the provided JSON schema and forces Claude
        to call it, returning the input dict as structured data.
        """
        tool = {
            "name": tool_name,
            "description": "Extract and return structured data from the provided text.",
            "input_schema": schema,
        }

        for attempt in range(self.MAX_RETRIES):
            try:
                kwargs = dict(
                    model=self.model,
                    max_tokens=4096,
                    tools=[tool],
                    tool_choice={"type": "tool", "name": tool_name},
                    messages=[{"role": "user", "content": prompt}],
                )
                if system:
                    kwargs["system"] = system
                response = self.client.messages.create(**kwargs)
                for block in response.content:
                    if block.type == "tool_use":
                        return block.input
                return None
            except Exception as e:
                if self._should_retry(e, attempt):
                    delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"  Claude API error, retrying in {delay}s: {e}")
                    time.sleep(delay)
                    continue
                print(f"  Claude API error: {e}")
                return None
        return None
