"""Build the digest page: events in, one HTML page out.

No store, no clock, and no files except this package's own explainers.toml and
glossary.toml.
collect.write_digest reads the events and writes the file; this module only
turns them into text.
"""

from __future__ import annotations

import functools
import html
import re
import tomllib
from collections.abc import Mapping
from datetime import datetime
from importlib import resources
from zoneinfo import ZoneInfo

from daily_digest_desk.models import Event

_NEW_YORK = ZoneInfo("America/New_York")

# SEC renamed the forms in 2024 ("SC 13D" -> "SCHEDULE 13D"); both show up.
_13D_FORMS = ("SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A")
_13G_FORMS = ("SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A")

Explainers = Mapping[str, Mapping[str, str]]  # key -> {"name", "what", "why", "look_for"}


@functools.cache
def load_explainers() -> dict[str, dict[str, str]]:
    """The explainers in explainers.toml, in file order. Read once per process."""
    text = resources.files(__package__).joinpath("explainers.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


@functools.cache
def load_glossary() -> dict[str, dict]:
    """The glossary in glossary.toml. Read once per process."""
    text = resources.files(__package__).joinpath("glossary.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


class Glossary:
    """Finds glossary words in already-escaped HTML text and wraps them in a hover tip.

    Matches whole words, any case, longest first ("shelf registration" before
    "shelf"). Works on escaped text, so the words are escaped the same way before
    matching: "S&P 500" is looked for as "S&amp;P 500".
    """

    def __init__(self, entries: Mapping[str, Mapping]):
        self._key_by_word: dict[str, str] = {}
        for key, entry in entries.items():
            for word in entry["words"]:
                self._key_by_word[html.escape(word).lower()] = key
        self._definitions = {key: entry["definition"] for key, entry in entries.items()}
        words = sorted(self._key_by_word, key=len, reverse=True)
        # Not after a letter, digit, "-" or "&" (so never inside "&amp;"), and not
        # before a letter, digit or "-": "EPS" doesn't match inside "STEPS" or "EPS-1".
        self._pattern = re.compile(
            r"(?<![\w&-])(?:" + "|".join(map(re.escape, words)) + r")(?![\w-])",
            re.IGNORECASE,
        ) if words else None

    def keys_in(self, escaped: str) -> set[str]:
        """The glossary entries mentioned in this text."""
        if self._pattern is None:
            return set()
        return {self._key_by_word[m.group(0).lower()] for m in self._pattern.finditer(escaped)}

    def mark(self, escaped: str, seen: set[str]) -> str:
        """`escaped` with the first mention of each entry not in `seen` wrapped in a tip.
        Adds what it marks to `seen`, so a caller can share it across several texts."""
        if self._pattern is None:
            return escaped

        def wrap(m: re.Match) -> str:
            key = self._key_by_word[m.group(0).lower()]
            if key in seen:
                return m.group(0)
            seen.add(key)
            # tabindex: a tap (phone) or Tab key focuses it, which shows the tip too.
            definition = html.escape(self._definitions[key])
            return f'<span class="term" tabindex="0" data-tip="{definition}">{m.group(0)}</span>'

        return self._pattern.sub(wrap, escaped)


_NO_GLOSSARY = Glossary({})


# Which explainer each kind of event uses. Big moves are one kind but four different
# things (a VIX jump isn't an NVDA drop), so explainer_key picks those by series.
_EXPLAINER_BY_KIND = {
    "8-K": "8-K", "8-K/A": "8-K",
    "4": "form-4",
    **{form: "13d" for form in _13D_FORMS},
    **{form: "13g" for form in _13G_FORMS},
    "10-Q": "10-Q",
    "10-K": "10-K",
    "S-3": "S-3",
    "earnings_headsup": "earnings-headsup",
    "earnings_result": "earnings-result",
    "CPIAUCSL": "cpi",
    "PAYEMS": "jobs",
}
_EXPLAINER_BY_SERIES = {
    "fred:DGS10": "10-year-yield",
    "fred:DFF": "fed-funds",
    "fred:VIXCLS": "vix",
}


def explainer_key(event: Event) -> str | None:
    """The explainer for this event, or None if it has none."""
    if event.kind == "big_move":
        series = event.fields.get("series", "")
        if series.startswith("finnhub:"):
            return "stock-big-move"
        return _EXPLAINER_BY_SERIES.get(series)
    return _EXPLAINER_BY_KIND.get(event.kind)


def _item(
    event: Event,
    explainers: Explainers | None = None,
    glossary: Glossary = _NO_GLOSSARY,
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

    info = ""
    key = explainer_key(event)
    if explainers and key in explainers:
        name = html.escape(explainers[key]["name"])
        # popovertarget: clicking opens the card with that id (HTML's built-in popover).
        info = (
            f' <button type="button" class="info" popovertarget="explain-{html.escape(key)}"'
            f' title="What is this? {name}" aria-label="What is this? {name}">ⓘ</button>'
        )

    return f'<li>{title} <span class="date">{day}</span>{info}</li>'


# What a folded group is called, by event kind. Several kinds can share a label, so
# they fold together: an 8-K and its amendment are both "8-K filings". A kind not
# listed here folds under its own name.
_GROUP_LABELS = {
    "8-K": "8-K filings", "8-K/A": "8-K filings",
    "4": "Insider transactions",
    **{form: "Big-holder stake filings" for form in _13D_FORMS + _13G_FORMS},
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


def _section(
    heading: str,
    events: list[Event],
    explainers: Explainers | None = None,
    glossary: Glossary = _NO_GLOSSARY,
) -> str:
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
        body = "\n".join([*sections, _explainer_cards(events, explainers, terms)])
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


def _explainer_cards(
    events: list[Event],
    explainers: Explainers,
    glossary: Glossary = _NO_GLOSSARY,
) -> str:
    """A hidden card per explainer the page's events use, once each, in file order.

    Each card is a popover: hidden until an ⓘ with its id is clicked, then shown over
    the page. Esc, a click outside, or the ✕ closes it. No JavaScript needed.
    Glossary words in the card get a tip, once per card, except the card's own
    subject: the CPI card doesn't need a tip explaining CPI."""
    used = {explainer_key(e) for e in events}
    blocks = []
    for key, entry in explainers.items():
        if key not in used:
            continue
        name = html.escape(entry["name"])
        seen = glossary.keys_in(name)  # the card's subject: no tip for it

        def text(field: str) -> str:
            return glossary.mark(html.escape(entry[field]), seen)

        parts = [
            f'<div class="explainer" id="explain-{html.escape(key)}" popover>',
            f'<button type="button" class="close" popovertarget="explain-{html.escape(key)}"'
            ' popovertargetaction="hide" aria-label="Close">✕</button>',
            f"<h3>{name}</h3>",
            f"<p><strong>What it is.</strong> {text('what')}</p>",
            f"<p><strong>Why stocks care.</strong> {text('why')}</p>",
        ]
        if entry.get("look_for"):
            parts.append(f"<p><strong>What to look for.</strong> {text('look_for')}</p>")
        parts.append("</div>")
        blocks.append("\n".join(parts))
    return "\n".join(blocks)


# Light by default, dark when the system is. Kept small: one column, readable type.
_STYLE = """
:root { --bg: #ffffff; --text: #1f2328; --muted: #6e7781; --link: #0969da; --line: #d8dee4;
        --tip-bg: #1f2328; --tip-text: #ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --text: #e6edf3; --muted: #8d96a0; --link: #4493f8; --line: #30363d;
          --tip-bg: #e6edf3; --tip-text: #0d1117; }
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
button.info { background: none; border: none; padding: 0 0.2rem; margin-left: 0.2rem;
              color: var(--muted); font-size: 0.9rem; cursor: pointer; }
button.info:hover { color: var(--link); }
/* An explainer card: centred over the page while open (the popover default). */
.explainer { max-width: min(28rem, calc(100vw - 2rem)); padding: 1rem 1.25rem;
             background: var(--bg); color: var(--text); border: 1px solid var(--line);
             border-radius: 10px; box-shadow: 0 8px 30px rgba(0, 0, 0, 0.25); }
.explainer::backdrop { background: rgba(0, 0, 0, 0.3); }
.explainer h3 { font-size: 1rem; margin: 0 1.5rem 0.5rem 0; }
.explainer p { margin: 0.4rem 0; }
.close { position: absolute; top: 0.6rem; right: 0.7rem; background: none; border: none;
         color: var(--muted); font-size: 1rem; cursor: pointer; }
.close:hover { color: var(--text); }
details ul { margin-top: 0.25rem; }
/* A glossary word: dotted underline; hover, tap or Tab shows its definition below it. */
.term { text-decoration: underline dotted; text-underline-offset: 3px; cursor: help;
        position: relative; }
.term:hover::after, .term:focus::after {
  content: attr(data-tip); position: absolute; left: 0; top: calc(100% + 4px); z-index: 10;
  width: max-content; max-width: min(18rem, 80vw); white-space: normal; pointer-events: none;
  padding: 0.4rem 0.6rem; border-radius: 6px; background: var(--tip-bg); color: var(--tip-text);
  font-size: 0.8rem; font-weight: normal; line-height: 1.35; text-align: left;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.2); }
.term:focus { outline: none; }
"""
