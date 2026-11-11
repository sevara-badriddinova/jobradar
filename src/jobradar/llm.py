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


class CredentialsRejected(LLMError):
    """The provider refused the account, not the request: a missing, revoked or
    invalid key, no permission, or no credit left. Retrying a job cannot help,
    so queue workers pause and keep the work queued instead of charging
    attempts (Nov 11: a dead key buried every match task for two days).
    `reason` is short and safe to show in an alert."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _credentials_reason(exc: Exception) -> str | None:
    if isinstance(exc, anthropic.AuthenticationError):
        return "401 invalid or revoked API key"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "403 key lacks permission"
    if isinstance(exc, anthropic.BadRequestError) and "credit balance" in str(exc).lower():
        return "account credit balance too low"
    return None


class BudgetExceeded(Exception):
    """Today's AI spending cap is reached. Not an LLMError: callers must keep the
    work queued for later instead of counting it as a failed attempt."""


# USD per million tokens (input, output), standard rates; longest prefix wins.
# https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-19).
PRICES = {
    "claude-haiku-3-5": (0.80, 4.00), "claude-haiku-4-5": (1.00, 5.00), "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-4": (3.00, 15.00), "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4": (15.00, 75.00),  # Opus 4 and 4.1
    "claude-opus-4-5": (5.00, 25.00), "claude-opus-4-6": (5.00, 25.00), "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00), "claude-opus-5": (5.00, 25.00), "claude-opus-5-5": (4.00, 20.00),
    "claude-fable-5": (10.00, 50.00), "claude-mythos-5": (10.00, 50.00),
}
# Unknown models are costed at the most expensive listed rate, so a cap is never
# silently exceeded by an unrecognized model. Cache reads are costed as full input
# (over-estimates; the matcher does not use prompt caching).
UNKNOWN_PRICE = (15.00, 75.00)


def price(model: str) -> tuple[float, float]:
    matches = [k for k in PRICES if model.startswith(k)]
    return PRICES[max(matches, key=len)] if matches else UNKNOWN_PRICE


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = price(model)
    return (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000


# Optional spend meter (see jobradar.ai_budget): .check() raises BudgetExceeded
# before a call; .record(model, input_tokens, output_tokens) after it.
_meter = None


def set_meter(meter) -> None:
    global _meter
    _meter = meter


@functools.cache
def _client() -> anthropic.Anthropic:
    # Without this check the SDK fails at request time with a bare TypeError.
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise CredentialsRejected("ANTHROPIC_API_KEY is not set",
                                  "create a key at https://console.anthropic.com")
    return anthropic.Anthropic()


def ask(output: type[T], *, system: str, prompt: str, model: str, max_tokens: int = 4096) -> T:
    """Return the model's answer as an instance of `output` (structured outputs)."""
    if _meter is not None:
        _meter.check()
    try:
        response = _client().messages.parse(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}], output_format=output,
        )
    except (anthropic.AnthropicError, ValidationError) as exc:
        reason = _credentials_reason(exc)
        if reason:
            raise CredentialsRejected(reason, f"{type(exc).__name__}: {exc}") from exc
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc
    usage = getattr(response, "usage", None)
    if _meter is not None and usage is not None:
        _meter.record(model, (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                      + (getattr(usage, "cache_read_input_tokens", 0) or 0), usage.output_tokens or 0)
    if response.parsed_output is None:
        raise LLMError(f"no structured output (stop_reason={response.stop_reason})")
    return response.parsed_output
