"""The only place that talks to an LLM API (Claude, via the official SDK).

Callers ask for a Pydantic model and get a validated instance back. Swapping
providers later means changing this file only. The SDK already retries
rate limits and server errors with backoff.
"""

import functools
import os
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The model call failed or didn't return the requested structure."""


@functools.cache
def _client() -> anthropic.Anthropic:
    # Without this check the SDK fails at request time with a bare TypeError.
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise LLMError("ANTHROPIC_API_KEY is not set (create a key at https://console.anthropic.com)")
    return anthropic.Anthropic()


def ask(output: type[T], *, system: str, prompt: str, model: str, max_tokens: int = 4096) -> T:
    """Return the model's answer as an instance of `output` (structured outputs)."""
    try:
        response = _client().messages.parse(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}], output_format=output,
        )
    except (anthropic.AnthropicError, ValidationError) as exc:
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc
    if response.parsed_output is None:
        raise LLMError(f"no structured output (stop_reason={response.stop_reason})")
    return response.parsed_output
