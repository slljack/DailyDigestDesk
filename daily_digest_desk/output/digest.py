"""Build the digest page: events in, one HTML page out.

No store, no clock, and no files except this package's own: explainers.toml,
glossary.toml and style.css. collect.write_digest reads the events and writes
the file; this module only turns them into text. The explainer cards are in
explainers.py, the hover tips in glossary.py.
"""

from __future__ import annotations

import html
import re
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime
from importlib import resources
from zoneinfo import ZoneInfo

from daily_digest_desk.forms import PROSPECTUS_FORMS, SCHEDULE_13D_FORMS, SCHEDULE_13G_FORMS
from daily_digest_desk.models import Event
from daily_digest_desk.output.explainers import (
    Explainers, explainer_cards, info_button, load_explainers,
)
from daily_digest_desk.output.glossary import NO_GLOSSARY, Glossary, load_glossary
from daily_digest_desk.reasoning.summarize import KeyNumber, Summary
from daily_digest_desk.red_flags import is_red_flag

_NEW_YORK = ZoneInfo("America/New_York")

# Light by default, dark when the system is. Kept small: one column, readable type.
_STYLE = resources.files(__package__).joinpath("style.css").read_text(encoding="utf-8")



def mark_key_numbers(escaped: str, key_numbers: Sequence[KeyNumber]) -> tuple[str, list[KeyNumber]]:
    """`escaped` (a summary, already escaped) with each key number wrapped in a tip
    that shows where it came from: 'EX-99.2: "Revenue for the third quarter was..."'.

    Returns the marked text and the key numbers it couldn't find in it, so the page
    can still show those. If a number is listed twice, the first time it appears in
    the text gets the first one's quote, the second time the second's. A key number
    with no text is skipped: there is nothing to show.
    """
    # Each number as it looks in escaped text ("S&P" is "S&amp;P" there), with the
    # key numbers waiting for it, in order.
    waiting: dict[str, deque[KeyNumber]] = {}
    for k in key_numbers:
        if k.number.strip():
            waiting.setdefault(html.escape(k.number), deque()).append(k)
    if not waiting:
        return escaped, []

    # Longest first, so "$550 million" is tried before "550" at the same spot.
    # Not inside a longer number: no letter, digit, "." or "," just before ("2%" in
    # "12%", "200" in "1,200"), and no letter or digit, or "." or "," with a digit,
    # just after ("2.70" in "2.705", "1,2" in "1,234"). A sentence's own "." is fine.
    numbers = sorted(waiting, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![\w.,])(?:" + "|".join(map(re.escape, numbers)) + r")(?![\w]|[.,]\d)"
    )

    def wrap(m: re.Match) -> str:
        queue = waiting[m.group(0)]
        if not queue:
            return m.group(0)  # a number more times in the text than in the list
        k = queue.popleft()
        quote = " ".join(k.quote.split())  # quotes keep the filing's line breaks
        tip = html.escape(f"{k.source}: \u201c{quote}\u201d")
        # "term" for the tip itself (style.css), "keynum" for how it looks. tabindex:
        # a tap or the Tab key shows it too, as with glossary words.
        return f'<span class="term keynum" tabindex="0" data-tip="{tip}">{m.group(0)}</span>'

    marked = pattern.sub(wrap, escaped)
    not_found = [k for queue in waiting.values() for k in queue]
    return marked, not_found


def _item(
    event: Event,
    explainers: Explainers | None = None,
    glossary: Glossary = NO_GLOSSARY,
    summary: Summary | None = None,
) -> str:
    """One event as an <li> line: the title (a link if the url is http/https), its day,
    and an ⓘ button that opens its explainer card, if it has one. Glossary words in
    the title get a hover tip, once each. If the filing has a summary, it goes
    under the title, tagged as written by a model, with a tip on each key number."""
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

    below = _summary_html(summary) if summary else ""

    if is_red_flag(event):
        return (
            f'<li class="flag"><span class="flag-mark term" tabindex="0" data-tip="{_RED_FLAG_TIP}">'
            f'⚠️ Red flag</span> {title} <span class="date">{day}</span>{info}{below}</li>'
        )
    return f'<li>{title} <span class="date">{day}</span>{info}{below}</li>'



def _summary_html(summary: Summary) -> str:
    """The summary paragraph: the model's sentences, each key number with a tip
    showing its quote, then its "key detail" bullets, if any, marked the same way.
    A key number neither contains goes at the end of the paragraph, after "Also in
    the filing:", so no checked number is hidden."""
    # The model's text is outside input too, like a title: escape it, then mark.
    text, left = mark_key_numbers(html.escape(summary.summary), summary.key_numbers)
    # The sentences first, then each bullet in turn, with the key numbers not used
    # yet: a key number gets one tip, wherever it first appears.
    bullets = []
    for change in summary.changes:
        if change.strip():
            marked, left = mark_key_numbers(html.escape(change), left)
            bullets.append(f"<li>{marked}</li>")
    if left:
        # Each one marked on its own: the number is its whole text, so it's found.
        also = ", ".join(mark_key_numbers(html.escape(k.number), [k])[0] for k in left)
        text += f' <span class="also">Also in the filing: {also}</span>'
    page = f'\n<p class="summary"><span class="ai-tag">AI summary</span> {text}</p>'
    if bullets:
        page += (
            '\n<div class="changes"><span class="changes-tag">Key details</span>'
            f'<ul>{"".join(bullets)}</ul></div>'
        )
    return page


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
    summaries: Mapping[str, Summary] | None = None,
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

    summaries = summaries or {}

    def item(e: Event) -> str:
        return _item(e, explainers, glossary, summaries.get(e.id))

    lines = []
    folded: set[str] = set()
    for event in newest_first:
        group = noisy.get(_label(event), [])
        if len(group) < 2:
            lines.append(item(event))
        elif _label(event) not in folded:
            # The whole group goes where its newest event would have been.
            folded.add(_label(event))
            items = "\n".join(item(e) for e in group)
            lines.append(
                f"<li><details><summary>{html.escape(_label(event))} ({len(group)})</summary>\n"
                f"<ul>\n{items}\n</ul></details></li>"
            )

    body = "\n".join(lines)
    return f"<section>\n<h2>{html.escape(heading)}</h2>\n<ul>\n{body}\n</ul>\n</section>"


# The sources that fetch from outside, as the page names them. Only these: if one
# failed, the page may be missing news. The other error keys of collect.run
# (signals, summaries, digest) are about this run's own work, not what's out there.
_FETCH_SOURCES = {
    "edgar": "SEC filings",
    "fred": "FRED",
    "finnhub_quotes": "Finnhub prices",
    "finnhub_earnings": "Finnhub earnings dates",
}


def _failed_html(errors: Mapping[str, Mapping[str, str]]) -> str:
    """A warning naming each source that failed and for what, or "" if none did.

    Names only, never the messages: an error can carry a request's url, API key and all.
    """
    parts = [
        f"{label} ({', '.join(errors[source])})"
        for source, label in _FETCH_SOURCES.items()
        if errors.get(source)
    ]
    if not parts:
        return ""
    return (
        f'<p class="failed">⚠️ Couldn\'t check: {html.escape("; ".join(parts))}. '
        "This page may be missing news from them.</p>"
    )


def build_digest(
    events: list[Event],
    now: datetime,
    tickers: list[str],
    explainers: Explainers | None = None,
    glossary: Mapping[str, Mapping] | None = None,
    summaries: Mapping[str, Summary] | None = None,
    errors: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """The whole page: Macro & Market first, then each holding in holdings order,
    then a hidden explainer card for each type on the page.

    A holding with nothing new is left out. Events for a ticker no longer in
    `tickers` (a holding you removed) still show, after the holdings, A to Z.
    `now` is only for the header; the caller decides which events are new.
    `explainers` and `glossary` default to the package's .toml files; tests pass their own.
    `summaries` maps an event id to its summary (store.summaries_for); an event
    missing from it just has no summary.
    `errors` is this run's errors by source (collect.run); a fetch source that
    failed gets a warning under the header.
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
        sections.append(_section("Macro & Market", by_ticker[None], explainers, terms, summaries))
    removed = sorted(t for t in by_ticker if t is not None and t not in tickers)
    for ticker in [*tickers, *removed]:
        if ticker in by_ticker:
            sections.append(_section(ticker, by_ticker[ticker], explainers, terms, summaries))

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
{_failed_html(errors or {})}
</header>
{body}
</body>
</html>
"""
