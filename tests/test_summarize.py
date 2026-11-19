from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from daily_digest_desk.models import Event
from daily_digest_desk.reasoning import summarize as summarize_module
from daily_digest_desk.reasoning.summarize import (
    MAX_KEY_NUMBERS, MAX_WORDS, KeyNumber, Summary, SummaryError,
    _message_text, _normalize, _NUMBER, check_summary, summarize,
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


# --- _NUMBER: what counts as a number ------------------------------------------------

def test_number_pattern_finds_amounts():
    text = "Revenue of $57.0 billion, up 62%, 1,200 hires, $1,234.56 million, in 2026."
    assert _NUMBER.findall(text) == ["57.0", "62", "1,200", "1,234.56", "2026"]


def test_number_pattern_skips_labels():
    # Glued to letters or a hyphen: names of things, not amounts.
    assert _NUMBER.findall("This 8-K and 10-Q, EX-99.1, Q3, the 3rd quarter.") == []


# --- _message_text -------------------------------------------------------------------

def test_message_text_has_a_header_then_each_document_in_order():
    docs = {"8-K": "cover", "EX-99.1": "press release"}
    assert _message_text(event(), docs) == (
        "NVDA 8-K, filed 2026-11-19: NVIDIA 8-K (2.02 Results of Operations)\n\n"
        '<document type="8-K">\ncover\n</document>\n\n'
        '<document type="EX-99.1">\npress release\n</document>'
    )


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
    # The live NVDA case: the prompt asks for "the third quarter" instead.
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


# --- summarize: with a fake client ---------------------------------------------------

class FakeClient:
    """Stands in for anthropic.Anthropic(): answers every call with `response`
    and remembers what was sent."""

    def __init__(self, response):
        self.response, self.sent = response, None
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.sent = kwargs
        return self.response


def answer(stop_reason="end_turn", parsed_output=None):
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed_output)


def test_summarize_returns_the_models_summary():
    client = FakeClient(answer(parsed_output=summary()))
    assert summarize(event(), DOCS, client) == summary()


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


@pytest.mark.parametrize("stop_reason, message", [
    ("refusal", "refused"),
    ("max_tokens", "cut off"),
])
def test_summarize_raises_when_the_model_gives_no_usable_answer(stop_reason, message):
    # A refused or cut-off answer is an error even if something was parsed.
    client = FakeClient(answer(stop_reason, parsed_output=summary()))
    with pytest.raises(SummaryError, match=message) as error:
        summarize(event(), DOCS, client)
    assert event().id in str(error.value)


def test_summarize_raises_when_there_is_no_summary_in_the_answer():
    client = FakeClient(answer("end_turn", parsed_output=None))
    with pytest.raises(SummaryError, match="No summary"):
        summarize(event(), DOCS, client)
