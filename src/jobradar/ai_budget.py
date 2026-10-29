"""Daily AI spending cap backed by the `ai_usage` ledger (UTC days).

The meter is checked before every model call and records actual token usage
after it. When today's recorded spend reaches the cap, `check()` raises
`BudgetExceeded`; queue workers then return their match tasks unchanged (no
attempt is charged) to be resumed after the next UTC midnight, so no eligible
job is dropped. A single in-flight call may overrun the cap by its own cost
(at most a few cents with the configured models).

Paced (the worker's default): the cap grows through the UTC day, one 24th per
hour (cumulative, so an idle hour's share carries over). Jobs are then scored
within about an hour of being found instead of all at once after the reset.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine

from jobradar.db import ai_usage
from jobradar.llm import BudgetExceeded, cost_usd

log = logging.getLogger(__name__)
ENV = "JOBRADAR_AI_DAILY_BUDGET_USD"
PACING_ENV = "JOBRADAR_AI_BUDGET_PACING"   # "hourly" (default) or "off"


def budget_from_env() -> float | None:
    value = os.environ.get(ENV, "").strip()
    if not value:
        return None
    budget = float(value)
    if budget < 0:
        raise ValueError(f"{ENV} must be >= 0")
    return budget


def pacing_from_env() -> bool:
    value = os.environ.get(PACING_ENV, "hourly").strip().lower()
    if value not in ("hourly", "off"):
        raise ValueError(f"{PACING_ENV} must be 'hourly' or 'off'")
    return value == "hourly"


class DailyBudgetMeter:
    def __init__(self, engine: Engine, daily_budget_usd: float | None,
                 today=None, *, paced: bool = False, clock=lambda: datetime.now(UTC)) -> None:
        self.engine, self.budget, self.paced, self._clock = engine, daily_budget_usd, paced, clock
        self._today = today or (lambda: self._clock().date())

    def allowance(self) -> float | None:
        """Spend allowed so far today: the whole budget, or (paced) the share of
        the hours begun since UTC midnight."""
        if self.budget is None or not self.paced:
            return self.budget
        now = self._clock()
        hours_begun = now.hour + 1
        return round(self.budget * min(24, hours_begun) / 24, 6)

    def spent(self, day: date | None = None) -> float:
        with self.engine.connect() as conn:
            return float(conn.execute(select(func.coalesce(func.sum(ai_usage.c.cost_usd), 0))
                                      .where(ai_usage.c.day == (day or self._today()))).scalar_one())

    def exhausted(self) -> bool:
        return self.budget is not None and self.spent() >= self.allowance()

    def seconds_until_reset(self) -> float:
        now = datetime.now(UTC)
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        return max(60.0, (tomorrow - now).total_seconds())

    def check(self) -> None:
        if self.budget is None:
            return
        spent, allowed = self.spent(), self.allowance()
        if spent >= allowed:
            if allowed < self.budget:
                raise BudgetExceeded(f"AI budget for this hour reached: ${spent:.4f} of ${allowed:.4f} "
                                     f"(daily ${self.budget:.2f})")
            raise BudgetExceeded(f"daily AI budget reached: ${spent:.4f} of ${self.budget:.2f}")

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        cost = cost_usd(model, input_tokens, output_tokens)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO ai_usage (day, model, calls, input_tokens, output_tokens, cost_usd)
                VALUES (:day, :model, 1, :i, :o, :c)
                ON CONFLICT (day, model) DO UPDATE SET calls = ai_usage.calls + 1,
                    input_tokens = ai_usage.input_tokens + excluded.input_tokens,
                    output_tokens = ai_usage.output_tokens + excluded.output_tokens,
                    cost_usd = ai_usage.cost_usd + excluded.cost_usd
            """), {"day": self._today(), "model": model, "i": int(input_tokens), "o": int(output_tokens),
                   "c": cost})
        return cost
