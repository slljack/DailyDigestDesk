"""Red flags: events that suggest something may be seriously wrong with a company.

Not the same as important: an earnings 8-K is important but routine. A red flag
is rare, almost always bad news, and easy to miss among ordinary filings, so the
digest marks it and puts it first. It means "read this one today", not "sell".

v1 flags five 8-K items. Pure: no store, no network.
"""

from __future__ import annotations

from daily_digest_desk.models import Event

# 8-K item numbers that are red flags, with why (the 8-K's title already names the item).
RED_FLAG_ITEMS = {
    "1.03": "bankruptcy or receivership: shareholders are usually paid last, often nothing",
    "2.06": "material impairment: something it owns is worth much less than it thought",
    "3.01": "delisting notice: the exchange may remove the stock",
    "4.01": "auditor change: usually harmless, sometimes a disagreement over the books",
    "4.02": "non-reliance: the company says its earlier financial statements were wrong",
}


def is_red_flag(event: Event) -> bool:
    """True for an 8-K (or its amendment) that reports any red-flag item."""
    if event.source != "edgar" or event.kind not in ("8-K", "8-K/A"):
        return False
    return any(item in RED_FLAG_ITEMS for item in event.fields.get("items", []))
