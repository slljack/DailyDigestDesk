"""Explainers: a short card per type of item on the digest page, opened by its ⓘ.

The text lives in explainers.toml, written once and reviewed. This module picks
the entry for each event, makes the ⓘ button next to a line, and makes the
hidden cards the buttons open.
"""

from __future__ import annotations

import functools
import html
import tomllib
from collections.abc import Mapping
from importlib import resources

from daily_digest_desk.forms import PROSPECTUS_FORMS, SCHEDULE_13D_FORMS, SCHEDULE_13G_FORMS
from daily_digest_desk.models import Event
from daily_digest_desk.output.glossary import NO_GLOSSARY, Glossary

Explainers = Mapping[str, Mapping[str, str]]  # key -> {"name", "what", "why", "look_for"}


@functools.cache
def load_explainers() -> dict[str, dict[str, str]]:
    """The explainers in explainers.toml, in file order. Read once per process."""
    text = resources.files(__package__).joinpath("explainers.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


# Which explainer each kind of event uses. Big moves are one kind but four different
# things (a VIX jump isn't an NVDA drop), so explainer_key picks those by series.
_EXPLAINER_BY_KIND = {
    "8-K": "8-K", "8-K/A": "8-K",
    "6-K": "6-K", "6-K/A": "6-K",
    "4": "form-4",
    **{form: "13d" for form in SCHEDULE_13D_FORMS},
    **{form: "13g" for form in SCHEDULE_13G_FORMS},
    "10-Q": "10-Q",
    "10-K": "10-K",
    "S-3": "S-3", "S-3ASR": "S-3",
    **{form: "424B" for form in PROSPECTUS_FORMS},
    "earnings_headsup": "earnings-headsup",
    "earnings_result": "earnings-result",
    "CPIAUCSL": "cpi",
    "PAYEMS": "jobs",
}
_EXPLAINER_BY_SERIES = {
    "fred:DGS10": "10-year-yield",
    "fred:DFF": "fed-funds",
    "fred:VIXCLS": "vix",
    "finnhub:SPY": "sp500-move",  # the benchmark: before the finnhub: catch-all below
}


def explainer_key(event: Event) -> str | None:
    """The explainer for this event, or None if it has none."""
    if event.kind == "big_move":
        series = event.fields.get("series", "")
        if series in _EXPLAINER_BY_SERIES:
            return _EXPLAINER_BY_SERIES[series]
        if series.startswith("finnhub:"):
            return "stock-big-move"  # any other Finnhub price is a holding
        return None
    return _EXPLAINER_BY_KIND.get(event.kind)


def info_button(event: Event, explainers: Explainers | None) -> str:
    """The ⓘ that opens this event's explainer card, or "" if it has none."""
    key = explainer_key(event)
    if not explainers or key not in explainers:
        return ""
    name = html.escape(explainers[key]["name"])
    # popovertarget: clicking opens the card with that id (HTML's built-in popover).
    return (
        f' <button type="button" class="info" popovertarget="explain-{html.escape(key)}"'
        f' title="What is this? {name}" aria-label="What is this? {name}">ⓘ</button>'
    )


def explainer_cards(
    events: list[Event],
    explainers: Explainers,
    glossary: Glossary = NO_GLOSSARY,
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
