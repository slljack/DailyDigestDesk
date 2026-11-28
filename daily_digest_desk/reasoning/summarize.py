"""A plain-English summary of an event's filing, written by Claude (DESIGN.md section 7).

The model fills in a Summary: a few sentences, plus the important numbers, each
with the exact sentence it came from. Code then checks those quotes against the
filing, so a made-up number can't reach the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from daily_digest_desk.forms import PROSPECTUS_FORMS, SCHEDULE_13D_FORMS
from daily_digest_desk.ingestion.edgar_text import CUT_NOTE
from daily_digest_desk.models import Event

_NEW_YORK = ZoneInfo("America/New_York")

# The filings that get a summary: free text a beginner can't easily read
# (DESIGN.md section 7). Not Form 4 (structured numbers code can describe),
# 13G (a passive stake, nothing to explain), or 10-Q/10-K yet (they need their
# MD&A section found first). Signals, FRED and earnings events are already
# clear sentences.
SUMMARY_FORMS: tuple[str, ...] = (
    "8-K", "8-K/A",
    "6-K", "6-K/A",
    *SCHEDULE_13D_FORMS,
    "S-3", "S-3ASR",
    *PROSPECTUS_FORMS,
)

MODEL = "claude-opus-5-5"
EFFORT = "medium"  # how hard the model thinks; a starting guess, to be checked on the eval

# MODEL's price per million tokens. Thinking is billed as output. A fallback
# model may be priced differently, so dollars() is an estimate: fine for a cap.
INPUT_DOLLARS_PER_MTOK = 4.00
OUTPUT_DOLLARS_PER_MTOK = 20.00

# The size check. The prompt aims for about 60 words; this limit only catches a
# broken answer (rambling, half the filing pasted in), so an accurate summary
# that runs a little long is never dropped.
MAX_WORDS = 150
# 12, up from 5 in v2: the v2 eval's big filings (earnings, NBIS's letter) used all
# 5, and the "key detail" bullets need quotes of their own. Set roomy on purpose,
# with the checklists: a limit too high shows in the eval (cases use less), but a
# limit too tight hides what the model cut.
MAX_KEY_NUMBERS = 12
MAX_CHANGES = 6  # "key detail" bullets, for a big filing; most filings get none
# A bullet is one sentence; like MAX_WORDS, this catches a broken one, and the
# prompt doesn't state it. 60, up from 40: v3's 29 bullets ran 9-34 words, but a
# fine 44-word NVDA bullet failed the check, and its retry was lost to a typo.
MAX_CHANGE_WORDS = 60

# Bump the version whenever the prompt changes, so each saved summary records
# which prompt wrote it.
# 3: built from the Nov 22 eval notes; set early so live summaries written with
# any v3 text aren't labeled v2 (the scheduled job runs this folder's code).
# 4: v3 plus headings, the bullets rule moved up next to the summary, "short
# filings usually need none", shorter calendar-year periods. v3's eval was
# stopped half done (runs/v3), so v4 is the first of these to be graded.
PROMPT_VERSION = 4
# An f-string, so the limits it states can't fall out of step with the checks.
SYSTEM_PROMPT = f"""\
You summarize SEC filings for an investor who owns this stock.
They read your summary instead of the filing, so accuracy matters more than style.

## What to write

Write 2-3 plain-English sentences (at most about 60 words) saying what the
filing actually reports: what happened, with the numbers that matter.

A big filing may need more than the summary can hold. Then the summary keeps
the main news and any limit the filing puts on its number; the rest goes in up
to {MAX_CHANGES} short "key detail" bullets. Each bullet is one plain sentence
that doesn't repeat the summary, and follows every rule here, numbers and
conditions included. Short filings usually need none: leave the list empty
rather than fill it.

If the filing is routine paperwork with nothing new about the business, say so
in one sentence, with nothing else.

## What matters

Lead with what's new for someone who already owns the stock: what changed,
by the filing's own account, from what the company said or did before. That
could be a forecast, a plan, a deal, a person, shareholders' rights or votes,
or what the company reports. The longest part of a filing isn't always its
news, and in a short filing the news can be a single sentence, even one
saying that something didn't happen.

A large amount is only half the story without its limits. When you give the
size of a deal, a guarantee, a share sale or a forecast, include the
conditions the filing attaches to it: whether it's a maximum ("up to"),
whether it must happen at all, what it depends on, when it starts, and any
minimum or maximum price. Leave out a condition only if the filing gives none.

When the filing leaves out something a reader would ask about first, such as
why someone left or who replaces them, say the filing doesn't say, so the
reader knows it isn't in the filing rather than missing from your summary.
Give at most one such note. A document that ends with "{CUT_NOTE}"
wasn't sent in full: what you can't find may be in the part not sent, so
don't say the filing doesn't say it.

## Checklists

Before writing, decide what kind of filing this is and go through its checklist
below in your head. A filing can be more than one kind. A checklist is a list
of places to look, not a form to fill: report only what the filing has, and
never list a check as "not disclosed" or "none". The one exception is the note
above, about what a reader would ask first. If the filing has something important
that no check covers, include it too. Compare only with what the filing itself
says came before. Order the bullets by importance, not by checklist order.

Earnings release. When a forecast changed, it is often bigger news than the
quarter just reported: lead with it, and with whatever the filing says caused
it.
- a forecast raised, cut, moved within its range or newly given, with its
  conditions;
- a delay in capacity, a product, deliveries or a project, and the cause the
  filing gives;
- results against a year earlier, and what drove them;
- profit that moved differently from revenue, and the one-time items, gains or
  losses behind it, including revenue the filing says won't repeat;
- a gap between reported (GAAP) and adjusted results, and what the adjustment
  leaves out;
- operating cash flow far from profit, or receivables growing much faster than
  revenue;
- spending plans compared with what the company said before;
- cash, debt and large holdings at the end of the period; new debt, share
  sales or buybacks;
- a measure the company starts or stops reporting;
- doubt about continuing as a going concern, a material weakness in its
  controls, or a restatement.

Agreement or deal: who the other party is and what the deal covers; its value,
length and payment timing; its conditions, termination rights and options.

Financing (a loan, notes, a share sale): the amount and type, interest rate,
maturity, conversion price or share count, and what the money is for.

Shelf registration or prospectus: the amount, and whether it's only a maximum;
who sells, the company or existing holders, and who gets the money; the price,
or how it's set.

Big-holder stake (13D or 13G): the stake and how it changed; any hedge, pledge
or loan on the shares and its terms, like minimum and maximum prices and
dates; what the holder says it plans to do, beyond the usual statement that it
may buy or sell more.

Leadership change: who is leaving or joining, the role, the effective date,
and whether the filing gives a reason.

Auditor change or restatement: the change, and whether the filing reports any
disagreements; which past periods can no longer be relied on, and why.

Shareholder rights (a rights plan, a charter or voting change): what changes
for shareholders, when, and who approved it.

Anything else: what happened, the numbers that matter, and when it takes
effect.

## Where facts come from

Use only the documents given: if something isn't in them, leave it out.
Don't add background you know from elsewhere, and don't define finance terms.

For people and firms, give only the role the filing states ("a
director", "the chief financial officer"). If it states none, use just the
name; don't add a title, or a judgment of how important someone is, from
what you know.

## Quotes and numbers

Every quote you give is checked by code against the filing, and a summary
with a quote that isn't found word for word is thrown away. Every number you
write in the summary or bullets, dates included, must appear in one of your
quotes. So for each one, add a key number: copy the exact sentence (or table
line) it appears in, without changing a character, and give the type of the
document it's in. One quote covers every number in it: numbers from the same
sentence or line can share one key number ("$40.1 million; $12.3 million"),
and a year or date already inside another quote needs none of its own. You
have at most {MAX_KEY_NUMBERS} key numbers. If you'd need more, leave the least important
number out; never write a number without its quote. The one exception is the
filing date in the first line: it needs no quote.

In a key number, write only the number, the same way you write it in the
summary ("$4.2 billion", not "$4.2 billion 2028 revenue"). A key number must
contain digits. A number written in words ("ten years") needs no key number:
only digits are checked. A digit inside a name is checked too: leave out
Rule, Item, Section and Form numbers, and make sure a quote contains any
other ("Chapter 11", "Phase 3").

Copy each number as the filing writes it: don't round it or add numbers
together, or it won't match its quote. Give a change or percentage only if the
filing states it; otherwise give both figures. When a figure is adjusted
(non-GAAP), say so, as the filing labels it.

Name a reporting period as the filing does, with its fiscal year and end date
("the second quarter of fiscal 2019, which ended March 30, 2019"): many
companies' fiscal years don't match the calendar. If the fiscal year matches
the calendar, "the third quarter of 2024" is enough. Give the date of anything
that matters on its own, like when debt is due, and of anything the filing
says happened earlier, so old news doesn't read as new.

Leave out items the filing presents as regular and recurring, like a regular
quarterly dividend, unless it says the amount or timing changed. Results
(revenue, profit, earnings per share) are never routine.

## Tone

Inform, don't advise: no buy/sell/hold, no price predictions, no "good" or
"bad news" judgments. The reader decides that against their own reasons for
owning the stock. Forecasts appear only as the company's own words
("the company expects..."), no stronger or softer than the filing puts them.
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

    # The descriptions (and titles) are sent with the schema, so the model reads
    # them next to the prompt: they say what the prompt says, in the prompt's words.
    number: str = Field(description="Only the number, in digits, written the same way as in the summary.")
    quote: str = Field(description="The exact sentence (or table line) the number appears in, copied word for word.")
    source: str = Field(description='The type of the document the quote is from, as in its <document type="..."> tag.')


class Summary(BaseModel):
    """What the model gives back for one filing."""

    summary: str = Field(description="2-3 plain-English sentences, at most about 60 words.")
    key_numbers: list[KeyNumber] = Field(
        description=f"Up to {MAX_KEY_NUMBERS} key numbers; empty if the summary and bullets have none."
    )
    # Up to MAX_CHANGES "key detail" bullets, for a big filing. A default, so the
    # model may leave it out (no bullets) and summaries saved before v3 still load.
    # Stored as "changes" (v3's first name); the model sees the title "Key details",
    # so the name doesn't steer it toward changes only.
    changes: list[str] = Field(
        default_factory=list, title="Key details",
        description=f'Up to {MAX_CHANGES} "key detail" bullets, one plain sentence each. Empty for most short filings.',
    )


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


# A number as written in text: 62, 57.0, 1,200, 1,234.56, $12-13 billion, a 90-day
# period, $981M. Not a label: 8-K, 10-Q, EX-99.1, Q3, Series A-1, Rule 10b5-1.
# A hyphen alone doesn't make a label; what's on its other side does.
_DIGITS = r"\d+(?:,\d{3})*(?:\.\d+)?"
_END = r"(?!\w|[.,]\d)"  # the whole number: not 1 of 1.2B, not 1 of 1,2345
_NUMBER = re.compile(
    rf"""
    (?<![\w.])            # nothing glued on before: Q3, 3rd, the 1 of 99.1
    (?<![A-Z]-)           # not after an uppercase word and a hyphen: EX-99, S-3, A-1
    (?<!\d[a-z]-)         # not after a rule code and a hyphen: Rule 13a-16
    (?<![a-z]\d-)         #   ... Rule 10b5-1
    (?:
        (?<=\$) {_DIGITS} (?=(?:[MBKT]|bn)? {_END})   # after a $, may end in M, B, K, T or bn
      | {_DIGITS} {_END}
    )
    (?!-[A-Z])            # not before a hyphen and an uppercase letter: 8-K, 10-Q, 20-F
    """,
    re.VERBOSE,
)


def _value(number: str) -> Decimal:
    """A number _NUMBER found, as a value: commas and trailing zeros don't matter.

    Example: _value("7,843,779") == _value("7843779.00") is True, and so is
    _value("5.350") == _value("5.35"). A rounded number isn't: 12 is not 11.7.
    """
    return Decimal(number.replace(",", ""))


# A number and the scale word right after it: "$4.2 billion", "980.8 million",
# "$981M", "$1.2bn". Only a scale written next to the number: a table's "in
# millions" heading is too far away to know, so a table line has none. A glued
# letter (K, M, B, T) counts only right after a currency sign, so "Rule 10b-5"
# isn't 10 billion and "3M" isn't 3 million; "bn" counts anywhere ("1.2bn"). The
# sign is looked behind, not matched, so "US$4.2B" and "C$981M" count too.
_SCALED = re.compile(
    rf"""(?<![\w.])(?:
        [$€£]? (?P<a>{_DIGITS}) \s* (?P<w>thousand|million|billion|trillion) \b
      | (?<=[$€£]) (?P<b>{_DIGITS}) (?P<l>bn|[KMBT]) \b
      | (?P<c>{_DIGITS}) (?P<bn>bn) \b
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_SCALE_NAMES = {"k": "thousand", "m": "million", "b": "billion", "bn": "billion", "t": "trillion"}


def _scaled(text: str) -> list[tuple[Decimal, str, str]]:
    """Each number with a scale word in `text`: (value, scale, as written).

    Example: _scaled("$4.2 billion and $981M") gives
    [(4.2, "billion", "$4.2 billion"), (981, "million", "$981M")].
    """
    found = []
    for m in _SCALED.finditer(text):
        number = m.group("a") or m.group("b") or m.group("c")
        word = (m.group("w") or m.group("l") or m.group("bn")).lower()
        # As written, with a looked-behind sign put back: "$4.2B", not "4.2B".
        start = m.start() - (m.start() > 0 and text[m.start() - 1] in "$€£")
        found.append((_value(number), _SCALE_NAMES.get(word, word), text[start:m.end()].strip()))
    return found


_POWERS = {"thousand": 3, "million": 6, "billion": 9, "trillion": 12}


def _number_forms(text: str) -> list[tuple[str, set[Decimal]]]:
    """Each number in `text`, as written, with the values it can match: its own,
    and its full size if a scale word follows it.

    Example: in "$1.25 billion, up 1.25%", "1.25" can be 1.25 or 1250000000, and
    the second "1.25" only 1.25: the scale goes with the number it's written
    next to, found by where it starts in the text, not by its value. Exact, so
    "$1.2 billion" is never 1,234,000,000.
    """
    # Where each scaled number starts, and its power of ten.
    power_at = {}
    for m in _SCALED.finditer(text):
        group = next(g for g in ("a", "b", "c") if m.group(g))
        word = (m.group("w") or m.group("l") or m.group("bn")).lower()
        power_at[m.start(group)] = _POWERS[_SCALE_NAMES.get(word, word)]

    forms = []
    for m in _NUMBER.finditer(text):
        value = _value(m.group(0))
        values = {value}
        if m.start() in power_at:
            values.add(value * 10 ** power_at[m.start()])  # Decimal times int: exact
        forms.append((m.group(0), values))
    return forms


def _all_values(text: str) -> set[Decimal]:
    """Every value a number in `text` can match (_number_forms), in one set.
    Example: _all_values("$1.25 billion, up 3%") == {1.25, 1250000000, 3}."""
    return {value for _, values in _number_forms(text) for value in values}


def _table_values(quote: str) -> set[Decimal]:
    """Values in `quote` that sit like table cells: followed by another number,
    a bracket ("(5.2)", a table's negative), a currency sign or a dash, or ending the quote. In "Revenues 146.1
    980.8 571%", 146.1 and 980.8. A table's unit is in its heading, which a quote
    doesn't have, so their scale can't be known. A percentage ("12%"), a day
    ("13, 2026") or a count ("5 directors") isn't one: it's followed by %, a
    comma or a word."""
    cells = set()
    for m in _NUMBER.finditer(quote):
        after = quote[m.end():].lstrip()
        if not after or after[0] in "0123456789()$€£—–":
            cells.add(_value(m.group(0)))
    return cells


def _scale_problems(where: str, text: str, quotes: list[str]) -> list[str]:
    """A number written with one scale whose quotes give it only another: "$4.2
    billion" when every quote with 4.2 says "million". Same value, so the value
    check passes it; the worst mistake a reader could get.

    Not judged: a value a quote gives as a table cell (_table_values), so a
    correct "$1.5 billion" from a table isn't failed by "1.5 million shares" in
    another quote. Each quote is read on its own, so one quote's end isn't taken
    for a table cell in the next."""
    said: dict[Decimal, set[str]] = {}
    cells: set[Decimal] = set()
    for quote in quotes:
        for value, scale, _ in _scaled(quote):
            said.setdefault(value, set()).add(scale)
        cells |= _table_values(quote)
    return [
        f"{where}: {written!r} but the quote says {' or '.join(sorted(said[value]))}"
        for value, scale, written in _scaled(text)
        if value in said and value not in cells and scale not in said[value]
    ]


# A space just inside a symbol a table keeps in its own cell: after "$" or "(",
# before ")" or "%". A filing's HTML often puts "$", ")" and "%" in cells of their
# own, so the text has "$ (112,965 )" where a reader sees "$(112,965)" (Nov 26, 2026:
# 913 of them across the eval's 22 filings). Spaces anywhere else stay.
_SYMBOL_SPACE = re.compile(r"(?<=[$(]) | (?=[)%])")


def _normalize(text: str) -> str:
    """Every run of spaces, tabs and line breaks becomes one space, so a quote
    copied as one clean line still matches a filing that breaks it across lines.
    And a space next to a symbol from its own table cell (_SYMBOL_SPACE) goes, so
    "$377,701" matches a table's "$ 377,701", and the other way round.

    Example: _normalize("up 62 %\\n from  $ (1.2 ) a year ago ") gives
    "up 62% from $(1.2) a year ago".
    """
    return _SYMBOL_SPACE.sub("", " ".join(text.split()))


def filed_date(event: Event) -> date:
    """The day the filing came out, in New York, as the model's first line gives it.
    New York's date, like the page: a filing at 9pm New York is already the next
    day in UTC."""
    return event.occurred_at.astimezone(_NEW_YORK).date()


def _date_pattern(filed: date | None) -> re.Pattern | None:
    """The filing date as a summary might write it: "November 13, 2026", "Nov. 13",
    "13 November 2026", "2026-11-13", "11/13/2026". It's in the model's first line,
    not in any document, so it can't be quoted: taken out before reading numbers,
    like a company name. Only the whole date: a 13 elsewhere is still checked."""
    if filed is None:
        return None
    months = {filed.strftime("%B"), filed.strftime("%b")} | ({"Sept"} if filed.month == 9 else set())
    month = "(?:" + "|".join(sorted((re.escape(m) + r"\.?" for m in months), key=len, reverse=True)) + ")"
    d, y, m = filed.day, filed.year, filed.month
    forms = [
        rf"{month}\s+0?{d}(?:st|nd|rd|th)?(?:,?\s+{y})?",   # November 13, 2026 / Nov. 13
        rf"0?{d}\s+{month},?\s+{y}",                         # 13 November 2026
        rf"{y}-{m:02d}-{d:02d}",                             # 2026-11-13
        rf"0?{m}/0?{d}/{y}",                                 # 11/13/2026
    ]
    return re.compile(rf"(?<![\w/-])(?:{'|'.join(forms)})(?![\w/-]|[.,]\d)", re.IGNORECASE)


def _message_text(event: Event, documents: dict[str, str]) -> str:
    """What the model reads: one line saying which filing this is, then each
    document wrapped in a tag with its type, so the model can name its source.

    The line has no event title: titles are our page labels ("Activist / >5%
    stake" for every 13D), not the filing's words, and the model would take
    them as facts (prompt v3).

    Example, for an 8-K with one exhibit:

        NVDA 8-K, filed 2026-11-19

        <document type="8-K">
        ...
        </document>

        <document type="EX-99.1">
        ...
        </document>
    """
    parts = [f"{event.ticker} {event.kind}, filed {filed_date(event)}"]
    for doc_type, text in documents.items():
        parts.append(f'<document type="{doc_type}">\n{text}\n</document>')
    return "\n\n".join(parts)


# The legal ending SEC puts on a name, dropped so "Hut 8 Corp." is found as the
# summary writes it, "Hut 8". Only at the very end, so "Coinbase" keeps its "Co".
_LEGAL_ENDING = re.compile(
    r"[,\s]+(?:corp(?:oration)?|inc(?:orporated)?|co|company|ltd|limited|plc|n\.v|s\.a"
    r"|ag|se|llc|l\.p)\.?$",
    re.IGNORECASE,
)


def _name_pattern(company: str | None) -> re.Pattern | None:
    """The company's name, to take out of a text before reading its numbers: the 8
    in "Hut 8" is part of a name, not a number. None if there's no name, or if it
    has no digit (nothing to take out) or no letter (taking out "8" would hide
    every 8)."""
    if not company:
        return None
    short = company.strip()
    while (shorter := _LEGAL_ENDING.sub("", short)) != short:  # "X Co., Ltd." -> "X"
        short = shorter
    if not re.search(r"\d", short) or not re.search(r"[A-Za-z]", short):
        return None
    # Whole words only, any capitals ("HUT 8 CORP." on a cover page).
    return re.compile(rf"(?<!\w){re.escape(short)}(?!\w)", re.IGNORECASE)


def check_summary(
    summary: Summary, documents: dict[str, str], company: str | None = None, filed: date | None = None,
) -> list[str]:
    """The problems with a summary, as messages. Empty means it passed.

    For each key number: its source must be one of the documents, its quote must
    be in that document word for word (only spacing is forgiven), and the numbers
    in it must be in its quote. And every number in the summary sentences and the
    "key detail" bullets must be in one of the quotes, so a number can't skip the
    checks by not being listed. Also: at most MAX_WORDS words, MAX_KEY_NUMBERS key
    numbers, MAX_CHANGES bullets, and MAX_CHANGE_WORDS words in a bullet.

    `company` is the name SEC gives the company (event.fields["company"]). A digit
    in it isn't read as a number anywhere: not in the summary or bullets, where
    "Hut 8" would fail, and not in a quote, where it would let any 8 pass. The
    word-for-word quote check still sees the whole quote.

    `filed` is the filing date from the model's first line (filed_date). Written
    as a date in the summary or a bullet, it needs no quote: no document has it.
    """
    problems = []
    name = _name_pattern(company)
    filing_date = _date_pattern(filed)

    def numbers_text(text: str, written: bool = False) -> str:
        """`text` with the company's name taken out, and for the model's own
        sentences (written=True), the filing date too."""
        if name:
            text = name.sub(" ", text)
        if written and filing_date:
            text = filing_date.sub(" ", text)
        return text

    # The size check: the API can't limit lengths while the model writes, so code does.
    words = len(summary.summary.split())
    if words == 0:
        problems.append("summary: empty")
    if words > MAX_WORDS:
        problems.append(f"summary: {words} words, more than {MAX_WORDS}")
    if len(summary.key_numbers) > MAX_KEY_NUMBERS:
        problems.append(f"{len(summary.key_numbers)} key numbers, more than {MAX_KEY_NUMBERS}")
    if len(summary.changes) > MAX_CHANGES:
        problems.append(f"{len(summary.changes)} bullets, more than {MAX_CHANGES}")
    # Bullets are named from 1, as a reader counts them: "bullet 2: ...".
    for i, change in enumerate(summary.changes, start=1):
        bullet_words = len(change.split())
        if bullet_words == 0:
            problems.append(f"bullet {i}: empty")
        elif bullet_words > MAX_CHANGE_WORDS:
            problems.append(f"bullet {i}: {bullet_words} words, more than {MAX_CHANGE_WORDS}")

    # Each document cleaned once, not once per key number that quotes it.
    clean = {doc_type: _normalize(text) for doc_type, text in documents.items()}

    for kn in summary.key_numbers:
        if kn.source not in clean:
            problems.append(f"{kn.source}: no such document (number {kn.number!r})")
            continue  # nothing to search the quote in

        if _normalize(kn.quote) not in clean[kn.source]:
            problems.append(f"{kn.source}: quote not found: {kn.quote!r}")

        # Compare the numbers, not the text: "$980.8 million (571%)" passes if
        # both 980.8 and 571 are in the quote, however the model wrote them. A
        # scaled number may match at full size, either way round: "$1.25 billion"
        # and "$1,250,000,000" are the same number.
        numbers = _number_forms(kn.number)
        quote_values = _all_values(numbers_text(kn.quote))
        if not numbers:
            problems.append(f"{kn.source}: {kn.number!r} has no number in it")
        elif any(not values & quote_values for _, values in numbers):
            problems.append(f"{kn.source}: {kn.number!r} is not in its quote: {kn.quote!r}")
        else:
            problems += _scale_problems(kn.source, kn.number, [numbers_text(kn.quote)])

    # The summary-numbers check, for the sentences and each bullet. Compare whole
    # numbers, not text: "2" is inside "62" as text, but it isn't the same number.
    quotes = [numbers_text(kn.quote) for kn in summary.key_numbers]
    # Each quote read on its own, so a scale word is never taken from the next one.
    quoted = set().union(*(_all_values(quote) for quote in quotes))
    texts = [("summary", summary.summary)]
    texts += [(f"bullet {i}", change) for i, change in enumerate(summary.changes, start=1)]
    for where, text in texts:
        for number, values in _number_forms(numbers_text(text, written=True)):
            if not values & quoted:
                problems.append(f"{where}: {number!r} is not in any quote")
        # Same value, wrong scale: "$4.2 billion" from a quote saying "$4.2 million".
        problems += _scale_problems(where, numbers_text(text, written=True), quotes)
    return problems


def dollars(input_tokens: int, output_tokens: int) -> float:
    """What these tokens cost at MODEL's prices. Example: dollars(28_000, 500) is 0.122."""
    return (
        input_tokens * INPUT_DOLLARS_PER_MTOK + output_tokens * OUTPUT_DOLLARS_PER_MTOK
    ) / 1_000_000


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
    # Keep the fact, add its quote: v2's "keep only numbers that are in your quotes"
    # made the model drop true numbers (CleanSpark's $814.9M of bitcoin, Nov 22 eval).
    return (
        "Your previous answer, below, failed the code checks. Write a new answer "
        "that fixes every problem listed. Copy quotes exactly from the documents "
        "above. If a number in your summary or bullets isn't in any quote, add a key number "
        "with the sentence from the documents that contains it. Remove the number "
        "only if it isn't in the documents at all, or if you already have "
        f"{MAX_KEY_NUMBERS} key numbers and it matters less than they do.\n\n"
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
    # SEC's name for the company, saved at collection (None for older events): so
    # the 8 in "Hut 8" isn't read as a number. Both tries are checked the same way.
    company, filed = event.fields.get("company"), filed_date(event)
    first = summarize(event, documents, client)
    problems = check_summary(first.summary, documents, company, filed)
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

    problems = check_summary(second.summary, documents, company, filed)
    if problems:
        raise SummaryError(
            f"Summary for {event.id} failed its checks twice: " + "; ".join(problems),
            input_tokens, output_tokens,
        )
    return SummaryResult(second.summary, second.model, input_tokens, output_tokens)
