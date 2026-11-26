from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from decimal import Decimal

from daily_digest_desk.models import Event
from daily_digest_desk.reasoning import summarize as summarize_module
from daily_digest_desk.reasoning.summarize import (
    MAX_CHANGE_WORDS, MAX_CHANGES, MAX_KEY_NUMBERS, MAX_WORDS, KeyNumber, Summary,
    SummaryError, SummaryResult, _message_text, _normalize, _NUMBER, _retry_note, _tokens, check_summary, summarize,
    dollars, summarize_checked,
)


def event(occurred_at=datetime(2026, 11, 19, 21, 30, tzinfo=timezone.utc)):
    return Event(
        id="edgar:0001045810-26-000088", source="edgar", kind="8-K", ticker="NVDA",
        occurred_at=occurred_at, title="NVIDIA 8-K (2.02 Results of Operations)",
        url="https://www.sec.gov/",
    )


# A filing as trim_documents gives it. The press release breaks its sentence
# across lines with a double space, the way EDGAR text often does.
DOCS = {
    "8-K": "Item 2.02 Results of Operations and Financial Condition.",
    "EX-99.1": "Revenue was $57.0 billion,  up 62%\nfrom a year ago. We added 1,200 employees.",
}
REVENUE = KeyNumber(
    number="$57.0 billion",
    quote="Revenue was $57.0 billion, up 62% from a year ago.",  # one clean line
    source="EX-99.1",
)


def summary(text="NVIDIA reported revenue of $57.0 billion, up 62%.", key_numbers=(REVENUE,)):
    return Summary(summary=text, key_numbers=list(key_numbers))


# --- _normalize ----------------------------------------------------------------------

def test_normalize_makes_every_run_of_whitespace_one_space():
    assert _normalize("  up 62%\n from\t\ta  year ago \n") == "up 62% from a year ago"


def test_normalize_joins_symbols_from_their_own_table_cells():
    # CleanSpark's EBITDA row, as the text lays its cells out.
    assert _normalize("$ (112,965 )   $ 377,701   30.5 %") == "$(112,965) $377,701 30.5%"
    assert _normalize("( 16,506 )") == "(16,506)"
    # Spaces first made one, then the symbol's taken off: a cell can end a line.
    assert _normalize("$\n   377,701   %") == "$377,701%"


def test_normalize_keeps_every_other_space():
    # After ")" and before "$" or "(": words go on either side of those.
    assert _normalize("a loss (net) of $5 and (b) 7 %") == "a loss (net) of $5 and (b) 7%"
    assert _normalize("1 234") == "1 234"  # two numbers stay two numbers


# --- _NUMBER: what counts as a number ------------------------------------------------

def test_number_pattern_finds_amounts():
    text = "Revenue of $57.0 billion, up 62%, 1,200 hires, $1,234.56 million, in 2026."
    assert _NUMBER.findall(text) == ["57.0", "62", "1,200", "1,234.56", "2026"]


def test_number_pattern_skips_labels():
    # Glued to letters or a hyphen: names of things, not amounts.
    assert _NUMBER.findall("This 8-K and 10-Q, EX-99.1, Q3, the 3rd quarter.") == []


# One row per format. When a filing shows a new one, add a row here first.
@pytest.mark.parametrize("text, numbers", [
    # Labels: an uppercase word or a rule code on the other side of the hyphen.
    ("8-K", []),
    ("10-Q", []),
    ("Form 20-F", []),
    ("EX-99.1", []),
    ("S-3ASR", []),
    ("SCHEDULE 13D/A", []),
    ("424B5", []),
    ("Series A-1 preferred", []),
    ("COVID-19", []),
    ("Form 10-K405", []),
    ("a Rule 10b5-1 plan", []),
    ("Rule 13a-16", []),
    ("Rule 10b-5", []),
    # Numbers next to a hyphen: a digit or a lowercase word on the other side.
    ("$12-13 billion", ["12", "13"]),
    ("each 90-day period", ["90"]),
    ("30-year notes", ["30"]),
    ("a 1-for-10 reverse split", ["1", "10"]),
    ("by mid-2027", ["2027"]),
    ("from 2.75%-3.50%", ["2.75", "3.50"]),
    ("-130.2", ["130.2"]),
    # Dollar amounts with a letter for the size.
    ("$981M", ["981"]),
    ("$1.2B", ["1.2"]),
    ("$5.5bn", ["5.5"]),
    ("$500K", ["500"]),
    # Whole numbers only, never the front of a longer one.
    ("7843779.00", ["7843779.00"]),
    ("3.5x", []),
])
def test_number_pattern_formats(text, numbers):
    assert _NUMBER.findall(text) == numbers


# --- _message_text -------------------------------------------------------------------

def test_message_text_has_a_header_then_each_document_in_order():
    docs = {"8-K": "cover", "EX-99.1": "press release"}
    assert _message_text(event(), docs) == (
        "NVDA 8-K, filed 2026-11-19\n\n"
        '<document type="8-K">\ncover\n</document>\n\n'
        '<document type="EX-99.1">\npress release\n</document>'
    )


def test_message_text_leaves_out_our_page_label():
    # Titles are our labels ("Activist / >5% stake" for every 13D), not the
    # filing's words: the model would take them as facts.
    activist = event().model_copy(update={"title": "HUT SCHEDULE 13D/A (amended): Activist / >5% stake"})
    assert "Activist" not in _message_text(activist, {"SCHEDULE 13D/A": "the filing"})


def test_message_text_uses_new_yorks_date():
    # 9pm New York on Nov 19 is 2am UTC on Nov 20.
    late = event(occurred_at=datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc))
    assert "filed 2026-11-19" in _message_text(late, {})


# --- check_summary: what passes ------------------------------------------------------

def test_a_good_summary_passes():
    # The quote's spacing differs from the filing's; only spacing is forgiven.
    assert check_summary(summary(), DOCS) == []


def test_a_routine_summary_with_no_numbers_passes():
    routine = summary("Routine filing: the company updated its bylaws.", key_numbers=[])
    assert check_summary(routine, DOCS) == []


def test_labels_in_the_summary_are_not_numbers_to_check():
    assert check_summary(summary("In this 8-K, revenue was $57.0 billion."), DOCS) == []


# One row of a table, laid out cell by cell: "$", ")" and "%" in cells of their own.
TABLE_DOCS = {"EX-99.1": "Adjusted EBITDA   $ (112,965 )   $ 377,701\nMargin   30.5 %"}


def test_a_table_quote_passes_however_its_symbols_are_spaced():
    for quote in (
        "Adjusted EBITDA $(112,965) $377,701",       # as a reader sees it
        "Adjusted EBITDA $ (112,965 ) $ 377,701",    # copied from the text
    ):
        kn = KeyNumber(number="$112,965", quote=quote, source="EX-99.1")
        assert check_summary(summary("Adjusted EBITDA was negative $112,965.", [kn]), TABLE_DOCS) == [], quote


def test_a_table_quote_with_a_wrong_number_still_fails():
    kn = KeyNumber(number="$112,966", quote="Adjusted EBITDA $(112,966)", source="EX-99.1")
    problems = check_summary(summary("Adjusted EBITDA was negative $112,966.", [kn]), TABLE_DOCS)
    assert problems == ["EX-99.1: quote not found: 'Adjusted EBITDA $(112,966)'"]


def test_a_number_with_odd_spacing_still_matches_its_quote():
    kn = REVENUE.model_copy(update={"number": "$57.0  billion"})
    assert check_summary(summary(key_numbers=[kn]), DOCS) == []


# --- check_summary: the key numbers --------------------------------------------------

def test_a_source_that_is_not_a_document():
    kn = REVENUE.model_copy(update={"source": "EX-99.4"})
    # One problem, not a KeyError, and no quote check (there's nothing to search).
    assert check_summary(summary(key_numbers=[kn]), DOCS) == [
        "EX-99.4: no such document (number '$57.0 billion')",
    ]


def test_after_a_missing_source_the_next_key_numbers_are_still_checked():
    missing = REVENUE.model_copy(update={"source": "EX-99.4"})
    wrong = REVENUE.model_copy(update={"number": "$12 billion"})
    problems = check_summary(summary(key_numbers=[missing, wrong]), DOCS)
    assert len(problems) == 2  # skip only the missing one, not the rest


def test_a_made_up_quote():
    kn = KeyNumber(number="$75.0 billion", quote="Revenue was $75.0 billion.", source="EX-99.1")
    problems = check_summary(summary("Revenue was $75.0 billion.", key_numbers=[kn]), DOCS)
    assert problems == ["EX-99.1: quote not found: 'Revenue was $75.0 billion.'"]


def test_a_reworded_quote_fails():
    # Same meaning, different characters: code can't tell harmless rewordings from
    # harmful ones, so only word-for-word passes.
    kn = REVENUE.model_copy(update={"quote": "Revenue was $57.0 billion, up 62 percent from a year ago."})
    assert check_summary(summary(key_numbers=[kn]), DOCS) == [
        f"EX-99.1: quote not found: {kn.quote!r}",
    ]


def test_a_real_quote_in_the_wrong_document_fails():
    kn = REVENUE.model_copy(update={"source": "8-K"})
    assert check_summary(summary(key_numbers=[kn]), DOCS) == [
        f"8-K: quote not found: {kn.quote!r}",
    ]


def test_a_number_that_is_not_in_its_quote():
    kn = REVENUE.model_copy(update={"number": "$12 billion"})
    assert check_summary(summary(key_numbers=[kn]), DOCS) == [
        f"EX-99.1: '$12 billion' is not in its quote: {kn.quote!r}",
    ]


def test_a_number_written_differently_from_its_quote_passes():
    # The live NBIS case: two numbers joined, with the model's own parentheses.
    # Both are in the quote, so the order and formatting don't matter.
    kn = REVENUE.model_copy(update={"number": "$57.0 billion (62%)"})
    assert check_summary(summary(key_numbers=[kn]), DOCS) == []


def test_a_key_number_matches_its_quote_by_value():
    # The HUT case: the quote is a form field, written without commas.
    docs = {"SCHEDULE 13D/A": "<aggregateAmountOwned>7843779.00</aggregateAmountOwned>"}
    kn = KeyNumber(
        number="7,843,779 shares",
        quote="<aggregateAmountOwned>7843779.00</aggregateAmountOwned>",
        source="SCHEDULE 13D/A",
    )
    assert check_summary(summary("He owns 7,843,779 shares.", [kn]), docs) == []


def test_a_summary_number_matches_a_quote_by_value():
    # The Apple notes: the filing writes 5.350%, a summary may write 5.35%. And 1200 for 1,200.
    docs = {"8-K": "$2,000,000,000 of its 5.350% Notes due 2036, and 1,200 more."}
    notes = KeyNumber(number="5.350%", quote=docs["8-K"], source="8-K")
    text = "Apple sold $2000000000 of 5.35% notes due 2036, and 1200 more."
    assert check_summary(summary(text, [notes]), docs) == []


def test_a_rounded_number_is_not_the_same_value():
    # The Apple case: the model rounded 11.7% to 12%. A different number, not
    # another way to write the same one.
    docs = {"EX-99.1": "Services revenue grew 11.7% from a year ago."}
    growth = KeyNumber(number="11.7%", quote="Services revenue grew 11.7%", source="EX-99.1")
    assert check_summary(summary("Services revenue grew 12%.", [growth]), docs) == [
        "summary: '12' is not in any quote",
    ]


def test_one_wrong_number_in_a_combined_number_fails():
    kn = REVENUE.model_copy(update={"number": "$57.0 billion (26%)"})
    assert check_summary(summary(key_numbers=[kn]), DOCS) == [
        f"EX-99.1: '$57.0 billion (26%)' is not in its quote: {kn.quote!r}",
    ]


def test_a_number_with_no_digits_fails():
    # Nothing to compare, so it can't be checked: a problem, not a silent pass.
    kn = REVENUE.model_copy(update={"number": "two"})
    assert check_summary(summary(key_numbers=[kn]), DOCS) == [
        "EX-99.1: 'two' has no number in it",
    ]


def test_an_important_date_quoted_like_a_key_number_passes():
    docs = {"8-K": "The notes will mature on March 15, 2031, unless converted earlier."}
    due = KeyNumber(number="March 15, 2031", quote="The notes will mature on March 15, 2031", source="8-K")
    assert check_summary(summary("The notes are due March 15, 2031.", [due]), docs) == []


def test_a_routine_date_with_no_quote_fails():
    # The live NVDA case: a date needs its sentence quoted, like any number
    # (prompt v3 asks for the period's end date, with its quote).
    text = "Revenue for the quarter ended October 25, 2026 was $57.0 billion."
    assert check_summary(summary(text), DOCS) == [
        "summary: '25' is not in any quote", "summary: '2026' is not in any quote",
    ]


def test_both_problems_of_one_key_number_are_reported():
    kn = KeyNumber(number="$12 billion", quote="Revenue was $75.0 billion.", source="EX-99.1")
    problems = check_summary(summary("Revenue grew.", key_numbers=[kn]), DOCS)
    assert len(problems) == 2


def test_every_key_number_is_checked_not_just_the_first():
    hires = KeyNumber(number="1,300", quote="We added 1,300 employees.", source="EX-99.1")
    problems = check_summary(summary("Revenue was $57.0 billion.", [REVENUE, hires]), DOCS)
    assert problems == ["EX-99.1: quote not found: 'We added 1,300 employees.'"]


# --- check_summary: the numbers in the summary sentences -----------------------------

def test_a_number_only_in_the_summary_is_caught():
    text = "NVIDIA reported revenue of $57.0 billion, up 62%, and profit of $40 billion."
    assert check_summary(summary(text), DOCS) == ["summary: '40' is not in any quote"]


def test_summary_numbers_are_compared_whole_not_as_text():
    # "2" is inside "62" as text, but it's a different number.
    assert check_summary(summary("Revenue rose 2%."), DOCS) == ["summary: '2' is not in any quote"]


def test_a_summary_number_can_come_from_any_quote():
    hires = KeyNumber(number="1,200", quote="We added 1,200 employees.", source="EX-99.1")
    text = "Revenue was $57.0 billion, up 62%, and NVIDIA added 1,200 employees."
    assert check_summary(summary(text, [REVENUE, hires]), DOCS) == []


def test_a_summary_number_in_the_filing_but_not_in_a_quote_is_caught():
    # 1,200 is really in the filing, but no quote backs it, so nothing checked it.
    text = "Revenue was $57.0 billion, and NVIDIA added 1,200 employees."
    assert check_summary(summary(text), DOCS) == ["summary: '1,200' is not in any quote"]


# --- check_summary: size -------------------------------------------------------------

def test_an_empty_summary():
    for text in ("", "  \n "):
        assert check_summary(summary(text, key_numbers=[]), DOCS) == ["summary: empty"]


def test_words_up_to_the_limit_pass_and_one_more_fails():
    at_limit = summary("word " * MAX_WORDS, key_numbers=[])
    over = summary("word " * (MAX_WORDS + 1), key_numbers=[])
    assert check_summary(at_limit, DOCS) == []
    assert check_summary(over, DOCS) == [f"summary: {MAX_WORDS + 1} words, more than {MAX_WORDS}"]


def test_key_numbers_up_to_the_limit_pass_and_one_more_fails():
    def many(n):
        return summary("Revenue was $57.0 billion.", key_numbers=[REVENUE] * n)

    assert check_summary(many(MAX_KEY_NUMBERS), DOCS) == []
    assert check_summary(many(MAX_KEY_NUMBERS + 1), DOCS) == [
        f"{MAX_KEY_NUMBERS + 1} key numbers, more than {MAX_KEY_NUMBERS}",
    ]


# --- check_summary: the "key detail" bullets ---------------------------------------

def with_bullets(*changes):
    return summary().model_copy(update={"changes": list(changes)})


def test_bullets_up_to_the_limit_pass_and_one_more_fails():
    bullet = "Revenue was $57.0 billion."
    assert check_summary(with_bullets(*[bullet] * MAX_CHANGES), DOCS) == []
    assert check_summary(with_bullets(*[bullet] * (MAX_CHANGES + 1)), DOCS) == [
        f"{MAX_CHANGES + 1} bullets, more than {MAX_CHANGES}",
    ]


def test_an_empty_bullet_and_a_rambling_one_fail():
    long = "word " * (MAX_CHANGE_WORDS + 1)
    ok = "word " * MAX_CHANGE_WORDS
    assert check_summary(with_bullets(" ", ok, long), DOCS) == [
        "bullet 1: empty",
        f"bullet 3: {MAX_CHANGE_WORDS + 1} words, more than {MAX_CHANGE_WORDS}",
    ]


def test_a_quoted_number_in_a_bullet_passes():
    assert check_summary(with_bullets("Growth was 62% from a year ago."), DOCS) == []


def test_a_number_in_a_bullet_with_no_quote_is_caught_and_named():
    # The bullets are where a summary could add numbers past the checks.
    problems = check_summary(with_bullets("Growth was 62%.", "It hired 1,200 people."), DOCS)
    assert problems == ["bullet 2: '1,200' is not in any quote"]


# --- check_summary: million vs billion -----------------------------------------------

SCALE_DOCS = {"EX-99.1": "Revenue was $4.2 million in the quarter.\nRevenues 146.1 980.8 571%"}
MILLION = KeyNumber(number="$4.2 million", quote="Revenue was $4.2 million in the quarter.", source="EX-99.1")
TABLE = KeyNumber(number="$980.8 million", quote="Revenues 146.1 980.8 571%", source="EX-99.1")


@pytest.mark.parametrize("text", ["Revenue was $4.2 billion.", "Revenue was $4.2B.", "Revenue was $4.2 thousand."])
def test_the_right_value_at_the_wrong_scale_fails(text):
    problems = check_summary(summary(text, [MILLION]), SCALE_DOCS)
    assert len(problems) == 1 and problems[0].startswith("summary: '$4.2") and "the quote says million" in problems[0]


def test_the_same_scale_written_another_way_passes():
    for text in ["Revenue was $4.2 million.", "Revenue was $4.2M.", "Revenue was $4.2 Million."]:
        assert check_summary(summary(text, [MILLION]), SCALE_DOCS) == [], text


def test_bn_is_billion():
    docs = {"EX-99.1": "Debt was $1.2 billion."}
    debt = KeyNumber(number="$1.2 billion", quote="Debt was $1.2 billion.", source="EX-99.1")
    assert check_summary(summary("Debt was $1.2bn.", [debt]), docs) == []


def test_a_table_line_with_no_scale_word_is_not_judged():
    # The table's "in millions" heading is too far away to know: leave it alone.
    assert check_summary(summary("Revenue was $980.8 million.", [TABLE]), SCALE_DOCS) == []
    assert check_summary(summary("Revenue was $980.8 billion.", [TABLE]), SCALE_DOCS) == []


def test_a_key_numbers_own_scale_is_checked_against_its_quote():
    wrong = KeyNumber(number="$4.2 billion", quote=MILLION.quote, source="EX-99.1")
    problems = check_summary(summary("Revenue was $4.2 million.", [wrong]), SCALE_DOCS)
    assert problems == ["EX-99.1: '$4.2 billion' but the quote says million"]


def test_a_bullet_at_the_wrong_scale_is_named():
    s = Summary(summary="Revenue was $4.2 million.", key_numbers=[MILLION], changes=["It was $4.2 billion."])
    assert check_summary(s, SCALE_DOCS) == ["bullet 1: '$4.2 billion' but the quote says million"]


def test_a_table_value_isnt_failed_by_the_same_value_at_another_scale_elsewhere():
    # The advisor's case: a correct "$1.5 billion" from a table line, and "1.5
    # million shares" in another quote. The table's unit is unknown: not judged.
    docs = {"EX-99.1": "Revenues 1.5 0.9 67%\nThe company repurchased 1.5 million shares."}
    table = KeyNumber(number="$1.5 billion", quote="Revenues 1.5 0.9 67%", source="EX-99.1")
    shares = KeyNumber(number="1.5 million", quote="The company repurchased 1.5 million shares.", source="EX-99.1")
    s = summary("Revenue was $1.5 billion; it bought back 1.5 million shares.", [table, shares])
    assert check_summary(s, docs) == []
    # With no table line, the same value at the wrong scale still fails.
    s = summary("It bought back 1.5 billion shares.", [shares])
    assert check_summary(s, docs) == ["summary: '1.5 billion' but the quote says million"]


@pytest.mark.parametrize("text", ["Rule 10b-5", "Rule 10b5-1", "10K filing", "a 3M product", "5B shares"])
def test_a_letter_after_a_number_is_a_scale_only_after_a_currency_sign(text):
    from daily_digest_desk.reasoning.summarize import _scaled
    assert _scaled(text) == []


@pytest.mark.parametrize("text, scale", [
    ("$4.2B", "billion"), ("$981M", "million"), ("$4.2m", "million"), ("€1.2bn", "billion"),
    ("1.2bn", "billion"), ("£3K", "thousand"), ("4.2 million", "million"), ("$4.2 Billion", "billion"),
])
def test_scale_words_that_count(text, scale):
    from daily_digest_desk.reasoning.summarize import _scaled
    assert [s for _, s, _ in _scaled(text)] == [scale]


@pytest.mark.parametrize("wrong, quote", [
    # The advisor's three: a small number elsewhere in the quote isn't a table cell.
    ("It has a $12 billion facility.", "It has a $12 million facility. Its margin rose 12% this year."),
    ("It raised $13 billion.", "It raised $13 million, and the deal closed on November 13, 2026."),
    ("It had a loss of $5 billion.", "It had a loss of $5 million, and the board has 5 directors."),
    ("It signed a $12 billion deal.", "It signed a $12 million deal for a 12-month term."),
])
def test_a_percentage_day_or_count_doesnt_hide_a_wrong_scale(wrong, quote):
    k = KeyNumber(number="$1", quote=quote, source="EX-99.1")
    problems = check_summary(summary(wrong, [k]), {"EX-99.1": quote})
    assert [p for p in problems if "but the quote says million" in p], (wrong, problems)


def test_a_cell_at_the_end_of_a_quote_is_still_a_cell():
    # Each quote is read on its own: joined, "Revenues 1.5" would run into the
    # next quote's first word and stop looking like a table cell.
    docs = {"EX-99.1": "Revenues 1.5\nThe company repurchased 1.5 million shares."}
    table = KeyNumber(number="$1.5 billion", quote="Revenues 1.5", source="EX-99.1")
    shares = KeyNumber(number="1.5 million", quote="The company repurchased 1.5 million shares.", source="EX-99.1")
    assert check_summary(summary("Revenue was $1.5 billion.", [table, shares]), docs) == []


def test_table_cells_are_numbers_next_to_numbers_or_at_the_end():
    from daily_digest_desk.reasoning.summarize import _table_values
    assert _table_values("Revenues 146.1 980.8 571%") == {Decimal("146.1"), Decimal("980.8")}
    assert _table_values("Total $ 4,213 $ 3,980") == {Decimal("4213"), Decimal("3980")}
    assert _table_values("Net loss (5.2) 465.8 n/m") == {Decimal("5.2")}  # a negative in brackets; 465.8 is before a word
    assert _table_values("margin rose 12%; closed November 13, 2026; 5 directors") == set()


@pytest.mark.parametrize("text, scale", [("US$4.2B", "billion"), ("C$981M", "million"), ("A$5m", "million")])
def test_a_glued_scale_after_a_prefixed_dollar_sign_counts(text, scale):
    from daily_digest_desk.reasoning.summarize import _scaled
    assert [(s, w) for _, s, w in _scaled(text)] == [(scale, text[text.index("$"):])]


def test_a_canadian_amount_at_the_wrong_scale_fails():
    quote = "It sold C$981M of notes."
    k = KeyNumber(number="C$981M", quote=quote, source="EX-99.1")
    assert check_summary(summary("It sold C$981 billion of notes.", [k]), {"EX-99.1": quote}) == [
        "summary: '$981 billion' but the quote says million"]


def test_a_value_the_quotes_give_at_both_scales_passes():
    docs = {"EX-99.1": "Cash was $4.2 million. Debt was $4.2 billion."}
    both = KeyNumber(number="$4.2 million; $4.2 billion", source="EX-99.1",
                     quote="Cash was $4.2 million. Debt was $4.2 billion.")
    assert check_summary(summary("Debt was $4.2 billion.", [both]), docs) == []


# --- check_summary: the filing date from the first line ------------------------------

from datetime import date  # noqa: E402

FILED = date(2026, 11, 13)


@pytest.mark.parametrize("written", [
    "November 13, 2026", "Nov. 13, 2026", "Nov 13", "November 13th", "13 November 2026",
    "2026-11-13", "11/13/2026", "NOVEMBER 13, 2026",
])
def test_the_filing_date_needs_no_quote(written):
    s = summary(f"The company filed it on {written}; revenue was $57.0 billion.")
    assert check_summary(s, DOCS, filed=FILED) == [], written


def test_without_the_date_known_it_is_checked_like_any_number():
    s = summary("The company filed it on November 13, 2026; revenue was $57.0 billion.")
    assert check_summary(s, DOCS) == ["summary: '13' is not in any quote", "summary: '2026' is not in any quote"]


def test_only_the_whole_filing_date_is_exempt():
    for text, numbers in [
        ("It has 13 directors.", ["13"]),                           # a 13 that isn't the date
        ("Filed November 14, 2026.", ["14", "2026"]),               # another day
        ("Filed November 13, 2025.", ["2025"]),                     # another year: the year is checked
        ("It sold 130 units on November 130.", ["130", "130"]),     # not a date at all
    ]:
        problems = check_summary(summary(text, [REVENUE]), DOCS, filed=FILED)
        assert problems == [f"summary: {n!r} is not in any quote" for n in numbers], text


def test_a_bullet_can_give_the_filing_date_too():
    s = Summary(summary="Revenue was $57.0 billion.", key_numbers=[REVENUE], changes=["Filed Nov. 13, 2026."])
    assert check_summary(s, DOCS, filed=FILED) == []


def test_summarize_checked_passes_new_yorks_filing_date():
    # 2am UTC on the 14th is 9pm on the 13th in New York: the date in the first line.
    late = Event(id="edgar:x", source="edgar", kind="8-K", ticker="NVDA", title="t", url="https://www.sec.gov/",
                 occurred_at=datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc))
    client = FakeClient(answer(parsed_output=summary("Filed November 13, 2026: revenue was $57.0 billion.")))
    summarize_checked(late, DOCS, client)
    assert len(client.calls) == 1  # passed first time: no retry
    assert client.calls[0]["messages"][0]["content"].startswith("NVDA 8-K, filed 2026-11-13")


def test_summarize_checked_uses_the_filing_date_on_the_retry_too():
    late = Event(id="edgar:x", source="edgar", kind="8-K", ticker="NVDA", title="t", url="https://www.sec.gov/",
                 occurred_at=datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc))
    client = FakeClient(answer(parsed_output=summary("Revenue was $58.0 billion.")),
                        answer(parsed_output=summary("Filed November 13, 2026: revenue was $57.0 billion.")))
    summarize_checked(late, DOCS, client)
    assert len(client.calls) == 2


def test_the_prompt_says_the_filing_date_needs_no_quote():
    prompt = " ".join(summarize_module.SYSTEM_PROMPT.split())
    assert "The one exception is the filing date in the first line: it needs no quote." in prompt


# --- check_summary: a digit in the company's name ------------------------------------

# A 13D/A as EDGAR gives it: the name only in a data tag, the rest in sentences.
HUT_DOCS = {"SCHEDULE 13D/A": (
    "<issuerName>Hut 8 Corp.</issuerName>\n"
    "The Reporting Person received 1,517,367 shares of Common Stock.\n"
    "Hut 8 Corp. has 7 directors."
)}
SHARES = KeyNumber(number="1,517,367", source="SCHEDULE 13D/A",
                   quote="The Reporting Person received 1,517,367 shares of Common Stock.")
DIRECTORS = KeyNumber(number="7", source="SCHEDULE 13D/A", quote="Hut 8 Corp. has 7 directors.")


def test_the_digit_in_the_company_name_is_not_a_number():
    hut = summary("Michael Ho received 1,517,367 Hut 8 shares.", [SHARES])
    assert check_summary(hut, HUT_DOCS, company="Hut 8 Corp.") == []
    # Without the name: the bug every HUT summary hit on its first try.
    assert check_summary(hut, HUT_DOCS) == ["summary: '8' is not in any quote"]


def test_the_name_is_found_in_bullets_and_in_any_capitals():
    hut = Summary(summary="HUT 8 reported a stake.", key_numbers=[SHARES],
                  changes=["Hut 8's holder received 1,517,367 shares."])
    assert check_summary(hut, HUT_DOCS, company="Hut 8 Corp.") == []


def test_a_real_number_next_to_the_name_is_still_checked():
    for text, number in [("Hut 8 sold 8 machines.", "8"), ("Hut 80 rigs.", "80")]:
        problems = check_summary(summary(text, [SHARES]), HUT_DOCS, company="Hut 8 Corp.")
        assert problems == [f"summary: {number!r} is not in any quote"], text


def test_the_name_in_a_quote_doesnt_let_another_8_pass():
    # Read as a number, "Hut 8" in the quote would vouch for any 8 in the summary.
    wrong = summary("Hut 8 has 8 directors.", [DIRECTORS])
    assert check_summary(wrong, HUT_DOCS, company="Hut 8 Corp.") == ["summary: '8' is not in any quote"]
    assert check_summary(wrong, HUT_DOCS) == []  # the false pass, before the name was known
    right = summary("Hut 8 has 7 directors.", [DIRECTORS])
    assert check_summary(right, HUT_DOCS, company="Hut 8 Corp.") == []


def test_the_quote_itself_is_still_checked_word_for_word():
    bad = KeyNumber(number="7", source="SCHEDULE 13D/A", quote="Hut 9 Corp. has 7 directors.")
    problems = check_summary(summary("It has 7 directors.", [bad]), HUT_DOCS, company="Hut 8 Corp.")
    assert problems == ["SCHEDULE 13D/A: quote not found: 'Hut 9 Corp. has 7 directors.'"]


@pytest.mark.parametrize("company, text, kept", [
    ("Hut 8 Corp.", "Hut 8 shares", True),                 # legal ending dropped
    ("1-800-Flowers.com, Inc.", "1-800-Flowers.com sales", True),
    ("Nebius Group N.V.", "Nebius Group sold 2 sites", False),  # no digit: nothing to take out
    ("8 Inc.", "8 shares", False),                          # no letter: would hide every 8
    (None, "Hut 8 shares", False),
    ("", "Hut 8 shares", False),
])
def test_name_pattern(company, text, kept):
    from daily_digest_desk.reasoning.summarize import _name_pattern
    pattern = _name_pattern(company)
    assert (pattern is not None) == kept
    if pattern:
        assert _NUMBER.findall(pattern.sub(" ", text)) == []


# --- summarize: with a fake client ---------------------------------------------------

class FakeClient:
    """Stands in for anthropic.Anthropic(): gives the responses in order, one per
    call, and remembers what each call sent (`calls`; `sent` is the last one)."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    @property
    def sent(self):
        return self.calls[-1]

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def answer(stop_reason="end_turn", parsed_output=None, model="claude-opus-5-5",
           input_tokens=28_131, output_tokens=496):
    return SimpleNamespace(
        stop_reason=stop_reason, parsed_output=parsed_output, model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def test_summarize_returns_the_summary_model_and_token_counts():
    # Different input and output counts, so swapping them would show.
    client = FakeClient(answer(parsed_output=summary(), input_tokens=28_131, output_tokens=496))
    assert summarize(event(), DOCS, client) == SummaryResult(
        summary=summary(), model="claude-opus-5-5", input_tokens=28_131, output_tokens=496,
    )


def test_summarize_records_the_model_that_answered_not_the_one_asked():
    # If the first model declined, the fallback model answered: save its name.
    client = FakeClient(answer(parsed_output=summary(), model="claude-fallback-model"))
    assert summarize(event(), DOCS, client).model == "claude-fallback-model"


def test_summarize_sends_the_prompt_the_filing_and_the_shape():
    client = FakeClient(answer(parsed_output=summary()))
    summarize(event(), DOCS, client)
    sent = client.sent
    assert sent["model"] == summarize_module.MODEL
    assert sent["system"] == summarize_module.SYSTEM_PROMPT
    assert sent["messages"] == [{"role": "user", "content": _message_text(event(), DOCS)}]
    assert sent["output_format"] is Summary
    assert sent["output_config"] == {"effort": summarize_module.EFFORT}
    assert sent["fallbacks"] == "default"


def test_the_prompt_states_the_limits_the_checks_use():
    # The prompt is an f-string, so changing a limit changes what the model is told.
    prompt = " ".join(summarize_module.SYSTEM_PROMPT.split())
    assert f"at most {MAX_KEY_NUMBERS} key numbers" in prompt
    assert f"up to {summarize_module.MAX_CHANGES} short" in prompt


def test_the_schema_the_model_fills_uses_the_prompts_words_and_limits():
    # The model reads field titles and descriptions next to the prompt.
    props = Summary.model_json_schema()["properties"]
    assert props["changes"]["title"] == "Key details"  # not "Changes": no "what changed" pull
    assert f"Up to {summarize_module.MAX_CHANGES} \"key detail\" bullets" in props["changes"]["description"]
    assert f"Up to {MAX_KEY_NUMBERS} key numbers" in props["key_numbers"]["description"]
    number = Summary.model_json_schema()["$defs"]["KeyNumber"]["properties"]["number"]
    assert "in digits" in number["description"]


def test_a_summary_without_bullets_still_loads():
    # Summaries saved before v3 have no "changes"; the model may also leave it out.
    assert Summary(summary="Routine.", key_numbers=[]).changes == []
    assert Summary.model_validate({"summary": "Routine.", "key_numbers": []}).changes == []


@pytest.mark.parametrize("stop_reason, message", [
    ("refusal", "refused"),
    ("max_tokens", "cut off"),
])
def test_summarize_raises_when_the_model_gives_no_usable_answer(stop_reason, message):
    # A refused or cut-off answer is an error even if something was parsed.
    client = FakeClient(answer(stop_reason, parsed_output=summary(),
                               input_tokens=28_131, output_tokens=496))
    with pytest.raises(SummaryError, match=message) as error:
        summarize(event(), DOCS, client)
    assert event().id in str(error.value)
    # The call still cost money, so the error says how much.
    assert (error.value.input_tokens, error.value.output_tokens) == (28_131, 496)


def test_summarize_raises_when_there_is_no_summary_in_the_answer():
    client = FakeClient(answer("end_turn", parsed_output=None, input_tokens=100, output_tokens=7))
    with pytest.raises(SummaryError, match="No summary") as error:
        summarize(event(), DOCS, client)
    assert (error.value.input_tokens, error.value.output_tokens) == (100, 7)


def test_summarize_adds_the_feedback_after_the_filing():
    client = FakeClient(answer(parsed_output=summary()))
    summarize(event(), DOCS, client, feedback="Fix these.")
    assert client.sent["messages"] == [
        {"role": "user", "content": _message_text(event(), DOCS) + "\n\nFix these."},
    ]


# --- _tokens: every attempt in one call ----------------------------------------------

def test_tokens_without_iterations_is_the_top_level_usage():
    assert _tokens(SimpleNamespace(input_tokens=100, output_tokens=7)) == (100, 7)


def test_tokens_with_a_fallback_counts_the_declined_attempt_too():
    # Opus declined after reading the filing; the fallback model then answered.
    # Top-level usage is only the fallback's attempt; iterations has both.
    usage = SimpleNamespace(
        input_tokens=28_000, output_tokens=500,
        iterations=[
            SimpleNamespace(type="message", input_tokens=28_000, output_tokens=40),
            SimpleNamespace(type="fallback_message", input_tokens=28_000, output_tokens=500),
        ],
    )
    assert _tokens(usage) == (56_000, 540)


def test_tokens_with_empty_iterations_is_the_top_level_usage():
    usage = SimpleNamespace(input_tokens=100, output_tokens=7, iterations=[])
    assert _tokens(usage) == (100, 7)


# --- _retry_note ---------------------------------------------------------------------

def test_retry_note_holds_the_failed_answer_and_every_problem():
    note = _retry_note(summary(), ["summary: '25' is not in any quote", "8-K: quote not found"])
    assert summary().model_dump_json(indent=2) in note
    assert "- summary: '25' is not in any quote\n- 8-K: quote not found" in note


def test_retry_note_asks_to_quote_a_missing_number_not_drop_it():
    # The CleanSpark case: v2's note said "keep only numbers that are in your
    # quotes", and the retry dropped $814.9M of bitcoin that was in the filing.
    note = _retry_note(summary(), ["summary: '814.9' is not in any quote"])
    assert "add a key number with the sentence from the documents that contains it" in note
    assert "keep only numbers" not in note
    assert f"already have {MAX_KEY_NUMBERS} key numbers" in note  # the limit, not a stale 5
    assert "in your summary or bullets isn't in any quote" in note  # bullets get the same fix


# --- summarize_checked: check, and retry once with the problems ----------------------

BAD = summary("Revenue was $57.0 billion on October 25.")  # 25 is in no quote
BAD_PROBLEMS = ["summary: '25' is not in any quote"]


def test_summarize_checked_passing_first_time_makes_one_call():
    client = FakeClient(answer(parsed_output=summary(), input_tokens=100, output_tokens=7))
    assert summarize_checked(event(), DOCS, client) == SummaryResult(summary(), "claude-opus-5-5", 100, 7)
    assert len(client.calls) == 1


def hut_event(**fields):
    return Event(
        id="edgar:0001104659-26-127278", source="edgar", kind="SCHEDULE 13D/A", ticker="HUT",
        occurred_at=datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc),
        title="HUT SCHEDULE 13D/A (amended): Activist / >5% stake", url="https://www.sec.gov/",
        fields=fields,
    )


HUT_SUMMARY = summary("Michael Ho received 1,517,367 Hut 8 shares.", [SHARES])


def test_summarize_checked_uses_the_events_company_name():
    client = FakeClient(answer(parsed_output=HUT_SUMMARY))
    summarize_checked(hut_event(company="Hut 8 Corp."), HUT_DOCS, client)
    assert len(client.calls) == 1  # "Hut 8" passed: no retry
    # An event saved before names were collected: the old retry, then the old failure.
    client = FakeClient(answer(parsed_output=HUT_SUMMARY), answer(parsed_output=HUT_SUMMARY))
    with pytest.raises(SummaryError, match="'8' is not in any quote"):
        summarize_checked(hut_event(), HUT_DOCS, client)


def test_summarize_checked_uses_the_name_on_the_retry_too():
    # The first try fails for another reason; the retry, with "Hut 8", must pass.
    unquoted = summary("Michael Ho received 2,000,000 Hut 8 shares.", [SHARES])
    client = FakeClient(answer(parsed_output=unquoted), answer(parsed_output=HUT_SUMMARY))
    result = summarize_checked(hut_event(company="Hut 8 Corp."), HUT_DOCS, client)
    assert result.summary == HUT_SUMMARY and len(client.calls) == 2
    assert "'8'" not in client.calls[1]["messages"][0]["content"]  # not named as a problem


def test_summarize_checked_retries_with_the_problems_and_adds_up_both_calls():
    client = FakeClient(
        answer(parsed_output=BAD, input_tokens=100, output_tokens=7),
        answer(parsed_output=summary(), model="claude-fallback-model", input_tokens=200, output_tokens=9),
    )
    result = summarize_checked(event(), DOCS, client)
    # The second summary, from whichever model wrote it, with both calls' tokens.
    assert result == SummaryResult(summary(), "claude-fallback-model", 300, 16)
    retry = client.calls[1]["messages"][0]["content"]
    assert retry == _message_text(event(), DOCS) + "\n\n" + _retry_note(BAD, BAD_PROBLEMS)


def test_summarize_checked_failing_twice_raises_with_the_problems_and_both_calls():
    client = FakeClient(
        answer(parsed_output=BAD, input_tokens=100, output_tokens=7),
        answer(parsed_output=BAD, input_tokens=200, output_tokens=9),
    )
    with pytest.raises(SummaryError, match="failed its checks twice") as error:
        summarize_checked(event(), DOCS, client)
    assert event().id in str(error.value)
    assert BAD_PROBLEMS[0] in str(error.value)
    assert (error.value.input_tokens, error.value.output_tokens) == (300, 16)


def test_summarize_checked_does_not_retry_a_refusal():
    client = FakeClient(answer("refusal", input_tokens=100, output_tokens=7))
    with pytest.raises(SummaryError, match="refused") as error:
        summarize_checked(event(), DOCS, client)
    assert len(client.calls) == 1
    assert (error.value.input_tokens, error.value.output_tokens) == (100, 7)


def test_summarize_checked_retry_that_is_refused_counts_both_calls():
    client = FakeClient(
        answer(parsed_output=BAD, input_tokens=100, output_tokens=7),
        answer("refusal", input_tokens=200, output_tokens=9),
    )
    with pytest.raises(SummaryError, match="refused") as error:
        summarize_checked(event(), DOCS, client)
    assert (error.value.input_tokens, error.value.output_tokens) == (300, 16)


# --- dollars -------------------------------------------------------------------------

def test_dollars_at_the_models_prices():
    assert dollars(1_000_000, 0) == pytest.approx(summarize_module.INPUT_DOLLARS_PER_MTOK)
    assert dollars(0, 1_000_000) == pytest.approx(summarize_module.OUTPUT_DOLLARS_PER_MTOK)
    assert dollars(28_131, 496) == pytest.approx(0.122444)  # NVDA's live run: $0.12


def test_the_prompt_names_the_note_trimming_adds():
    # Same constant on both sides: the note the model is told about is the one it sees.
    from daily_digest_desk.ingestion.edgar_text import CUT_NOTE, trim_documents
    prompt = " ".join(summarize_module.SYSTEM_PROMPT.split())
    assert f'ends with "{CUT_NOTE}"' in prompt
    assert trim_documents([("8-K", "abcdef")], max_chars=3)["8-K"].endswith(CUT_NOTE)
