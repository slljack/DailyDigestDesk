"""EDGAR filings -> Events.

Fetching (`fetch_edgar_events`) is separate from mapping (`filing_to_event`)
so the mapping can be unit-tested with fake filings and no network.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, time, timezone

from daily_digest_desk.forms import PROSPECTUS_FORMS, SCHEDULE_13D_FORMS, SCHEDULE_13G_FORMS
from daily_digest_desk.ingestion.edgar import _company
from daily_digest_desk.models import Event

log = logging.getLogger(__name__)

# Forms worth surfacing in a daily digest. The form families are in forms.py.
WATCH_FORMS: tuple[str, ...] = (
    "8-K", "8-K/A",
    "6-K", "6-K/A",  # a foreign company's 8-K (e.g. NBIS)
    "4",
    *SCHEDULE_13D_FORMS,
    *SCHEDULE_13G_FORMS,
    "10-Q", "10-K",
    "S-3", "S-3ASR",  # S-3ASR: the automatic shelf for large companies
    *PROSPECTUS_FORMS,
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
    "S-3ASR": "Shelf registration (possible dilution)",
    # Unlike an 8-K, a 6-K has no item numbers, so the title can't say what it's about.
    "6-K": "Current report (foreign company)",
    **{form: "Prospectus: securities being offered" for form in PROSPECTUS_FORMS},
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


def filing_to_event(ticker: str, filing, company: str | None = None) -> Event:
    """`company` is the ticker's name as SEC has it ("Hut 8 Corp."), saved so the
    summary checker can tell the 8 in "Hut 8" from a number. None leaves it out."""
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
            **({"company": company} if company else {}),
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
            company = _company(ticker)
            filings = company.get_filings(
                form=list(forms), filing_date=f"{since.isoformat()}:"
            )
            # The name is on the Company we already have: no extra SEC request.
            events.extend(filing_to_event(ticker, f, company.name) for f in filings)
        except Exception as exc:  # noqa: BLE001 — isolate per-ticker failures
            log.warning("EDGAR fetch failed for %s: %s", ticker, exc)
            errors[ticker] = str(exc)
    return events, errors
