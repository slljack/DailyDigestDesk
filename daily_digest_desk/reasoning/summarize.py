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
    """No usable summary: the model refused, its answer was cut off, or it failed
    the checks even after the retry.

    It carries the tokens spent getting there, since a failed call still costs
    money and the run's cost cap has to count it.
    """

    def __init__(self, message: str, input_tokens: int = 0, output_tokens: int = 0):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


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


def _tokens(usage) -> tuple[int, int]:
    """Input and output tokens for every attempt in one call.

    If the first model declined and a fallback model answered, top-level usage
    covers only the fallback's attempt; usage.iterations lists every attempt,
    the declined one included. Count them all: the cap should never undercount.
    """
    attempts = getattr(usage, "iterations", None) or [usage]
    return sum(a.input_tokens for a in attempts), sum(a.output_tokens for a in attempts)


def summarize(
    event: Event, documents: dict[str, str], client, feedback: str | None = None,
) -> SummaryResult:
    """Ask Claude for a Summary of one event's filing, with the model and token counts.

    `documents` is trim_documents' output. `client` is an anthropic.Anthropic();
    it's passed in so tests can hand in a fake one and never call the real API.
    `feedback`, for a retry, is added after the filing (see _retry_note).
    Raises SummaryError if the model refused or ran out of room. The quotes
    aren't checked here: summarize_checked does that.
    """
    content = _message_text(event, documents)
    if feedback:
        content += "\n\n" + feedback
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16_000,  # room for the model's thinking plus the short answer
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_format=Summary,  # the API makes the answer fit this shape
        output_config={"effort": EFFORT},
        # If the model declines (its safety checks can misfire on harmless text),
        # the API retries on a fallback model in the same call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    # Counted first: a call that ends in an error below still cost this much.
    input_tokens, output_tokens = _tokens(response.usage)

    # Check why it stopped before reading the answer: a refused or cut-off
    # answer may not fit the Summary shape at all.
    if response.stop_reason == "refusal":
        message = f"Model refused to summarize {event.id}"
    elif response.stop_reason == "max_tokens":
        message = f"Answer was cut off for {event.id}"
    elif response.parsed_output is None:
        message = f"No summary in the answer for {event.id} ({response.stop_reason})"
    else:
        return SummaryResult(
            summary=response.parsed_output,
            model=response.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    raise SummaryError(message, input_tokens, output_tokens)


def _retry_note(summary: Summary, problems: list[str]) -> str:
    """What the retry adds after the filing: the failed answer and its problems.

    The model remembers nothing between calls, so the retry is a fresh request:
    the same filing again, plus this note.
    """
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        "Your previous answer, below, failed the code checks. Write a new answer "
        "that fixes every problem listed. Copy quotes exactly from the documents "
        "above, and keep only numbers that are in your quotes.\n\n"
        f"<previous_answer>\n{summary.model_dump_json(indent=2)}\n</previous_answer>\n\n"
        f"<problems>\n{listed}\n</problems>"
    )


def summarize_checked(event: Event, documents: dict[str, str], client) -> SummaryResult:
    """A Summary that passed check_summary, retrying once with the problems.

    The tokens returned (or carried by a SummaryError) cover every call made,
    so the summaries table and the cost cap see what this filing really cost.
    Raises SummaryError if a call gives no usable answer, or if the retry
    still fails the checks. A refusal or cut-off isn't retried: the API has
    already tried the fallback model, and the same filing would be cut off again.
    """
    first = summarize(event, documents, client)
    problems = check_summary(first.summary, documents)
    if not problems:
        return first

    try:
        second = summarize(event, documents, client, feedback=_retry_note(first.summary, problems))
    except SummaryError as exc:
        exc.input_tokens += first.input_tokens
        exc.output_tokens += first.output_tokens
        raise
    input_tokens = first.input_tokens + second.input_tokens
    output_tokens = first.output_tokens + second.output_tokens

    problems = check_summary(second.summary, documents)
    if problems:
        raise SummaryError(
            f"Summary for {event.id} failed its checks twice: " + "; ".join(problems),
            input_tokens, output_tokens,
        )
    return SummaryResult(second.summary, second.model, input_tokens, output_tokens)
