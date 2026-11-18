"""Build the digest page: events in, one HTML page out.

No store, no clock, and no files except this package's own: explainers.toml,
glossary.toml and style.css. collect.write_digest reads the events and writes
the file; this module only turns them into text. The explainer cards are in
explainers.py, the hover tips in glossary.py.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from datetime import datetime
from importlib import resources
from zoneinfo import ZoneInfo

from daily_digest_desk.forms import PROSPECTUS_FORMS, SCHEDULE_13D_FORMS, SCHEDULE_13G_FORMS
from daily_digest_desk.models import Event
from daily_digest_desk.output.explainers import (
    Explainers, explainer_cards, info_button, load_explainers,
)
from daily_digest_desk.output.glossary import NO_GLOSSARY, Glossary, load_glossary
from daily_digest_desk.red_flags import is_red_flag

_NEW_YORK = ZoneInfo("America/New_York")

# Light by default, dark when the system is. Kept small: one column, readable type.
_STYLE = resources.files(__package__).joinpath("style.css").read_text(encoding="utf-8")


def _item(
    event: Event,
    explainers: Explainers | None = None,
    glossary: Glossary = NO_GLOSSARY,
) -> str:
    """One event as an <li> line: the title (a link if the url is http/https), its day,
    and an ⓘ button that opens its explainer card, if it has one. Glossary words in
    the title get a hover tip, once each."""
    # Titles and urls come from outside sources: escape them, so a "<script>" in a
    # title shows up as text instead of running in the browser. Escape first, then
    # mark glossary words: the tips are our own HTML and must not be escaped.
    title = glossary.mark(html.escape(event.title), seen=set())

    # New York first, then the day: a filing at 9pm New York is 2am UTC the next day.
    # .day, not %d, so it's "Nov 6", not "Nov 06".
    local = event.occurred_at.astimezone(_NEW_YORK)
    day = f"{local:%b} {local.day}"

    # Escaping can't make "javascript:..." safe to click, so only web links become links.
    if event.url.startswith(("http://", "https://")):
        title = f'<a href="{html.escape(event.url)}">{title}</a>'

    info = info_button(event, explainers)

    if is_red_flag(event):
        return (
            f'<li class="flag"><span class="flag-mark term" tabindex="0" data-tip="{_RED_FLAG_TIP}">'
            f'⚠️ Red flag</span> {title} <span class="date">{day}</span>{info}</li>'
        )
    return f'<li>{title} <span class="date">{day}</span>{info}</li>'


_RED_FLAG_TIP = html.escape(
    "Rare and usually bad news, like the company saying its past numbers were wrong. "
    "Not a reason to sell on its own: read this one today."
)


# What a folded group is called, by event kind. Several kinds can share a label, so
# they fold together: an 8-K and its amendment are both "8-K filings". A kind not
# listed here folds under its own name.
_GROUP_LABELS = {
    "8-K": "8-K filings", "8-K/A": "8-K filings",
    "6-K": "6-K filings", "6-K/A": "6-K filings",
    "4": "Insider transactions",
    **{form: "Big-holder stake filings" for form in SCHEDULE_13D_FORMS + SCHEDULE_13G_FORMS},
    "10-Q": "Quarterly reports",
    "10-K": "Annual reports",
    **{form: "Share offering filings" for form in ("S-3", "S-3ASR", *PROSPECTUS_FORMS)},
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


def _section(
    heading: str,
    events: list[Event],
    explainers: Explainers | None = None,
    glossary: Glossary = NO_GLOSSARY,
) -> str:
    """A heading and its events: red flags first, then the rest, each newest first.
    Two or more of a noisy label fold into one click-to-open line with a count:
    "Insider transactions (3)".
    """
    # Newest first; id breaks ties so the order is the same on every run. Then red
    # flags move to the top (sorted() is stable, so each part stays newest first).
    newest_first = sorted(events, key=lambda e: (e.occurred_at, e.id), reverse=True)
    newest_first = sorted(newest_first, key=lambda e: not is_red_flag(e))

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
            lines.append(_item(event, explainers, glossary))
        elif _label(event) not in folded:
            # The whole group goes where its newest event would have been.
            folded.add(_label(event))
            items = "\n".join(_item(e, explainers, glossary) for e in group)
            lines.append(
                f"<li><details><summary>{html.escape(_label(event))} ({len(group)})</summary>\n"
                f"<ul>\n{items}\n</ul></details></li>"
            )

    body = "\n".join(lines)
    return f"<section>\n<h2>{html.escape(heading)}</h2>\n<ul>\n{body}\n</ul>\n</section>"


def build_digest(
    events: list[Event],
    now: datetime,
    tickers: list[str],
    explainers: Explainers | None = None,
    glossary: Mapping[str, Mapping] | None = None,
) -> str:
    """The whole page: Macro & Market first, then each holding in holdings order,
    then a hidden explainer card for each type on the page.

    A holding with nothing new is left out. Events for a ticker no longer in
    `tickers` (a holding you removed) still show, after the holdings, A to Z.
    `now` is only for the header; the caller decides which events are new.
    `explainers` and `glossary` default to the package's .toml files; tests pass their own.
    """
    if explainers is None:
        explainers = load_explainers()
    terms = Glossary(load_glossary() if glossary is None else glossary)
    local = now.astimezone(_NEW_YORK)
    hour = local.hour % 12 or 12  # 0 -> 12am, 13 -> 1pm
    when = f"{local:%a %b} {local.day}, {hour}:{local:%M}{'am' if local.hour < 12 else 'pm'}"

    by_ticker: dict[str | None, list[Event]] = {}
    for event in events:
        by_ticker.setdefault(event.ticker, []).append(event)

    sections = []
    if None in by_ticker:  # no ticker: FRED releases and macro big moves
        sections.append(_section("Macro & Market", by_ticker[None], explainers, terms))
    removed = sorted(t for t in by_ticker if t is not None and t not in tickers)
    for ticker in [*tickers, *removed]:
        if ticker in by_ticker:
            sections.append(_section(ticker, by_ticker[ticker], explainers, terms))

    if events:
        count = f"{len(events)} new item{'s' if len(events) != 1 else ''}"
        flags = sum(is_red_flag(e) for e in events)
        if flags:
            # Plain text: run-digest.sh copies this line into the pop-up.
            count += f" · ⚠️ {flags} red flag{'s' if flags != 1 else ''}"
        body = "\n".join([*sections, explainer_cards(events, explainers, terms)])
    else:
        count = "Nothing new since the last digest."
        body = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Digest · {when}</title>
<style>
{_STYLE}</style>
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
