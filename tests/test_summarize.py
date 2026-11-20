from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from daily_digest_desk.models import Event
from daily_digest_desk.reasoning import summarize as summarize_module
from daily_digest_desk.reasoning.summarize import (
    MAX_KEY_NUMBERS, MAX_WORDS, KeyNumber, Summary, SummaryError, SummaryResult,
    _message_text, _normalize, _NUMBER, _retry_note, _tokens, check_summary, summarize,
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


# --- summarize_checked: check, and retry once with the problems ----------------------

BAD = summary("Revenue was $57.0 billion on October 25.")  # 25 is in no quote
BAD_PROBLEMS = ["summary: '25' is not in any quote"]


def test_summarize_checked_passing_first_time_makes_one_call():
    client = FakeClient(answer(parsed_output=summary(), input_tokens=100, output_tokens=7))
    assert summarize_checked(event(), DOCS, client) == SummaryResult(summary(), "claude-opus-5-5", 100, 7)
    assert len(client.calls) == 1


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
