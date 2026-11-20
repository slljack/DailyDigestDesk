"""A plain-English summary of an event's filing, written by Claude (DESIGN.md section 7).

The model fills in a Summary: a few sentences, plus the important numbers, each
with the exact sentence it came from. Code then checks those quotes against the
filing, so a made-up number can't reach the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from daily_digest_desk.models import Event

_NEW_YORK = ZoneInfo("America/New_York")

MODEL = "claude-opus-5-5"
EFFORT = "medium"  # how hard the model thinks; a starting guess, to be checked on the eval

# The size check. The prompt aims for about 60 words; this limit only catches a
# broken answer (rambling, half the filing pasted in), so an accurate summary
# that runs a little long is never dropped.
MAX_WORDS = 150
MAX_KEY_NUMBERS = 5

# Bump the version whenever the prompt changes, so each saved summary records
# which prompt wrote it.
PROMPT_VERSION = 1
SYSTEM_PROMPT = """\
You summarize SEC filings for a beginner investor who owns this stock.
They read your summary instead of the filing, so accuracy matters more than style.

Write 2-3 plain-English sentences (at most about 60 words) saying what the
filing actually reports: what happened, with the numbers that matter. If a
finance term is unavoidable, explain it in a few words the first time.

Use only the documents given. If something isn't in them, leave it out;
don't add background you know from elsewhere. Every quote you give is
checked by code against the filing, and a summary with a quote that isn't
found word for word is thrown away. So for each important number (up to 5),
copy the exact sentence it appears in, without changing a character, and
give the type of the document it's in. Every number in your summary, dates
included, must appear in one of those quotes. Describe a reporting period in
words ("the third quarter"); a date that matters on its own, like when debt
is due, should be quoted like a key number.

Inform, don't advise: no buy/sell/hold, no price predictions, no "good" or
"bad news" judgments. The reader decides that against their own reasons for
owning the stock. Forecasts appear only as the company's own words
("the company expects...").

If the filing is routine paperwork with nothing new about the business,
say so in one sentence.
"""


class SummaryError(Exception):
    """The model gave no usable summary: it refused, or its answer was cut off."""


class KeyNumber(BaseModel):
    """One important number, with where it came from, so code can check it."""

    number: str  # as written in the filing ("$57.0 billion"), so it can be found in the quote
    quote: str   # the exact sentence it appears in, copied word for word
    source: str  # the document it's in: "8-K", "EX-99.1", ...


class Summary(BaseModel):
    """What the model gives back for one filing."""

    summary: str                  # 2-3 plain-English sentences
    key_numbers: list[KeyNumber]  # up to 5; empty for a routine filing with no numbers


@dataclass
class SummaryResult:
    """A Summary, plus which model wrote it and what it cost, for the summaries table.

    Not a pydantic model like Summary: the API never sees this shape, so it needs
    no schema. It's a plain box for four values.
    """

    summary: Summary
    model: str          # response.model: the fallback model's name if it answered instead
    input_tokens: int
    output_tokens: int  # thinking included: it's billed as output


# A number as written in text: 62, 57.0, 1,200, 1,234.56. Not one glued to letters
# or a hyphen, like 8-K, Q3, 10-Q or EX-99.1: those are labels, not amounts.
_NUMBER = re.compile(r"(?<![\w.-])\d+(?:,\d{3})*(?:\.\d+)?(?![\w-])")


def _normalize(text: str) -> str:
    """Every run of spaces, tabs and line breaks becomes one space, so a quote
    copied as one clean line still matches a filing that breaks it across lines.

    Example: _normalize("up 62%\\n from  a year ago ") gives "up 62% from a year ago".
    """
    return " ".join(text.split())


def _message_text(event: Event, documents: dict[str, str]) -> str:
    """What the model reads: one line saying which filing this is, then each
    document wrapped in a tag with its type, so the model can name its source.

    Example, for an 8-K with one exhibit:

        NVDA 8-K, filed 2026-11-19: NVIDIA 8-K (2.02 Results of Operations)

        <document type="8-K">
        ...
        </document>

        <document type="EX-99.1">
        ...
        </document>
    """
    # New York's date, like the page: a filing at 9pm New York is already the next day in UTC.
    filed = event.occurred_at.astimezone(_NEW_YORK).date()
    parts = [f"{event.ticker} {event.kind}, filed {filed}: {event.title}"]
    for doc_type, text in documents.items():
        parts.append(f'<document type="{doc_type}">\n{text}\n</document>')
    return "\n\n".join(parts)


def check_summary(summary: Summary, documents: dict[str, str]) -> list[str]:
    """The problems with a summary, as messages. Empty means it passed.

    For each key number: its source must be one of the documents, its quote must
    be in that document word for word (only spacing is forgiven), and the numbers
    in it must be in its quote. And every number in the summary sentences must be in
    one of the quotes, so a number can't skip the checks by not being listed.
    Also: at most MAX_WORDS words and MAX_KEY_NUMBERS key numbers.
    """
    problems = []

    # The size check: the API can't limit lengths while the model writes, so code does.
    words = len(summary.summary.split())
    if words == 0:
        problems.append("summary: empty")
    if words > MAX_WORDS:
        problems.append(f"summary: {words} words, more than {MAX_WORDS}")
    if len(summary.key_numbers) > MAX_KEY_NUMBERS:
        problems.append(f"{len(summary.key_numbers)} key numbers, more than {MAX_KEY_NUMBERS}")

    # Each document cleaned once, not once per key number that quotes it.
    clean = {doc_type: _normalize(text) for doc_type, text in documents.items()}

    for kn in summary.key_numbers:
        if kn.source not in clean:
            problems.append(f"{kn.source}: no such document (number {kn.number!r})")
            continue  # nothing to search the quote in

        if _normalize(kn.quote) not in clean[kn.source]:
            problems.append(f"{kn.source}: quote not found: {kn.quote!r}")

        # Compare the numbers, not the text: "$980.8 million (571%)" passes if
        # both 980.8 and 571 are in the quote, however the model wrote them.
        numbers = _NUMBER.findall(kn.number)
        if not numbers:
            problems.append(f"{kn.source}: {kn.number!r} has no number in it")
        elif not set(numbers) <= set(_NUMBER.findall(kn.quote)):
            problems.append(f"{kn.source}: {kn.number!r} is not in its quote: {kn.quote!r}")

    # The summary-numbers check. Compare whole numbers, not text: "2" is inside
    # "62" as text, but it isn't the same number.
    quoted = set(_NUMBER.findall(" ".join(kn.quote for kn in summary.key_numbers)))
    for number in _NUMBER.findall(summary.summary):
        if number not in quoted:
            problems.append(f"summary: {number!r} is not in any quote")
    return problems


def summarize(event: Event, documents: dict[str, str], client) -> SummaryResult:
    """Ask Claude for a Summary of one event's filing, with the model and token counts.

    `documents` is trim_documents' output. `client` is an anthropic.Anthropic();
    it's passed in so tests can hand in a fake one and never call the real API.
    Raises SummaryError if the model refused or ran out of room. The quotes
    aren't checked here: that's the caller's next step.
    """
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16_000,  # room for the model's thinking plus the short answer
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _message_text(event, documents)}],
        output_format=Summary,  # the API makes the answer fit this shape
        output_config={"effort": EFFORT},
        # If the model declines (its safety checks can misfire on harmless text),
        # the API retries on a fallback model in the same call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    # Check why it stopped before reading the answer: a refused or cut-off
    # answer may not fit the Summary shape at all.
    if response.stop_reason == "refusal":
        raise SummaryError(f"Model refused to summarize {event.id}")
    if response.stop_reason == "max_tokens":
        raise SummaryError(f"Answer was cut off for {event.id}")
    if response.parsed_output is None:
        raise SummaryError(f"No summary in the answer for {event.id} ({response.stop_reason})")
    return SummaryResult(
        summary=response.parsed_output,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
