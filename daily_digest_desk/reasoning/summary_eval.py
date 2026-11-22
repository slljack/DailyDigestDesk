"""The summary eval: run the real summarizer on a fixed set of filings, so a
prompt or model change can be checked instead of guessed (DESIGN.md section 7).

The filings are in evals/summaries/cases.toml. Each is found on SEC the way the
daily run finds filings, and becomes an Event through the same filing_to_event,
so the model reads exactly what a daily run would show it.

Its documents are saved to disk the first time (evals/summaries/filings/), and
every later run reads them from there: no SEC requests, and the same text every
time. The saved copy is untrimmed; trimming happens when the eval runs, so a
change to trim_documents' limit is part of what the eval measures.

    load_cases(path)           -> list[Case]        (the approved filings)
    load_filing(case, folder)  -> (Event, documents) (from disk, or SEC the first time)
    run_eval(cases, run, client) -> what it did      (summarize each case, save each result)
    write_sheet(cases, run)    -> the grading sheet  (grading.md: one section per result)
    read_marks(sheet text)     -> marks, problems    (your marks, read back)
    score(run)                 -> the report         (each check's score, and the automatic numbers)

Run it (costs money: about $0.10 a filing):
    python -m daily_digest_desk.reasoning.summary_eval RUN_NAME [--only ID,ID] [--cap 3]
    python -m daily_digest_desk.reasoning.summary_eval RUN_NAME --sheet-only   (free)
    python -m daily_digest_desk.reasoning.summary_eval RUN_NAME --score        (free)

Each run gets its own folder, evals/summaries/runs/RUN_NAME/:
    run.json       what was run: prompt version, model, effort, the trim limit
    results.jsonl  one line per summarized case, written as each one finishes
    errors.jsonl   one line per case that failed, with what the model wrote (tried again next time)
    grading.md     the sheet you grade: each summary with five pass/fail marks to fill in
Running the same name again skips the cases already in results.jsonl, so a run
that stopped (a crash, the cap) picks up where it left off.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from daily_digest_desk.ingestion.edgar import _company
from daily_digest_desk.ingestion.edgar_events import filing_to_event
from daily_digest_desk.ingestion.edgar_text import MAX_CHARS, fetch_filing_documents, trim_documents
from daily_digest_desk.models import Event
from daily_digest_desk.reasoning.summarize import (
    EFFORT, MODEL, PROMPT_VERSION, KeyNumber, SummaryError, _normalize, check_summary, dollars,
    summarize_checked,
)

EVAL_DIR = Path(__file__).resolve().parents[2] / "evals" / "summaries"
CASES = EVAL_DIR / "cases.toml"
FILINGS = EVAL_DIR / "filings"  # the saved documents, one file per case
RUNS = EVAL_DIR / "runs"        # one folder per run


@dataclass(frozen=True)
class Case:
    """One approved filing in the eval."""

    id: str          # short and readable: "keel-8k-auditor-jun16"
    ticker: str
    accession: str   # SEC's number for the filing: "0001213900-26-069269"
    form: str        # "8-K", "6-K", "SCHEDULE 13D/A", ...
    filed: str       # SEC's filing date, "2026-06-16"
    tags: list[str] = field(default_factory=list)  # tags[0] groups the results
    note: str = ""   # what to look for when grading it


def load_cases(path: Path = CASES) -> list[Case]:
    """The cases in the file, in order. Raises ValueError on a repeated id or
    filing, so two cases can never quietly share one results line."""
    cases = [Case(**raw) for raw in tomllib.loads(path.read_text(encoding="utf-8"))["case"]]
    for name, values in (("id", [c.id for c in cases]), ("accession", [c.accession for c in cases])):
        repeated = sorted({v for v in values if values.count(v) > 1})
        if repeated:
            raise ValueError(f"Repeated {name} in {path.name}: {', '.join(repeated)}")
    return cases


def find_filing(case: Case):
    """The case's filing from SEC, looked up the way the daily run does: the
    company's filings of that form on that day. That way it carries the same
    details (8-K item numbers, the time it was accepted) a daily run would see."""
    filings = _company(case.ticker).get_filings(form=[case.form], filing_date=f"{case.filed}:{case.filed}")
    for filing in filings:
        if filing.accession_no == case.accession:
            return filing
    raise ValueError(f"{case.id}: no {case.form} {case.accession} from {case.ticker} on {case.filed}")


# (case) -> the filing; (accession, form) -> its documents. Passed in so tests
# can load cases with no network.
FilingFinder = Callable[[Case], object]
DocumentFetcher = Callable[[str, str], list[tuple[str, str]]]


def load_filing(
    case: Case,
    folder: Path = FILINGS,
    find: FilingFinder = find_filing,
    fetch: DocumentFetcher = fetch_filing_documents,
) -> tuple[Event, list[tuple[str, str]]]:
    """The case's Event and its documents (untrimmed), from `folder` if they were
    saved before, else from SEC, and then saved for next time."""
    saved = folder / f"{case.id}.json"
    if saved.exists():
        data = json.loads(saved.read_text(encoding="utf-8"))
        if data["accession"] != case.accession:
            # The case was pointed at another filing since: its old copy is stale.
            raise ValueError(f"{saved} is for {data['accession']}, not {case.accession}; delete it")
        return Event.model_validate(data["event"]), [tuple(d) for d in data["documents"]]

    event = filing_to_event(case.ticker, find(case))
    documents = fetch(case.accession, case.form)
    if not any(text for _, text in documents):
        # Nothing to summarize would make every run fail the same way: say so now.
        raise ValueError(f"{case.id}: the filing has no text to summarize")

    folder.mkdir(parents=True, exist_ok=True)
    saved.write_text(
        json.dumps(
            {"accession": case.accession, "event": event.model_dump(mode="json"), "documents": documents},
            ensure_ascii=False, indent=1,
        ),
        encoding="utf-8",
    )
    return event, documents


# --- Running the summaries ----------------------------------------------------------

class CountingClient:
    """Wraps an anthropic client, counting its calls and keeping each answer.
    summarize_checked only returns the final summary; 2 calls means it needed
    its one retry, and the first answer (kept in `answers`) says why."""

    def __init__(self, client):
        self.calls = 0
        self.answers = []  # each call's Summary, or None if it had none (a refusal)

        def parse(**kwargs):
            self.calls += 1
            response = client.beta.messages.parse(**kwargs)
            self.answers.append(response.parsed_output)
            return response

        # The one path summarize uses: client.beta.messages.parse(...)
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=parse))


def first_try_problems(counting: CountingClient, documents: dict[str, str]) -> list[str]:
    """Why the first answer was retried: the code checks' problems with it, or []
    when there was no retry. The checks run again on the answer the API already
    gave, so this costs nothing."""
    if counting.calls < 2 or counting.answers[0] is None:
        return []
    return check_summary(counting.answers[0], documents)


def is_bare_quote(key_number: KeyNumber) -> bool:
    """True when the quote is little more than the number: fewer than 3 other words.
    HUT's "6.45" is one. It passes the code checks but shows nothing about what
    the number is, so the eval counts them (see the quote check idea, DESIGN.md 7)."""
    rest = _normalize(key_number.quote).replace(_normalize(key_number.number), " ")
    return len(rest.split()) < 3


def _append(path: Path, row: dict) -> None:
    """One JSON line, written and closed at once, so a crash keeps every finished case."""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines() if line}


def _start_run(run: Path) -> None:
    """Write run.json, or check it: one run's results must all come from the same
    prompt, model, effort and trim limit, or comparing them means nothing."""
    setup = {"prompt_version": PROMPT_VERSION, "model": MODEL, "effort": EFFORT, "max_chars": MAX_CHARS}
    path = run / "run.json"
    if path.exists():
        before = json.loads(path.read_text(encoding="utf-8"))
        changed = {k: (before.get(k), v) for k, v in setup.items() if before.get(k) != v}
        if changed:
            raise ValueError(
                f"{run.name} was started with a different setup {changed}; use a new run name"
            )
        return
    run.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps({**setup, "started": started}, indent=1) + "\n", encoding="utf-8")


Loader = Callable[[Case], tuple[Event, list[tuple[str, str]]]]


@dataclass
class EvalRun:
    """What run_eval did, for the report it prints."""

    done: list[str] = field(default_factory=list)    # case ids summarized this time
    failed: list[str] = field(default_factory=list)  # case ids that failed this time
    skipped: int = 0     # already in results.jsonl from before
    left: int = 0        # not tried because the cap was reached
    dollars: float = 0.0  # spent this time, failed calls included


def run_eval(
    cases: list[Case],
    run: Path,
    client,
    cap: float = 3.0,
    load: Loader = load_filing,
    summarize=summarize_checked,
) -> EvalRun:
    """Summarize each case with the real summarize_checked, as a daily run would
    (same trimming, same checks, same retry), and save one line per case.

    Cases already in results.jsonl are skipped. A case that fails goes to
    errors.jsonl, not results: a failure isn't a summary to grade, and it's
    tried again next time. Stops before the next case once `cap` dollars are spent.
    """
    _start_run(run)
    results, errors = run / "results.jsonl", run / "errors.jsonl"
    done_before = _done_ids(results)
    report = EvalRun()

    to_do = [c for c in cases if c.id not in done_before]
    report.skipped = len(cases) - len(to_do)
    for i, case in enumerate(to_do):
        if report.dollars >= cap:
            report.left = len(to_do) - i
            break
        counting = CountingClient(client)
        start = time.monotonic()
        documents: dict[str, str] = {}
        try:
            event, untrimmed = load(case)
            documents = trim_documents(untrimmed)
            result = summarize(event, documents, counting)
        except SummaryError as exc:
            cost = dollars(exc.input_tokens, exc.output_tokens)  # a failed call still costs
            report.dollars += cost
            report.failed.append(case.id)
            _append(errors, {
                "id": case.id, "error": str(exc), "kind": "summary",
                "first_try_problems": first_try_problems(counting, documents),
                # What the model wrote each time (None: no answer, like a refusal),
                # so a rejection can be judged: a real mistake, or a checker gap?
                "answers": [a.model_dump() if a else None for a in counting.answers],
                "calls": counting.calls, "input_tokens": exc.input_tokens,
                "output_tokens": exc.output_tokens, "dollars": round(cost, 4),
                "seconds": round(time.monotonic() - start, 1),
            })
            continue
        except Exception as exc:  # noqa: BLE001 — SEC or the API down: record it, go on
            report.failed.append(case.id)
            _append(errors, {
                "id": case.id, "error": f"{type(exc).__name__}: {exc}", "kind": "other",
                "calls": counting.calls, "seconds": round(time.monotonic() - start, 1),
            })
            continue

        cost = dollars(result.input_tokens, result.output_tokens)
        report.dollars += cost
        report.done.append(case.id)
        summary = result.summary
        _append(results, {
            "id": case.id, "tags": case.tags, "ticker": case.ticker, "form": case.form,
            "title": event.title, "url": event.url,
            "summary": summary.summary,
            "key_numbers": [k.model_dump() for k in summary.key_numbers],
            "model": result.model,  # the fallback model's name, if it answered instead
            "calls": counting.calls, "retried": counting.calls > 1,
            "first_try_problems": first_try_problems(counting, documents),
            "words": len(summary.summary.split()),
            "key_number_count": len(summary.key_numbers),
            "bare_quotes": sum(is_bare_quote(k) for k in summary.key_numbers),
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "dollars": round(cost, 4),
            "seconds": round(time.monotonic() - start, 1),
        })
    return report


# --- The grading sheet --------------------------------------------------------------------

# The five checks, as (key, what passing means). Approved on Nov 21, 2026.
CHECKS: list[tuple[str, str]] = [
    ("accurate", "every fact matches the filing, including what each number is"),
    ("faithful", "nothing added or stretched: no role, reason or claim the filing doesn't support"),
    ("main point", "the most important thing in the filing is in the summary, with the details a "
                   "reader needs; `partial` if the main point is there but something important is left out"),
    ("plain English", "a beginner could follow it; jargon is avoided or explained"),
    ("no advice", "it informs and doesn't advise: no buy, sell, good news or worrying"),
]

# What each mark counts for. `partial` only for the checks in PARTIAL_CHECKS: the
# pilot (Nov 21) graded HUT's summary a pass on main point though it left out the
# deal's floor and cap, so a pass/fail score couldn't show a prompt that adds them.
MARKS: dict[str, float] = {"pass": 1.0, "partial": 0.5, "fail": 0.0}
PARTIAL_CHECKS = {"main point"}


def allowed_marks(check: str) -> list[str]:
    """The marks a check can get: pass and fail, plus partial for main point."""
    return [m for m in MARKS if m != "partial" or check in PARTIAL_CHECKS]

_SHEET_HEADER = """# Grading sheet: {run}

For each case, read the summary, open the filing if you need to, and write
`pass` or `fail` after each check (main point can also be `partial`). Add a
note when something fails or is partial: it's what a prompt change will be
aimed at. Leave a mark blank until you've decided.

{checks}

The model's words are shown as quotes (lines starting with >), so nothing it
writes can look like a mark.
"""


def _quoted(text: str) -> str:
    """Model text as a Markdown quote: every line starts with "> ", so no line
    of it can be read as a heading or a mark when the sheet is read back."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def _one_line(text: str) -> str:
    """Filing or model text on one line, for a list item: line breaks would let
    it start a new line of the sheet."""
    return " ".join(text.split())


def _sheet_section(row: dict, case: Case | None) -> str:
    """One case's part of the sheet: the summary, its key numbers with their
    quotes, what was recorded automatically, and the blank marks."""
    parts = [f"## {row['id']}", "", f"**{_one_line(row['title'])}** · [the filing on SEC]({row['url']})", ""]
    if case is not None and case.note:
        parts += [f"*Look for:* {_one_line(case.note)}", ""]
    parts += ["**Summary**", "", _quoted(row["summary"]), ""]
    if row["key_numbers"]:
        parts += ["**Key numbers**", ""]
        for k in row["key_numbers"]:
            parts.append(f"- `{_one_line(k['number'])}` ({_one_line(k['source'])}): {_one_line(k['quote'])}")
        parts.append("")
    recorded = (
        f"{row['words']} words · {row['key_number_count']} key numbers, {row['bare_quotes']} bare quotes · "
        f"{row['calls']} call{'s' if row['calls'] != 1 else ''} · ${row['dollars']:.2f} · {row['model']}"
    )
    parts += [f"*Recorded:* {recorded}", ""]
    for problem in row.get("first_try_problems", []):
        parts.append(f"*First try failed:* {_one_line(problem)}")
    if row.get("first_try_problems"):
        parts.append("")
    parts += ["**Marks**", ""]
    parts += [f"- {key}: " for key, _ in CHECKS]
    parts += ["- note: ", ""]
    return "\n".join(parts)


def _sheet_ids(text: str) -> set[str]:
    """The case ids already in a sheet: its "## id" headings."""
    return {line[3:].strip() for line in text.splitlines() if line.startswith("## ")}


def write_sheet(cases: list[Case], run: Path) -> int:
    """Write run/grading.md from results.jsonl, or add the cases it doesn't have
    yet. Sections already there are never rewritten: they may hold your marks.
    Cases come in cases.toml's order. Returns how many sections were added."""
    rows = {json.loads(line)["id"]: json.loads(line)
            for line in (run / "results.jsonl").read_text(encoding="utf-8").splitlines() if line}
    sheet = run / "grading.md"
    existing = sheet.read_text(encoding="utf-8") if sheet.exists() else ""
    have = _sheet_ids(existing)

    by_id = {c.id: c for c in cases}
    order = [c.id for c in cases if c.id in rows] + [i for i in rows if i not in by_id]
    new = [_sheet_section(rows[i], by_id.get(i)) for i in order if i not in have]
    if not existing:
        checks = "\n".join(f"- **{key}**: {meaning}" for key, meaning in CHECKS)
        existing = _SHEET_HEADER.format(run=run.name, checks=checks)
    if new:
        sheet.write_text(existing.rstrip("\n") + "\n\n" + "\n".join(new), encoding="utf-8")
    elif not sheet.exists():
        sheet.write_text(existing, encoding="utf-8")
    return len(new)


# --- Scoring ----------------------------------------------------------------------------

Marks = dict[str, dict[str, str | None]]  # case id -> check -> its mark, None if blank


def read_marks(text: str) -> tuple[Marks, list[str]]:
    """Your marks from a grading sheet, and any problems with them.

    Only a "## id" heading or a "- check:" line at the start of a line counts.
    The model's text is all quoted ("> ...") or inside a key-number line
    ("- `5` ..."), so nothing it wrote can be read as a mark. A blank mark is
    None: not graded yet. A typo ("pas") or a partial where only pass/fail is
    allowed is a problem, reported with its case, so no mark is quietly dropped.
    """
    marks: Marks = {}
    problems: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            marks[current] = {key: None for key, _ in CHECKS}
            continue
        if current is None or not line.startswith("- "):
            continue
        check, colon, value = line[2:].partition(":")
        if not colon or check not in marks[current]:
            continue  # the note, or a key-number line
        mark = value.strip().lower()
        if not mark:
            continue
        if mark not in allowed_marks(check):
            problems.append(
                f"{current}: {check} is {value.strip()!r}; write {' or '.join(allowed_marks(check))}"
            )
            continue
        marks[current][check] = mark
    return marks, problems


@dataclass
class CheckScore:
    """One check's marks across the cases graded so far."""

    check: str
    counts: dict[str, int]     # "pass" / "partial" / "fail" -> how many
    blank: int                 # cases not marked yet
    not_pass: list[str]        # ids marked partial or fail: where to look

    @property
    def graded(self) -> int:
        return sum(self.counts.values())

    @property
    def score(self) -> float | None:
        """The mean mark, pass 1, partial 0.5, fail 0. None if nothing is graded."""
        if not self.graded:
            return None
        return sum(MARKS[m] * n for m, n in self.counts.items()) / self.graded


def score_marks(marks: Marks) -> list[CheckScore]:
    """Each check's score, in CHECKS' order."""
    scores = []
    for key, _ in CHECKS:
        given = {case_id: m[key] for case_id, m in marks.items()}
        scores.append(CheckScore(
            check=key,
            counts={mark: sum(1 for v in given.values() if v == mark) for mark in allowed_marks(key)},
            blank=sum(1 for v in given.values() if v is None),
            not_pass=[case_id for case_id, v in given.items() if v not in (None, "pass")],
        ))
    return scores


def score(run: Path) -> tuple[str, list[str]]:
    """The report for a run, and the problems with its marks (if any, fix them
    first: a report would leave those marks out). Costs nothing: it reads the
    sheet and the result files."""
    marks, problems = read_marks((run / "grading.md").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (run / "results.jsonl").read_text(encoding="utf-8").splitlines() if line]
    errors_file = run / "errors.jsonl"
    errors = len(errors_file.read_text(encoding="utf-8").splitlines()) if errors_file.exists() else 0

    scores = score_marks(marks)
    fully = sum(1 for m in marks.values() if all(v is not None for v in m.values()))
    lines = [f"# {run.name}: {len(marks)} cases on the sheet, {fully} fully graded", ""]
    lines.append(f"{'check':<15}{'score':>7}{'pass':>6}{'partial':>9}{'fail':>6}{'blank':>7}")
    for sc in scores:
        shown = "-" if sc.score is None else f"{sc.score:.0%}"
        partial = sc.counts.get("partial")
        lines.append(
            f"{sc.check:<15}{shown:>7}{sc.counts['pass']:>6}{'-' if partial is None else partial:>9}"
            f"{sc.counts['fail']:>6}{sc.blank:>7}"
        )
    lines.append("")
    for sc in scores:
        if sc.not_pass:
            lines.append(f"Not a pass on {sc.check}: {', '.join(sc.not_pass)}")
    if rows:
        spent = sum(r["dollars"] for r in rows)
        retried = sum(1 for r in rows if r["retried"])
        lines += [
            "",
            f"Recorded: ${spent:.2f} for {len(rows)} summaries (${spent / len(rows):.2f} each) · "
            f"{retried} of {len(rows)} retried · {sum(r['words'] for r in rows) / len(rows):.0f} words on average · "
            f"{sum(r['key_number_count'] for r in rows)} key numbers, {sum(r['bare_quotes'] for r in rows)} bare quotes · "
            f"{errors} failed (errors.jsonl)",
        ]
    if marks:
        lines.append(f"With {len(marks)} cases, one case moves a check's score by {1 / len(marks):.0%}.")
    return "\n".join(lines), problems


def main() -> None:
    """Run the summary eval from the command line. See the module docstring."""
    import anthropic
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="Run the summary eval on the approved filings.")
    parser.add_argument("run", help="a name for this run, e.g. v2-baseline; its folder is runs/NAME")
    parser.add_argument("--only", help="comma-separated case ids, e.g. for a pilot of a few")
    parser.add_argument("--cap", type=float, default=3.0, help="stop once this many dollars are spent")
    parser.add_argument("--sheet-only", action="store_true",
                        help="only write the grading sheet from the results so far (free)")
    parser.add_argument("--score", action="store_true",
                        help="only read the marks on the grading sheet and print the scores (free)")
    args = parser.parse_args()

    run = RUNS / args.run
    if args.score:
        if not (run / "grading.md").exists():
            parser.error(f"no grading sheet in {run} yet")
        report, problems = score(run)
        for problem in problems:
            print(f"# FIX {problem}")
        print(report)
        raise SystemExit(1 if problems else 0)

    load_dotenv()
    cases = load_cases()
    if args.only:
        wanted = args.only.split(",")
        unknown = sorted(set(wanted) - {c.id for c in cases})
        if unknown:
            parser.error(f"unknown case ids: {', '.join(unknown)}")
        cases = [c for c in cases if c.id in wanted]

    if not args.sheet_only:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        report = run_eval(cases, run, client, cap=args.cap)
        print(f"# {len(report.done)} summarized, {len(report.failed)} failed, "
              f"{report.skipped} already done, {report.left} left by the ${args.cap:.2f} cap")
        print(f"# about ${report.dollars:.2f} spent")
        for case_id in report.failed:
            print(f"# ERROR {case_id}: see errors.jsonl")
    elif not (run / "results.jsonl").exists():
        parser.error(f"no results in {run} yet")
    added = write_sheet(load_cases(), run)  # every case, so --only doesn't drop any from the sheet
    print(f"# Grading sheet: {run / 'grading.md'} ({added} new section{'s' if added != 1 else ''})")


if __name__ == "__main__":
    main()
