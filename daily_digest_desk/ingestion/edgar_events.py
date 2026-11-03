"""EDGAR filings -> Events.

Fetching (`fetch_edgar_events`) is separate from mapping (`filing_to_event`)
so the mapping can be unit-tested with fake filings and no network.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, time, timezone

from daily_digest_desk.models import Event
from daily_digest_desk.ingestion.edgar import _company

log = logging.getLogger(__name__)

# Forms worth surfacing in a daily digest. 13D/G were renamed "SCHEDULE 13D/G"
# when SEC moved them to structured XML; both spellings appear in the index.
WATCH_FORMS: tuple[str, ...] = (
    "8-K", "8-K/A",
    "4",
    "SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A",
    "SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A",
    "10-Q", "10-K",
    "S-3",
)

FORM_DESCRIPTIONS: dict[str, str] = {
    "4": "Insider transaction",
    "SC 13D": "Activist / >5% stake",
    "SCHEDULE 13D": "Activist / >5% stake",
    "SC 13G": "Passive >5% stake",
    "SCHEDULE 13G": "Passive >5% stake",
    "10-Q": "Quarterly report",
    "10-K": "Annual report",
    "S-3": "Shelf registration (possible dilution)",
}

# 8-K item numbers -> short labels. The item number is the cheapest
# materiality signal there is: 4.02 is a red flag, 7.01/9.01 are usually noise.
ITEM_DESCRIPTIONS: dict[str, str] = {
    "1.01": "Material agreement",
    "1.02": "Material agreement terminated",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Acquisition or disposition completed",
    "2.02": "Earnings results",
    "2.03": "New debt obligation",
    "2.05": "Exit / restructuring costs",
    "2.06": "Material impairment",
    "3.01": "Delisting notice",
    "3.02": "Unregistered equity sale",
    "4.01": "Auditor change",
    "4.02": "Non-reliance on prior financials",
    "5.01": "Change in control",
    "5.02": "Director / officer change",
    "5.07": "Shareholder vote results",
    "7.01": "Reg FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}


def parse_items(raw: str | Iterable[str] | None) -> list[str]:
    """'2.02,9.01' -> ['2.02', '9.01']. Tolerates None, '', and lists."""
    if not raw:
        return []
    parts = raw.split(",") if isinstance(raw, str) else raw
    return [p.strip() for p in parts if p and p.strip()]


def _base_form(form: str) -> str:
    return form.removesuffix("/A")


def _occurred_at(filing) -> datetime:
    """Acceptance time if available (tz-aware), else midnight UTC of the filing date."""
    accepted = getattr(filing, "acceptance_datetime", None)
    if isinstance(accepted, datetime) and accepted.tzinfo is not None:
        return accepted
    filed = filing.filing_date
    if isinstance(filed, str):
        filed = date.fromisoformat(filed)
    return datetime.combine(filed, time.min, tzinfo=timezone.utc)


def filing_to_event(ticker: str, filing) -> Event:
    form = filing.form
    items = parse_items(getattr(filing, "items", None))
    amended = " (amended)" if form.endswith("/A") else ""

    if _base_form(form) == "8-K":
        labels = [ITEM_DESCRIPTIONS.get(i, f"Item {i}") for i in items if i != "9.01"]
        summary = "; ".join(labels) or "Current report"
    else:
        summary = FORM_DESCRIPTIONS.get(_base_form(form), form)

    return Event(
        id=f"edgar:{filing.accession_number}",
        source="edgar",
        kind=form,
        ticker=ticker.upper(),
        occurred_at=_occurred_at(filing),
        title=f"{ticker.upper()} {form}{amended}: {summary}",
        url=getattr(filing, "homepage_url", None) or filing.filing_url,
        fields={
            "accession_number": filing.accession_number,
            "filing_date": str(filing.filing_date),
            "items": items,
        },
    )


def fetch_edgar_events(
    tickers: Iterable[str],
    since: date,
    forms: Iterable[str] = WATCH_FORMS,
) -> tuple[list[Event], dict[str, str]]:
    """Filings on or after `since` for each ticker.

    Returns (events, errors). One failing ticker (bad symbol, network blip)
    is logged and reported in `errors` instead of aborting the whole run.
    """
    events: list[Event] = []
    errors: dict[str, str] = {}
    for ticker in tickers:
        try:
            filings = _company(ticker).get_filings(
                form=list(forms), filing_date=f"{since.isoformat()}:"
            )
            events.extend(filing_to_event(ticker, f) for f in filings)
        except Exception as exc:  # noqa: BLE001 — isolate per-ticker failures
            log.warning("EDGAR fetch failed for %s: %s", ticker, exc)
            errors[ticker] = str(exc)
    return events, errors
