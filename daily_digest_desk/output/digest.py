"""Build the digest page: events in, one HTML page out.

Pure: no store, no files, no clock. collect.write_digest reads the events and
writes the file; this module only turns them into text.
"""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from daily_digest_desk.models import Event

_NEW_YORK = ZoneInfo("America/New_York")


def _item(event: Event) -> str:
    """One event as an <li> line: the title (a link if the url is http/https) and its day."""
    # Titles and urls come from outside sources: escape them, so a "<script>" in a
    # title shows up as text instead of running in the browser.
    title = html.escape(event.title)

    # New York first, then the day: a filing at 9pm New York is 2am UTC the next day.
    # .day, not %d, so it's "Nov 6", not "Nov 06".
    local = event.occurred_at.astimezone(_NEW_YORK)
    day = f"{local:%b} {local.day}"

    # Escaping can't make "javascript:..." safe to click, so only web links become links.
    if event.url.startswith(("http://", "https://")):
        title = f'<a href="{html.escape(event.url)}">{title}</a>'

    return f'<li>{title} <span class="date">{day}</span></li>'


# What a folded group is called, by event kind. Several kinds can share a label, so
# they fold together: an 8-K and its amendment are both "8-K filings". A kind not
# listed here folds under its own name.
_GROUP_LABELS = {
    "8-K": "8-K filings", "8-K/A": "8-K filings",
    "4": "Insider transactions",
    **{form: "Big-holder stake filings" for form in (
        "SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A",
        "SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A",
    )},
    "10-Q": "Quarterly reports",
    "10-K": "Annual reports",
    "S-3": "Share offering filings",
    "earnings_headsup": "Earnings heads-ups",
    "earnings_result": "Earnings results",
    "big_move": "Big moves",
    "CPIAUCSL": "CPI releases",
    "PAYEMS": "Jobs reports",
}


# Only these fold: they pile up and mostly say the same thing. Everything else
# (8-Ks, earnings, big moves, releases) always gets its own line, so an earnings
# 8-K is never hidden behind a click.
_NOISY_LABELS = {"Insider transactions", "Big-holder stake filings"}


def _label(event: Event) -> str:
    return _GROUP_LABELS.get(event.kind, event.kind)


def _section(heading: str, events: list[Event]) -> str:
    """A heading and its events, newest first. Two or more of a noisy label fold
    into one click-to-open line with a count: "Insider transactions (3)".
    """
    # Newest first; id breaks ties so the order is the same on every run.
    newest_first = sorted(events, key=lambda e: (e.occurred_at, e.id), reverse=True)

    # The events of each noisy label, still newest first.
    noisy: dict[str, list[Event]] = {}
    for event in newest_first:
        if _label(event) in _NOISY_LABELS:
            noisy.setdefault(_label(event), []).append(event)

    lines = []
    folded: set[str] = set()
    for event in newest_first:
        group = noisy.get(_label(event), [])
        if len(group) < 2:
            lines.append(_item(event))
        elif _label(event) not in folded:
            # The whole group goes where its newest event would have been.
            folded.add(_label(event))
            items = "\n".join(_item(e) for e in group)
            lines.append(
                f"<li><details><summary>{html.escape(_label(event))} ({len(group)})</summary>\n"
                f"<ul>\n{items}\n</ul></details></li>"
            )

    body = "\n".join(lines)
    return f"<section>\n<h2>{html.escape(heading)}</h2>\n<ul>\n{body}\n</ul>\n</section>"


def build_digest(events: list[Event], now: datetime, tickers: list[str]) -> str:
    """The whole page: Macro & Market first, then each holding in holdings order.

    A holding with nothing new is left out. Events for a ticker no longer in
    `tickers` (a holding you removed) still show, after the holdings, A to Z.
    `now` is only for the header; the caller decides which events are new.
    """
    local = now.astimezone(_NEW_YORK)
    hour = local.hour % 12 or 12  # 0 -> 12am, 13 -> 1pm
    when = f"{local:%a %b} {local.day}, {hour}:{local:%M}{'am' if local.hour < 12 else 'pm'}"

    by_ticker: dict[str | None, list[Event]] = {}
    for event in events:
        by_ticker.setdefault(event.ticker, []).append(event)

    sections = []
    if None in by_ticker:  # no ticker: FRED releases and macro big moves
        sections.append(_section("Macro & Market", by_ticker[None]))
    removed = sorted(t for t in by_ticker if t is not None and t not in tickers)
    for ticker in [*tickers, *removed]:
        if ticker in by_ticker:
            sections.append(_section(ticker, by_ticker[ticker]))

    if events:
        count = f"{len(events)} new item{'s' if len(events) != 1 else ''}"
        body = "\n".join(sections)
    else:
        count = "Nothing new since the last digest."
        body = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Digest · {when}</title>
<style>{_STYLE}</style>
</head>
<body>
<header>
<h1>Daily digest</h1>
<p class="meta">{when} · {count}</p>
</header>
{body}
</body>
</html>
"""


# Light by default, dark when the system is. Kept small: one column, readable type.
_STYLE = """
:root { --bg: #ffffff; --text: #1f2328; --muted: #6e7781; --link: #0969da; --line: #d8dee4; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --text: #e6edf3; --muted: #8d96a0; --link: #4493f8; --line: #30363d; }
}
body { background: var(--bg); color: var(--text); margin: 0 auto; max-width: 46rem;
       padding: 1.5rem 1rem 3rem; font: 16px/1.5 -apple-system, system-ui, sans-serif; }
h1 { font-size: 1.5rem; margin: 0; }
h2 { font-size: 1.1rem; margin: 2rem 0 0.5rem; padding-bottom: 0.25rem;
     border-bottom: 1px solid var(--line); }
.meta, .date { color: var(--muted); }
.date { font-size: 0.85rem; margin-left: 0.4rem; white-space: nowrap; }
ul { padding-left: 1.2rem; margin: 0; }
li { margin: 0.35rem 0; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
summary { cursor: pointer; }
details ul { margin-top: 0.25rem; }
"""
