"""Normalized event model. Every source (EDGAR, FRED, Finnhub) emits Events.

Downstream layers (scoring, summarization, output) only ever see Events, so
adding a source means writing one mapper, not touching the rest of the pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Event(BaseModel):
    """One thing that became public, from one source.

    `id` is deterministic (`<source>:<native id>`, e.g. `edgar:0000320193-24-000123`),
    so fetching the same item twice is a no-op in the store. That makes
    ingestion idempotent: overlapping fetch windows are safe.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    source: str                  # "edgar", "fred", "finnhub"
    kind: str                    # source-specific type, e.g. "8-K", "4", "SC 13D"
    ticker: str | None           # None for macro events
    occurred_at: datetime        # when it became public; always UTC
    title: str
    url: str
    fields: dict[str, Any] = Field(default_factory=dict)  # structured, source-specific

    @field_validator("occurred_at")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        # Naive datetimes are ambiguous (ET? UTC?) and break ordering across sources.
        if v.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return v.astimezone(timezone.utc)
