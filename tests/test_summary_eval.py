import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

import pytest

from daily_digest_desk.reasoning import summary_eval
from daily_digest_desk.reasoning.summary_eval import Case, find_filing, load_cases, load_filing


@dataclass
class FakeFiling:
    """The EntityFiling attributes filing_to_event and find_filing read."""

    accession_no: str = "0001213900-26-069269"
    form: str = "8-K"
    filing_date: date = date(2026, 6, 16)
    items: str = "4.01,9.01"
    acceptance_datetime: datetime | None = datetime(2026, 6, 16, 20, 17, 23, tzinfo=timezone.utc)
    homepage_url: str = "https://www.sec.gov/index.html"
    filing_url: str = "https://www.sec.gov/doc.htm"

    @property
    def accession_number(self):
        return self.accession_no


CASE = Case(
    id="keel-8k-auditor-jun16", ticker="KEEL", accession="0001213900-26-069269",
    form="8-K", filed="2026-06-16", tags=["red-flag"],
)
DOCUMENTS = [("8-K", "Item 4.01 Changes in Registrant’s Certifying Accountant…"), ("EX-99.1", "")]


def never(*args):
    raise AssertionError("SEC must not be asked: the filing is saved")


# --- load_cases ----------------------------------------------------------------

def test_the_approved_cases_load():
    # 20 approved Nov 21, 2026, plus 2 live mistakes (CLSK, Nov 24) added Nov 25.
    cases = load_cases()
    assert len(cases) == 22
    assert len({c.accession for c in cases}) == 22
    assert all(c.tags and c.form and c.filed for c in cases)


def write_cases(tmp_path, *blocks):
    path = tmp_path / "cases.toml"
    path.write_text("\n".join(blocks))
    return path


def case_block(id, accession, note=None):
    lines = ["[[case]]", f'id = "{id}"', 'ticker = "KEEL"', f'accession = "{accession}"',
             'form = "8-K"', 'filed = "2026-06-16"', 'tags = ["x"]']
    if note:
        lines.append(f'note = "{note}"')
    return "\n".join(lines) + "\n"


def test_load_cases_reads_each_field_and_the_note_is_optional(tmp_path):
    cases = load_cases(write_cases(tmp_path, case_block("a", "1", note="look here"), case_block("b", "2")))
    assert cases[0] == Case("a", "KEEL", "1", "8-K", "2026-06-16", ["x"], "look here")
    assert cases[1].note == ""


def test_load_cases_rejects_a_repeated_id(tmp_path):
    with pytest.raises(ValueError, match="Repeated id.*: a"):
        load_cases(write_cases(tmp_path, case_block("a", "1"), case_block("a", "2")))


def test_load_cases_rejects_a_repeated_filing(tmp_path):
    with pytest.raises(ValueError, match="Repeated accession.*: 1"):
        load_cases(write_cases(tmp_path, case_block("a", "1"), case_block("b", "1")))


def test_load_cases_rejects_an_unknown_field(tmp_path):
    # A typo like "acession" must fail, not be silently dropped.
    with pytest.raises(TypeError):
        load_cases(write_cases(tmp_path, case_block("a", "1") + 'acession = "2"\n'))


# --- find_filing -----------------------------------------------------------------

class FakeCompany:
    def __init__(self, filings):
        self.filings = filings
        self.asked = None

    def get_filings(self, form, filing_date):
        self.asked = (form, filing_date)
        return self.filings


def test_find_filing_asks_for_that_form_on_that_day_and_picks_the_accession(monkeypatch):
    other, wanted = FakeFiling(accession_no="0001213900-26-000001"), FakeFiling()
    company = FakeCompany([other, wanted])
    monkeypatch.setattr(summary_eval, "_company", lambda ticker: company)
    assert find_filing(CASE) is wanted
    assert company.asked == (["8-K"], "2026-06-16:2026-06-16")


def test_find_filing_raises_when_the_filing_is_not_there(monkeypatch):
    monkeypatch.setattr(summary_eval, "_company", lambda ticker: FakeCompany([FakeFiling(accession_no="x")]))
    with pytest.raises(ValueError, match="keel-8k-auditor-jun16: no 8-K"):
        find_filing(CASE)


# --- load_filing -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_sec_name_lookup(monkeypatch):
    # load_filing asks SEC for the company's name when it has none: never in a test.
    monkeypatch.setattr(summary_eval, "company_name", lambda ticker: f"{ticker} Corp.")


def test_first_load_saves_the_company_name(tmp_path):
    asked = []
    event, _ = load_filing(CASE, tmp_path, find=lambda c: FakeFiling(), fetch=lambda a, f: DOCUMENTS,
                           name=lambda t: asked.append(t) or "Keel Infrastructure Corp.")
    assert event.fields["company"] == "Keel Infrastructure Corp." and asked == ["KEEL"]
    saved = json.loads((tmp_path / "keel-8k-auditor-jun16.json").read_text(encoding="utf-8"))
    assert saved["event"]["fields"]["company"] == "Keel Infrastructure Corp."


def test_a_copy_saved_before_names_gets_one_once(tmp_path):
    load_filing(CASE, tmp_path, find=lambda c: FakeFiling(), fetch=lambda a, f: DOCUMENTS)
    path = tmp_path / "keel-8k-auditor-jun16.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    del old["event"]["fields"]["company"]  # as the 20 cases were saved before Nov 23
    path.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")

    asked = []
    event, documents = load_filing(CASE, tmp_path, find=never, fetch=never,
                                   name=lambda t: asked.append(t) or "Keel Infrastructure Corp.")
    assert event.fields["company"] == "Keel Infrastructure Corp." and asked == ["KEEL"]
    assert documents == DOCUMENTS
    # Saved with the name, the rest as it was: the next load asks nobody.
    new = json.loads(path.read_text(encoding="utf-8"))
    assert new == {**old, "event": {**old["event"], "fields": {**old["event"]["fields"],
                                                              "company": "Keel Infrastructure Corp."}}}
    again, _ = load_filing(CASE, tmp_path, find=never, fetch=never, name=never)
    assert again == event


def test_load_filing_looks_the_name_up_when_called(tmp_path):
    # The default is found at call time, so the fixture above really stops SEC.
    event, _ = load_filing(CASE, tmp_path, find=lambda c: FakeFiling(), fetch=lambda a, f: DOCUMENTS)
    assert event.fields["company"] == "KEEL Corp."


def test_first_load_asks_sec_and_saves_the_filing(tmp_path):
    asked = []
    event, documents = load_filing(
        CASE, tmp_path,
        find=lambda case: asked.append(case.id) or FakeFiling(),
        fetch=lambda accession, form: asked.append((accession, form)) or DOCUMENTS,
    )
    assert asked == ["keel-8k-auditor-jun16", ("0001213900-26-069269", "8-K")]
    # The same Event a daily run would make: filing_to_event's title and id.
    assert event.id == "edgar:0001213900-26-069269"
    assert event.title == "KEEL 8-K: Auditor change"
    assert documents == DOCUMENTS
    assert (tmp_path / "keel-8k-auditor-jun16.json").exists()


def test_a_saved_filing_is_read_from_disk_unchanged(tmp_path):
    first = load_filing(CASE, tmp_path, find=lambda c: FakeFiling(), fetch=lambda a, f: DOCUMENTS)
    second = load_filing(CASE, tmp_path, find=never, fetch=never)
    assert second == first  # the same Event (time zone included) and documents
    assert second[0].occurred_at == datetime(2026, 6, 16, 20, 17, 23, tzinfo=timezone.utc)


def test_the_saved_file_keeps_text_as_written(tmp_path):
    load_filing(CASE, tmp_path, find=lambda c: FakeFiling(), fetch=lambda a, f: DOCUMENTS)
    raw = (tmp_path / "keel-8k-auditor-jun16.json").read_text(encoding="utf-8")
    assert "Registrant’s" in raw  # not ’: readable when you open the file


def test_a_saved_filing_for_another_accession_is_refused(tmp_path):
    load_filing(CASE, tmp_path, find=lambda c: FakeFiling(), fetch=lambda a, f: DOCUMENTS)
    moved = Case(**{**CASE.__dict__, "accession": "0001213900-26-999999"})
    with pytest.raises(ValueError, match="delete it"):
        load_filing(moved, tmp_path, find=never, fetch=never)


def test_a_filing_with_no_text_is_refused_and_not_saved(tmp_path):
    with pytest.raises(ValueError, match="no text to summarize"):
        load_filing(CASE, tmp_path, find=lambda c: FakeFiling(), fetch=lambda a, f: [("8-K", ""), ("EX-99.1", "")])
    assert not (tmp_path / "keel-8k-auditor-jun16.json").exists()


def test_documents_are_saved_untrimmed(tmp_path):
    from daily_digest_desk.ingestion.edgar_text import MAX_CHARS
    long = [("424B5", "x" * (MAX_CHARS + 5_000))]  # past what trim_documents keeps
    load_filing(CASE, tmp_path, find=lambda c: FakeFiling(), fetch=lambda a, f: long)
    saved = json.loads((tmp_path / "keel-8k-auditor-jun16.json").read_text(encoding="utf-8"))
    assert len(saved["documents"][0][1]) == MAX_CHARS + 5_000


# --- running the summaries ----------------------------------------------------------

from daily_digest_desk.ingestion.edgar_text import CUT_NOTE, MAX_CHARS  # noqa: E402
from daily_digest_desk.reasoning.summarize import KeyNumber, SummaryError, dollars  # noqa: E402
from daily_digest_desk.reasoning.summary_eval import (  # noqa: E402
    CountingClient, is_bare_quote, run_eval,
)
from test_summarize import BAD, BAD_PROBLEMS, DOCS, REVENUE, FakeClient, answer, event, summary  # noqa: E402

FILING = (event(), list(DOCS.items()))  # what load_filing gives: untrimmed (type, text) pairs


def case(id):
    return Case(id=id, ticker="NVDA", accession=f"acc-{id}", form="8-K", filed="2026-11-19", tags=["earnings"])


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_counting_client_counts_keeps_each_answer_and_passes_everything_through():
    inner = FakeClient(answer(parsed_output=BAD), answer("refusal"))
    counting = CountingClient(inner)
    response = counting.beta.messages.parse(model="m", messages=[])
    counting.beta.messages.parse(model="m2", messages=[])
    assert counting.calls == 2
    assert response.parsed_output == BAD
    assert counting.answers == [BAD, None]  # a refusal has no answer
    assert [c["model"] for c in inner.calls] == ["m", "m2"]


@pytest.mark.parametrize("number, quote, bare", [
    ("6.45", "6.45", True),                       # HUT's: only the number
    ("6.45", "6.45%", True),
    ("6.45", "percent of class 6.45", False),     # 3 other words: enough to say what it is
    ("6.45", "of class 6.45", True),              # 2 are not
    ("$57.0 billion", "Revenue was $57.0 billion, up 62% from a year ago.", False),
    ("$57.0 billion", "Revenue\n  was $57.0\nbillion", True),  # line breaks don't hide the number
])
def test_is_bare_quote(number, quote, bare):
    assert is_bare_quote(KeyNumber(number=number, quote=quote, source="8-K")) is bare


def test_a_good_summary_is_saved_with_its_numbers(tmp_path):
    client = FakeClient(answer(parsed_output=summary(), input_tokens=28_000, output_tokens=500))
    report = run_eval([case("a")], tmp_path, client, load=lambda c: FILING)
    [row] = lines(tmp_path / "results.jsonl")
    assert report.done == ["a"] and report.failed == []
    assert row["id"] == "a" and row["tags"] == ["earnings"]
    assert row["title"] == event().title and row["url"] == event().url
    assert row["summary"] == summary().summary
    assert row["key_numbers"] == [REVENUE.model_dump()]
    assert (row["calls"], row["retried"]) == (1, False)
    assert (row["input_tokens"], row["output_tokens"]) == (28_000, 500)
    assert row["dollars"] == round(dollars(28_000, 500), 4) and row["dollars"] > 0
    assert row["words"] == 8 and row["key_number_count"] == 1 and row["bare_quotes"] == 0
    assert row["model"] == "claude-opus-5-5"
    assert report.dollars == pytest.approx(dollars(28_000, 500))


def test_a_retry_is_counted(tmp_path):
    # The real summarize_checked: the first answer fails the code checks, the second passes.
    client = FakeClient(
        answer(parsed_output=BAD, input_tokens=100, output_tokens=7),
        answer(parsed_output=summary(), input_tokens=200, output_tokens=9),
    )
    run_eval([case("a")], tmp_path, client, load=lambda c: FILING)
    [row] = lines(tmp_path / "results.jsonl")
    assert (row["calls"], row["retried"]) == (2, True)
    assert (row["input_tokens"], row["output_tokens"]) == (300, 16)  # both calls
    assert row["first_try_problems"] == BAD_PROBLEMS  # why it was retried


def test_no_retry_means_no_first_try_problems(tmp_path):
    run_eval([case("a")], tmp_path, FakeClient(answer(parsed_output=summary())), load=lambda c: FILING)
    [row] = lines(tmp_path / "results.jsonl")
    assert row["first_try_problems"] == []


def test_a_summary_that_fails_goes_to_errors_with_its_cost(tmp_path):
    client = FakeClient(
        answer(parsed_output=BAD, input_tokens=100, output_tokens=7),
        answer(parsed_output=BAD, input_tokens=200, output_tokens=9),
    )
    report = run_eval([case("a")], tmp_path, client, load=lambda c: FILING)
    assert lines(tmp_path / "results.jsonl") == []
    [error] = lines(tmp_path / "errors.jsonl")
    assert error["kind"] == "summary" and "failed its checks twice" in error["error"]
    assert error["first_try_problems"] == BAD_PROBLEMS
    assert (error["calls"], error["input_tokens"], error["output_tokens"]) == (2, 300, 16)
    assert report.failed == ["a"] and report.dollars == pytest.approx(dollars(300, 16))


def test_a_failed_summary_keeps_what_the_model_wrote(tmp_path):
    # Both answers, in order, so the rejected numbers can be read in context.
    retry = BAD.model_copy(update={"summary": "Revenue was $57.0 billion, up 26%."})
    client = FakeClient(answer(parsed_output=BAD), answer(parsed_output=retry))
    run_eval([case("a")], tmp_path, client, load=lambda c: FILING)
    [error] = lines(tmp_path / "errors.jsonl")
    assert error["answers"] == [BAD.model_dump(), retry.model_dump()]


def test_a_refusal_is_kept_as_no_answer(tmp_path):
    run_eval([case("a")], tmp_path, FakeClient(answer("refusal")), load=lambda c: FILING)
    [error] = lines(tmp_path / "errors.jsonl")
    assert error["answers"] == [None]


def test_a_case_that_cannot_load_is_recorded_and_the_next_still_runs(tmp_path):
    def load(c):
        if c.id == "a":
            raise ValueError("SEC is down")
        return FILING
    client = FakeClient(answer(parsed_output=summary()))
    report = run_eval([case("a"), case("b")], tmp_path, client, load=load)
    [error] = lines(tmp_path / "errors.jsonl")
    assert (error["id"], error["kind"], error["calls"]) == ("a", "other", 0)
    assert "ValueError: SEC is down" in error["error"]
    assert [r["id"] for r in lines(tmp_path / "results.jsonl")] == ["b"]
    assert report.failed == ["a"] and report.done == ["b"]


def test_running_again_skips_what_is_done_and_retries_what_failed(tmp_path):
    first = FakeClient(answer(parsed_output=summary()), answer("refusal"))
    run_eval([case("a"), case("b")], tmp_path, first, load=lambda c: FILING)
    second = FakeClient(answer(parsed_output=summary()))
    report = run_eval([case("a"), case("b")], tmp_path, second, load=lambda c: FILING)
    assert report.skipped == 1 and report.done == ["b"]
    assert len(second.calls) == 1  # only b was asked again
    assert [r["id"] for r in lines(tmp_path / "results.jsonl")] == ["a", "b"]


def test_the_cap_stops_before_the_next_case(tmp_path):
    client = FakeClient(*[answer(parsed_output=summary(), input_tokens=28_000, output_tokens=500)] * 3)
    report = run_eval([case("a"), case("b"), case("c")], tmp_path, client,
                      cap=dollars(28_000, 500) * 1.5, load=lambda c: FILING)
    assert report.done == ["a", "b"] and report.left == 1  # over the cap by at most one case
    assert len(client.calls) == 2


def test_documents_are_trimmed_as_in_a_daily_run(tmp_path):
    seen = {}
    def fake_summarize(evt, documents, client):
        seen.update(documents)
        raise SummaryError("stop here")
    long = (event(), [("424B5", "x" * (MAX_CHARS + 5_000))])
    run_eval([case("a")], tmp_path, FakeClient(), load=lambda c: long, summarize=fake_summarize)
    assert seen["424B5"] == "x" * MAX_CHARS + "\n\n" + CUT_NOTE  # trimmed, and says so


def test_a_crash_keeps_the_cases_already_finished(tmp_path):
    # Ctrl-C isn't an Exception, so it isn't recorded: it stops the run. Case a
    # was written the moment it finished, so it survives.
    def load(c):
        if c.id == "b":
            raise KeyboardInterrupt
        return FILING
    with pytest.raises(KeyboardInterrupt):
        run_eval([case("a"), case("b")], tmp_path, FakeClient(answer(parsed_output=summary())), load=load)
    assert [r["id"] for r in lines(tmp_path / "results.jsonl")] == ["a"]


def test_run_json_records_the_setup_and_refuses_a_changed_one(tmp_path, monkeypatch):
    run_eval([], tmp_path, FakeClient())
    setup = json.loads((tmp_path / "run.json").read_text())
    assert setup["prompt_version"] == summary_eval.PROMPT_VERSION
    assert setup["model"] == summary_eval.MODEL and setup["max_chars"] == MAX_CHARS
    monkeypatch.setattr(summary_eval, "PROMPT_VERSION", summary_eval.PROMPT_VERSION + 1)
    with pytest.raises(ValueError, match="different setup"):
        run_eval([case("a")], tmp_path, FakeClient(), load=lambda c: FILING)


def test_a_refusal_has_no_first_try_problems(tmp_path):
    run_eval([case("a")], tmp_path, FakeClient(answer("refusal")), load=lambda c: FILING)
    [error] = lines(tmp_path / "errors.jsonl")
    assert (error["calls"], error["first_try_problems"]) == (1, [])


def test_first_try_problems_are_checked_against_the_trimmed_documents(tmp_path):
    # A quote past the trim limit wasn't in what the model read: a real problem.
    quote = "Revenue was $57.0 billion, up 62% from a year ago."
    far = (event(), [("EX-99.1", "x " * MAX_CHARS + quote)])
    late = summary(key_numbers=[KeyNumber(number="$57.0 billion", quote=quote, source="EX-99.1")])
    client = FakeClient(answer(parsed_output=late), answer(parsed_output=late))
    run_eval([case("a")], tmp_path, client, load=lambda c: far)
    [error] = lines(tmp_path / "errors.jsonl")
    assert error["first_try_problems"] == [f"EX-99.1: quote not found: {quote!r}"]


# --- the grading sheet ----------------------------------------------------------------

from daily_digest_desk.reasoning.summary_eval import CHECKS, MARKS, allowed_marks, write_sheet  # noqa: E402


def result_row(id, **overrides):
    row = {
        "id": id, "tags": ["earnings"], "ticker": "NVDA", "form": "8-K",
        "title": "NVDA 8-K: Earnings results", "url": "https://www.sec.gov/x",
        "summary": "NVIDIA reported revenue of $57.0 billion, up 62%.",
        "key_numbers": [REVENUE.model_dump()], "model": "claude-opus-5-5",
        "calls": 1, "retried": False, "first_try_problems": [],
        "words": 8, "key_number_count": 1, "bare_quotes": 0,
        "input_tokens": 100, "output_tokens": 10, "dollars": 0.0123, "seconds": 1.0,
    }
    return {**row, **overrides}


def write_results(run, *rows):
    run.mkdir(exist_ok=True)
    with (run / "results.jsonl").open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def sections(sheet_text):
    """The sheet split at its "## id" headings: {id: that case's text}."""
    parts = sheet_text.split("\n## ")[1:]
    return {p.split("\n", 1)[0]: p for p in parts}


def test_the_sheet_has_a_section_per_result_with_blank_marks(tmp_path):
    write_results(tmp_path, result_row("a"))
    assert write_sheet([case("a")], tmp_path) == 1
    text = (tmp_path / "grading.md").read_text(encoding="utf-8")
    assert text.startswith(f"# Grading sheet: {tmp_path.name}")
    section = sections(text)["a"]
    assert "**NVDA 8-K: Earnings results** · [the filing on SEC](https://www.sec.gov/x)" in section
    assert "> NVIDIA reported revenue of $57.0 billion, up 62%." in section
    assert "- `$57.0 billion` (EX-99.1): Revenue was $57.0 billion, up 62% from a year ago." in section
    assert "8 words · 0 bullets · 1 key numbers, 0 bare quotes · 1 call · $0.01 · claude-opus-5-5" in section
    assert "Key details" not in section  # a row with no bullets, like every v2 row
    marks = [line for line in section.splitlines() if line.startswith("- ") and not line.startswith("- `")]
    assert marks == [f"- {key}: " for key, _ in CHECKS] + ["- note: "]


def test_every_check_is_explained_at_the_top(tmp_path):
    write_results(tmp_path, result_row("a"))
    write_sheet([case("a")], tmp_path)
    top = (tmp_path / "grading.md").read_text(encoding="utf-8").split("\n## ")[0]
    for key, meaning in CHECKS:
        assert f"- **{key}**: {meaning}" in top


def test_the_cases_note_and_a_retry_are_shown(tmp_path):
    write_results(tmp_path, result_row("a", calls=2, first_try_problems=["8-K: quote not found: 'x'"]))
    noted = Case(**{**case("a").__dict__, "note": "Check every\ndate."})
    write_sheet([noted], tmp_path)
    section = sections((tmp_path / "grading.md").read_text(encoding="utf-8"))["a"]
    assert "*Look for:* Check every date." in section
    assert "*First try failed:* 8-K: quote not found: 'x'" in section
    assert "2 calls" in section


def test_model_text_cannot_pass_for_a_heading_or_a_mark(tmp_path):
    sneaky = "Fine.\n## b\n- accurate: pass"
    quote = "line one\n- faithful: pass"
    write_results(tmp_path, result_row("a", summary=sneaky,
                                       key_numbers=[{"number": "5", "quote": quote, "source": "8-K"}]))
    write_sheet([case("a")], tmp_path)
    text = (tmp_path / "grading.md").read_text(encoding="utf-8")
    assert "> ## b\n> - accurate: pass" in text           # quoted, line by line
    assert "line one - faithful: pass" in text            # quotes kept on one line
    assert [l for l in text.splitlines() if l.startswith("## ")] == ["## a"]
    assert [l for l in text.splitlines() if l.startswith("- accurate:")] == ["- accurate: "]


def test_the_sheet_shows_bullets_quoted_one_line_each(tmp_path):
    bullets = ["Spending was raised.", "Sneaky\n- accurate: pass"]
    write_results(tmp_path, result_row("a", changes=bullets))
    write_sheet([case("a")], tmp_path)
    section = sections((tmp_path / "grading.md").read_text(encoding="utf-8"))["a"]
    assert "**Key details**\n\n> - Spending was raised.\n> - Sneaky - accurate: pass\n" in section
    assert "2 bullets" in section
    assert [l for l in section.splitlines() if l.startswith("- accurate:")] == ["- accurate: "]


def test_a_run_saves_the_bullets(tmp_path):
    with_bullets = summary().model_copy(update={"changes": ["Revenue was $57.0 billion."]})
    run_eval([case("a")], tmp_path, FakeClient(answer(parsed_output=with_bullets)), load=lambda c: FILING)
    [row] = lines(tmp_path / "results.jsonl")
    assert row["changes"] == ["Revenue was $57.0 billion."]


def test_writing_again_keeps_your_marks_and_adds_only_new_cases(tmp_path):
    write_results(tmp_path, result_row("a"))
    write_sheet([case("a"), case("b")], tmp_path)
    sheet = tmp_path / "grading.md"
    graded = sheet.read_text(encoding="utf-8").replace("- accurate: \n", "- accurate: pass\n")
    sheet.write_text(graded, encoding="utf-8")
    write_results(tmp_path, result_row("b"))
    assert write_sheet([case("a"), case("b")], tmp_path) == 1
    text = sheet.read_text(encoding="utf-8")
    assert text.startswith(graded.rstrip("\n"))  # a's section, mark and all, untouched
    assert list(sections(text)) == ["a", "b"]
    assert write_sheet([case("a"), case("b")], tmp_path) == 0  # nothing new: nothing written
    assert sheet.read_text(encoding="utf-8") == text


def test_sections_follow_the_case_order_not_the_finishing_order(tmp_path):
    write_results(tmp_path, result_row("b"), result_row("a"))
    write_sheet([case("a"), case("b")], tmp_path)
    assert list(sections((tmp_path / "grading.md").read_text(encoding="utf-8"))) == ["a", "b"]


def test_a_result_for_a_case_no_longer_listed_still_gets_a_section(tmp_path):
    write_results(tmp_path, result_row("gone"))
    write_sheet([case("a")], tmp_path)
    assert "gone" in sections((tmp_path / "grading.md").read_text(encoding="utf-8"))


def test_a_summary_with_no_key_numbers_has_no_key_numbers_part(tmp_path):
    write_results(tmp_path, result_row("a", key_numbers=[], key_number_count=0))
    write_sheet([case("a")], tmp_path)
    assert "**Key numbers**" not in (tmp_path / "grading.md").read_text(encoding="utf-8")


def test_only_main_point_and_focused_can_be_partial():
    assert allowed_marks("main point") == ["pass", "partial", "fail"]
    assert allowed_marks("focused") == ["pass", "partial", "fail"]
    for key, _ in CHECKS:
        if key not in ("main point", "focused"):
            assert allowed_marks(key) == ["pass", "fail"]
    assert MARKS == {"pass": 1.0, "partial": 0.5, "fail": 0.0}


def test_the_sheet_says_which_checks_can_be_partial(tmp_path):
    write_results(tmp_path, result_row("a"))
    write_sheet([case("a")], tmp_path)
    top = (tmp_path / "grading.md").read_text(encoding="utf-8").split("\n## ")[0]
    assert "(main point and focused can also be `partial`)" in top
    assert "`partial` if the main point is there but something important is left out" in top
    assert "`partial` if one bullet or clause is filler" in top


# --- scoring ----------------------------------------------------------------------------

from daily_digest_desk.reasoning.summary_eval import read_marks, score, score_marks  # noqa: E402


def graded_sheet(tmp_path, *marks_by_case):
    """A real sheet with marks filled in: (id, {check: mark}) pairs."""
    write_results(tmp_path, *(result_row(case_id) for case_id, _ in marks_by_case))
    write_sheet([case(case_id) for case_id, _ in marks_by_case], tmp_path)
    sheet = tmp_path / "grading.md"
    text = sheet.read_text(encoding="utf-8")
    for case_id, marks in marks_by_case:
        head, _, rest = text.partition(f"## {case_id}\n")
        section, sep, tail = rest.partition("\n## ")
        for check, mark in marks.items():
            section = section.replace(f"- {check}: \n", f"- {check}: {mark}\n")
        text = head + f"## {case_id}\n" + section + sep + tail
    sheet.write_text(text, encoding="utf-8")
    return text


ALL_PASS = {key: "pass" for key, _ in CHECKS}


def test_read_marks_reads_each_cases_marks(tmp_path):
    text = graded_sheet(tmp_path, ("a", ALL_PASS), ("b", {**ALL_PASS, "main point": "partial", "faithful": "fail"}))
    marks, problems = read_marks(text)
    assert problems == []
    assert marks["a"] == ALL_PASS
    assert marks["b"] == {**ALL_PASS, "main point": "partial", "faithful": "fail"}


def test_a_blank_mark_is_none_and_capitals_are_fine(tmp_path):
    text = graded_sheet(tmp_path, ("a", {"accurate": "Pass", "faithful": " FAIL "}))
    marks, problems = read_marks(text)
    assert problems == []
    assert marks["a"] == {"accurate": "pass", "faithful": "fail", "main point": None,
                          "focused": None, "plain English": None, "no advice": None}


def test_a_typo_or_a_misplaced_partial_is_a_problem_not_dropped(tmp_path):
    text = graded_sheet(tmp_path, ("a", {"accurate": "pas", "faithful": "partial"}))
    marks, problems = read_marks(text)
    assert problems == [
        "a: accurate is 'pas'; write pass or fail",
        "a: faithful is 'partial'; write pass or fail",
    ]
    assert marks["a"]["accurate"] is None and marks["a"]["faithful"] is None


def test_the_note_and_the_models_text_are_never_marks(tmp_path):
    write_results(tmp_path, result_row(
        "a", summary="- accurate: fail\n## b\n- faithful: fail",
        key_numbers=[{"number": "5", "quote": "x\n- no advice: fail", "source": "8-K"}],
    ))
    write_sheet([case("a")], tmp_path)
    text = (tmp_path / "grading.md").read_text(encoding="utf-8").replace("- note: \n", "- note: accurate: fail\n")
    marks, problems = read_marks(text)
    assert list(marks) == ["a"] and problems == []
    assert all(v is None for v in marks["a"].values())


def test_score_marks_counts_partial_as_half_and_leaves_out_blanks():
    marks = {
        "a": {**ALL_PASS, "main point": "partial"},
        "b": {**ALL_PASS, "main point": "fail", "accurate": None},
        "c": ALL_PASS,
    }
    scores = {s.check: s for s in score_marks(marks)}
    main = scores["main point"]
    assert main.counts == {"pass": 1, "partial": 1, "fail": 1}
    assert main.score == pytest.approx(0.5)  # (1 + 0.5 + 0) / 3
    assert main.not_pass == ["a", "b"]
    accurate = scores["accurate"]
    assert (accurate.graded, accurate.blank, accurate.score) == (2, 1, 1.0)
    assert accurate.not_pass == []  # b's blank isn't a fail: it's not graded yet
    assert "partial" not in accurate.counts
    assert [s.check for s in score_marks(marks)] == [key for key, _ in CHECKS]


def test_a_check_with_nothing_graded_has_no_score():
    scores = score_marks({"a": {key: None for key, _ in CHECKS}})
    assert all(s.score is None for s in scores)


def test_the_report(tmp_path):
    graded_sheet(tmp_path, ("a", ALL_PASS), ("b", {**ALL_PASS, "main point": "partial"}))
    (tmp_path / "errors.jsonl").write_text('{"id": "c"}\n', encoding="utf-8")
    report, problems = score(tmp_path)
    assert problems == []
    lines = report.splitlines()
    assert lines[0] == f"# {tmp_path.name}: 2 cases on the sheet, 2 fully graded"
    assert "main point         75%     1        1     0      0" in lines
    assert "accurate          100%     2        -     0      0" in lines
    assert "Not a pass on main point: b" in lines
    assert ("Recorded: $0.02 for 2 summaries ($0.01 each) · 0 of 2 retried · 8 words on average · "
            "2 key numbers, 0 bare quotes · 1 failed (errors.jsonl)") in lines
    assert lines[-1] == "With 2 cases, one case moves a check's score by 50%."
    assert not any(l.startswith("Bullets in") for l in lines)  # no bullets, no line


def test_the_report_names_the_cases_with_bullets(tmp_path):
    # The padding signal: a routine case with bullets shows up by name.
    write_results(tmp_path, result_row("a", changes=["One.", "Two."]), result_row("b"),
                  result_row("c", changes=["Three."]))
    write_sheet([case("a"), case("b"), case("c")], tmp_path)
    report, _ = score(tmp_path)
    assert "Bullets in 2 of 3 cases (3 in all): a, c" in report.splitlines()


def test_the_report_returns_the_problems_too(tmp_path):
    graded_sheet(tmp_path, ("a", {"accurate": "yes"}))
    _, problems = score(tmp_path)
    assert problems == ["a: accurate is 'yes'; write pass or fail"]


def _drop_lines(tmp_path, prefix: str, case_ids: list[str] | None = None) -> None:
    """Delete the "- {prefix}" lines from the sheet: all of them, or only in these cases."""
    sheet = tmp_path / "grading.md"
    current, kept = None, []
    for line in sheet.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith("## "):
            current = line[3:].strip()
        if line.startswith(f"- {prefix}") and (case_ids is None or current in case_ids):
            continue
        kept.append(line)
    sheet.write_text("".join(kept), encoding="utf-8")


def test_a_sheet_older_than_a_check_still_scores(tmp_path):
    # v2 sheets have no "focused" line anywhere: no row for it, and still fully graded.
    graded_sheet(tmp_path, ("a", ALL_PASS), ("b", {**ALL_PASS, "main point": "partial"}))
    _drop_lines(tmp_path, "focused:")
    marks, problems = read_marks((tmp_path / "grading.md").read_text(encoding="utf-8"))
    assert problems == [] and all("focused" not in m for m in marks.values())
    report, _ = score(tmp_path)
    lines = report.splitlines()
    assert lines[0] == f"# {tmp_path.name}: 2 cases on the sheet, 2 fully graded"
    assert not any(l.startswith("focused") for l in lines)
    assert "main point         75%     1        1     0      0" in lines


def test_a_case_missing_a_checks_line_is_blank_on_it(tmp_path):
    # The other cases have "focused", so a deleted line is a blank, not a quiet skip.
    graded_sheet(tmp_path, ("a", ALL_PASS), ("b", ALL_PASS))
    _drop_lines(tmp_path, "focused:", ["b"])
    marks, _ = read_marks((tmp_path / "grading.md").read_text(encoding="utf-8"))
    assert marks["a"]["focused"] == "pass" and marks["b"]["focused"] is None
    report, _ = score(tmp_path)
    assert report.splitlines()[0] == f"# {tmp_path.name}: 2 cases on the sheet, 1 fully graded"
    assert "focused           100%     1        0     0      1" in report.splitlines()


def test_a_case_with_a_blank_mark_is_not_fully_graded(tmp_path):
    graded_sheet(tmp_path, ("a", ALL_PASS), ("b", {"accurate": "pass"}))
    report, _ = score(tmp_path)
    assert report.splitlines()[0] == f"# {tmp_path.name}: 2 cases on the sheet, 1 fully graded"


# --- first_try_problems: the company's name ------------------------------------------

def test_first_try_problems_use_the_company_name_like_the_retry_did():
    # The reason on the sheet must be the one that caused the retry: with the
    # name known, "Hut 8" was never a problem, so it isn't listed as one.
    from types import SimpleNamespace

    from daily_digest_desk.reasoning.summarize import KeyNumber, Summary
    from daily_digest_desk.reasoning.summary_eval import first_try_problems

    docs = {"SCHEDULE 13D/A": "The Reporting Person received 1,517,367 shares of Common Stock."}
    first = Summary(summary="Michael Ho received 2,000,000 Hut 8 shares.", key_numbers=[KeyNumber(
        number="1,517,367", source="SCHEDULE 13D/A",
        quote="The Reporting Person received 1,517,367 shares of Common Stock.")])
    counting = SimpleNamespace(calls=2, answers=[first, None])
    assert first_try_problems(counting, docs, "Hut 8 Corp.") == ["summary: '2,000,000' is not in any quote"]
    assert first_try_problems(counting, docs) == [
        "summary: '2,000,000' is not in any quote", "summary: '8' is not in any quote",
    ]


def test_first_try_problems_use_the_filing_date_like_the_retry_did():
    from types import SimpleNamespace

    from daily_digest_desk.reasoning.summarize import KeyNumber, Summary
    from daily_digest_desk.reasoning.summary_eval import first_try_problems

    docs = {"8-K": "Revenue was $57.0 billion."}
    first = Summary(summary="Filed November 13, 2026: revenue was $58.0 billion.", key_numbers=[KeyNumber(
        number="$57.0 billion", source="8-K", quote="Revenue was $57.0 billion.")])
    counting = SimpleNamespace(calls=2, answers=[first, None])
    assert first_try_problems(counting, docs, filed=date(2026, 11, 13)) == ["summary: '58.0' is not in any quote"]
