import html
from datetime import datetime, timezone

from daily_digest_desk.ingestion.edgar_events import WATCH_FORMS
from daily_digest_desk.output.digest import (
    _GROUP_LABELS, _item, _section, build_digest, mark_key_numbers,
)
from daily_digest_desk.output.explainers import explainer_key, load_explainers
from daily_digest_desk.reasoning.summarize import KeyNumber, Summary
from digest_helpers import NOW, big_move, event, visible_lines

# --- _item: one line ------------------------------------------------------------

def test_item_links_the_title_and_shows_the_new_york_day():
    e = event("1", title="AAPL 8-K: Earnings results")
    assert _item(e) == (
        '<li><a href="https://sec.test/x">AAPL 8-K: Earnings results</a>'
        ' <span class="date">Nov 17</span></li>'
    )


def test_item_day_is_new_york_not_utc():
    # 2am UTC on Nov 6 is 9pm New York on Nov 5. No leading zero on the day.
    e = event("1").model_copy(update={"occurred_at": datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc)})
    assert '<span class="date">Nov 5</span>' in _item(e)


def test_item_escapes_title_and_url():
    e = event("1", title='<script>alert("hi")</script>', url='https://sec.test/x?a=1&b="2"')
    assert _item(e) == (
        '<li><a href="https://sec.test/x?a=1&amp;b=&quot;2&quot;">'
        "&lt;script&gt;alert(&quot;hi&quot;)&lt;/script&gt;</a>"
        ' <span class="date">Nov 17</span></li>'
    )


def test_item_only_links_web_urls():
    for url in ["javascript:alert(1)", "file:///etc/passwd", ""]:
        line = _item(event("1", title="t", url=url))
        assert line == '<li>t <span class="date">Nov 17</span></li>', url
    assert "<a href" in _item(event("1", url="http://sec.test/x"))


# --- _section: grouping and folding ---------------------------------------------

def test_section_lists_newest_first_with_id_breaking_ties():
    page = _section("AAPL", [
        event("old", hours_ago=30, title="old"),
        # Same time: id decides (reversed, like the time). Given a-then-b on purpose:
        # Python's sort is stable, so without the id the output would keep a-then-b.
        event("a", hours_ago=2, title="a"),
        event("b", hours_ago=2, title="b"),
        event("new", hours_ago=1, title="new"),
    ])
    assert visible_lines(page) == ["AAPL", "new Nov 17", "b Nov 17", "a Nov 17", "old Nov 16"]


def test_section_folds_two_or_more_insider_transactions_where_the_newest_was():
    page = _section("AAPL", [
        event("8k", kind="8-K", hours_ago=1, title="8-K"),
        event("f1", kind="4", hours_ago=2, title="Form 4 newer"),
        event("10k", kind="10-K", hours_ago=3, title="10-K"),
        event("f2", kind="4", hours_ago=4, title="Form 4 older"),
    ])
    assert visible_lines(page) == [
        "AAPL",
        "8-K Nov 17",
        "Insider transactions (2)",
        "Form 4 newer Nov 17",
        "Form 4 older Nov 17",
        "10-K Nov 17",
    ]
    assert page.count("<details>") == 1


TETER = (
    "On Nov 19, Teter Timothy S. (EVP, General Counsel and Sec) sold 30,000 shares, at an "
    "average of $151.43 ($146.03 to $157.49), about $4.5 million. Under a 10b5-1 plan. "
    "Holds 2,925,898 shares after."
)


def form4_event(id, line, hours_ago=1):
    e = event(id, kind="4", ticker="NVDA", hours_ago=hours_ago, title=f"NVDA 4: {id}")
    return e.model_copy(update={"fields": {"insider": line}})


def test_item_shows_a_form4s_line_under_the_title_with_its_own_tag():
    line = _item(form4_event("f1", TETER))
    assert line == (
        '<li><a href="https://sec.test/x">NVDA 4: f1</a> <span class="date">Nov 17</span>\n'
        f'<p class="insider"><span class="form-tag">From the form</span> {html.escape(TETER)}</p></li>'
    )
    assert "AI summary" not in line  # code wrote it, not a model


def test_item_without_a_line_is_as_before():
    # Another form, or a Form 4 saved before lines were made, or a field that isn't text.
    for e in [event("8k"), event("f1", kind="4"), form4_event("f2", ""), form4_event("f3", 42)]:
        assert 'class="insider"' not in _item(e)


def test_item_escapes_the_line():
    assert "Doe &lt;b&gt;Jane&lt;/b&gt; (CEO)" in _item(form4_event("f1", "Doe <b>Jane</b> (CEO) sold 1 shares."))


def test_the_line_comes_after_a_summary():
    # A Form 4 doesn't get a summary today, but if one ever did, the summary leads.
    page = _item(form4_event("f1", TETER), summary=Summary(summary="Sold shares.", key_numbers=[]))
    assert page.index('class="summary"') < page.index('class="insider"')


def test_folded_form4s_keep_their_lines():
    page = _section("NVDA", [form4_event("f1", "First sold 1 shares.", 1), form4_event("f2", "Second sold 2 shares.", 2)])
    assert visible_lines(page) == [
        "NVDA",
        "Insider transactions (2)",
        "NVDA 4: f1 Nov 17",
        "From the form First sold 1 shares.",
        "NVDA 4: f2 Nov 17",
        "From the form Second sold 2 shares.",
    ]


def test_section_one_noisy_event_is_not_folded():
    page = _section("NVDA", [event("f1", kind="4", ticker="NVDA", title="Form 4")])
    assert "<details>" not in page
    assert visible_lines(page) == ["NVDA", "Form 4 Nov 17"]


def test_section_never_folds_8ks():
    page = _section("AAPL", [
        event("1", kind="8-K", hours_ago=1, title="8-K: Other events"),
        event("2", kind="8-K/A", hours_ago=2, title="8-K/A: Amendment"),
        event("3", kind="8-K", hours_ago=3, title="8-K: Earnings results"),
    ])
    assert "<details>" not in page
    assert visible_lines(page)[1:] == [
        "8-K: Other events Nov 17", "8-K/A: Amendment Nov 17", "8-K: Earnings results Nov 17",
    ]


def test_section_13d_and_13g_variants_fold_together():
    page = _section("CLSK", [
        event("1", kind="SC 13D/A", ticker="CLSK", hours_ago=1),
        event("2", kind="SCHEDULE 13G", ticker="CLSK", hours_ago=2),
        event("3", kind="SC 13G/A", ticker="CLSK", hours_ago=3),
    ])
    assert "<summary>Big-holder stake filings (3)</summary>" in page
    assert page.count("<details>") == 1


def test_section_escapes_the_heading():
    assert "<h2>A&amp;B</h2>" in _section("A&B", [event("1")])


# --- build_digest: the page ------------------------------------------------------

def test_page_macro_first_then_holdings_in_order_then_removed_a_to_z():
    page = build_digest([
        event("z", ticker="ZZZ", title="removed z"),
        event("n", ticker="NVDA", title="nvda"),
        event("m", ticker=None, kind="CPIAUCSL", title="cpi"),
        event("y", ticker="YYY", title="removed y"),
        event("a", ticker="AAPL", title="aapl"),
    ], NOW, ["NVDA", "CLSK", "AAPL"])
    headings = [line for line in page.splitlines() if line.startswith("<h2>")]
    # CLSK has nothing new, so it has no heading at all.
    assert headings == [
        "<h2>Macro &amp; Market</h2>", "<h2>NVDA</h2>", "<h2>AAPL</h2>",
        "<h2>YYY</h2>", "<h2>ZZZ</h2>",
    ]


def test_page_header_shows_new_york_time_and_count():
    page = build_digest([event("1"), event("2")], NOW, ["AAPL"])
    assert "<title>Digest · Tue Nov 17, 8:00am</title>" in page
    assert '<p class="meta">Tue Nov 17, 8:00am · 2 new items</p>' in page


def test_page_one_item_is_singular():
    assert "· 1 new item</p>" in build_digest([event("1")], NOW, ["AAPL"])


def test_page_afternoon_and_midnight_times():
    evening = datetime(2026, 11, 17, 22, 5, tzinfo=timezone.utc)   # 5:05pm New York
    midnight = datetime(2026, 11, 18, 5, 0, tzinfo=timezone.utc)   # 12:00am New York
    noon = datetime(2026, 11, 17, 17, 0, tzinfo=timezone.utc)      # 12:00pm New York
    assert "Tue Nov 17, 5:05pm" in build_digest([], evening, [])
    assert "Wed Nov 18, 12:00am" in build_digest([], midnight, [])
    assert "Tue Nov 17, 12:00pm" in build_digest([], noon, [])


def test_page_with_nothing_new():
    page = build_digest([], NOW, ["AAPL"])
    assert '<p class="meta">Tue Nov 17, 8:00am · Nothing new since the last digest.</p>' in page
    assert "<h2>" not in page


# --- the warning for sources that failed -----------------------------------------

def test_failed_sources_are_named_in_source_order_with_what_failed():
    # Nov 23, 2026: a run during a maintenance wake, no network. Given out of order on
    # purpose: the page lists them in _FETCH_SOURCES order, not the dict's.
    errors = {
        "finnhub_quotes": {"NVDA": "dns", "SPY": "dns"},
        "edgar": {"CLSK": "dns", "NVDA": "dns"},
        "fred": {"DFF": "dns"},
    }
    page = build_digest([], NOW, ["CLSK", "NVDA"], errors=errors)
    assert (
        '<p class="failed">⚠️ Couldn\'t check: SEC filings (CLSK, NVDA); FRED (DFF); '
        "Finnhub prices (NVDA, SPY). This page may be missing news from them.</p>"
    ) in page


def test_no_warning_when_every_source_worked():
    # collect.run puts an empty dict under each source that ran without errors.
    errors = {"edgar": {}, "fred": {}, "finnhub_quotes": {}, "finnhub_earnings": {}, "digest": {}}
    for page in [build_digest([], NOW, ["AAPL"], errors=errors), build_digest([], NOW, ["AAPL"])]:
        assert 'class="failed"' not in page


def test_only_fetch_sources_get_the_warning():
    # A failed summary or signal isn't news missing from the page.
    errors = {"summaries": {"e1": "timeout"}, "signals": {"DFF 2026-11-20": "x"}}
    assert 'class="failed"' not in build_digest([], NOW, ["AAPL"], errors=errors)


def test_the_warning_never_shows_the_error_message():
    # A failed request's url carries its API key.
    errors = {"finnhub_quotes": {"NVDA": "HTTP 500 for https://finnhub.io/api/v1/quote?token=SECRET"}}
    page = build_digest([], NOW, ["NVDA"], errors=errors)
    assert "Finnhub prices (NVDA)" in page
    assert "SECRET" not in page and "finnhub.io" not in page


def test_the_warning_escapes_names():
    errors = {"edgar": {"<b>X</b>": "x"}}
    assert "SEC filings (&lt;b&gt;X&lt;/b&gt;)" in build_digest([], NOW, ["X"], errors=errors)


def test_page_is_a_whole_html_document():
    page = build_digest([event("1")], NOW, ["AAPL"])
    assert page.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in page
    assert page.rstrip().endswith("</html>")


# --- red flags ---------------------------------------------------------------------

def flagged(id, hours_ago=1, items=("4.02", "9.01"), ticker="AAPL"):
    return event(id, kind="8-K", ticker=ticker, hours_ago=hours_ago, title=f"red {id}").model_copy(
        update={"source": "edgar", "fields": {"items": list(items)}},
    )


def test_red_flag_line_gets_a_label_with_a_tip():
    line = _item(flagged("1"))
    assert line.startswith('<li class="flag"><span class="flag-mark term" tabindex="0" data-tip="Rare and')
    assert '⚠️ Red flag</span> <a href="https://sec.test/x">red 1</a>' in line


def test_red_flags_go_first_in_their_section_even_when_older():
    page = _section("AAPL", [
        event("new", hours_ago=1, title="new"),
        flagged("old-flag", hours_ago=50),
        event("mid", hours_ago=5, title="mid"),
        flagged("newer-flag", hours_ago=3),  # older than "new", still first
    ])
    assert visible_lines(page) == [
        "AAPL",
        "⚠️ Red flag red newer-flag Nov 17",  # flags first, newest flag first
        "⚠️ Red flag red old-flag Nov 15",
        "new Nov 17",                          # then the rest, newest first
        "mid Nov 17",
    ]


def test_an_ordinary_8k_is_not_flagged():
    line = _item(flagged("1", items=("2.02", "9.01")))
    assert "Red flag" not in line and line.startswith("<li>")


def test_header_counts_red_flags():
    page = build_digest([flagged("1"), flagged("2", ticker="NVDA"), event("3")], NOW, ["AAPL", "NVDA"], {})
    assert '<p class="meta">Tue Nov 17, 8:00am · 3 new items · ⚠️ 2 red flags</p>' in page
    one = build_digest([flagged("1")], NOW, ["AAPL"], {})
    assert '<p class="meta">Tue Nov 17, 8:00am · 1 new item · ⚠️ 1 red flag</p>' in one


def test_header_says_nothing_about_red_flags_when_there_are_none():
    page = build_digest([event("1")], NOW, ["AAPL"], {})
    assert '<p class="meta">Tue Nov 17, 8:00am · 1 new item</p>' in page


def test_market_move_shows_under_macro_and_market():
    spy = big_move("finnhub:SPY").model_copy(update={"title": "S&P 500 (SPY) down 2.32% to $585.12"})
    page = build_digest([spy, event("a", ticker="AAPL")], NOW, ["AAPL"], {})
    headings = [line for line in page.splitlines() if line.startswith("<h2>")]
    assert headings == ["<h2>Macro &amp; Market</h2>", "<h2>AAPL</h2>"]
    macro = page[page.index("Macro &amp; Market"):page.index("<h2>AAPL</h2>")]
    assert "S&amp;P 500 (SPY) down 2.32%" in visible_lines(macro)[1]


# --- every watched form is handled on the page ------------------------------------

def test_every_watched_form_has_a_group_label_and_an_explainer():
    explainers = load_explainers()
    for form in WATCH_FORMS:
        assert form in _GROUP_LABELS, form
        assert explainer_key(event("1", kind=form)) in explainers, form


def test_share_offering_filings_fold_together_only_when_noisy():
    # S-3, S-3ASR and 424B share a label but aren't noisy: each keeps its own line.
    page = _section("KEEL", [event("1", kind="S-3ASR"), event("2", kind="424B5", hours_ago=2)])
    assert "<details>" not in page


# --- summaries: the model's few sentences under a filing ---------------------------

def summary(text):
    return Summary(summary=text, key_numbers=[])


def test_item_shows_its_summary_under_the_title_with_a_tag():
    line = _item(event("1", title="AAOI 8-K"), summary=summary("It may sell up to $550 million of stock."))
    assert line == (
        '<li><a href="https://sec.test/x">AAOI 8-K</a> <span class="date">Nov 17</span>\n'
        '<p class="summary"><span class="ai-tag">AI summary</span> '
        "It may sell up to $550 million of stock.</p></li>"
    )


def test_item_escapes_the_summary():
    # The model's words are outside text, like a title.
    line = _item(event("1"), summary=summary('An "at-the-market" offering <b>now</b> & later.'))
    assert "An &quot;at-the-market&quot; offering &lt;b&gt;now&lt;/b&gt; &amp; later.</p>" in line


def test_a_red_flag_line_keeps_its_summary():
    line = _item(flagged("1"), summary=summary("The company says past results were wrong."))
    assert line.startswith('<li class="flag">')
    assert line.endswith('<span class="ai-tag">AI summary</span> The company says past results were wrong.</p></li>')


def test_section_gives_each_event_its_own_summary():
    page = _section("AAOI", [
        event("8k", hours_ago=1, title="8-K"),
        event("424b5", kind="424B5", hours_ago=2, title="424B5"),
        event("form4", kind="4", hours_ago=3, title="Form 4"),  # no summary: just its line
    ], summaries={"8k": summary("Eight."), "424b5": summary("Prospectus.")})
    assert visible_lines(page) == [
        "AAOI", "8-K Nov 17", "AI summary Eight.", "424B5 Nov 17", "AI summary Prospectus.", "Form 4 Nov 17",
    ]


def test_section_a_folded_filing_keeps_its_summary_inside_the_group():
    page = _section("HUT", [
        event("13d", kind="SCHEDULE 13D/A", hours_ago=1, title="13D/A"),
        event("13g", kind="SCHEDULE 13G", hours_ago=2, title="13G"),
    ], summaries={"13d": summary("He received shares.")})
    assert visible_lines(page) == [
        "HUT", "Big-holder stake filings (2)", "13D/A Nov 17", "AI summary He received shares.", "13G Nov 17",
    ]


def test_page_shows_summaries_for_holdings_and_removed_holdings():
    page = build_digest(
        [event("aapl", ticker="AAPL"), event("old", ticker="GONE")],  # GONE: a removed holding
        NOW, ["AAPL"],
        summaries={"aapl": summary("Apple one."), "old": summary("Gone one.")},
    )
    assert "AI summary Apple one." in visible_lines(page)
    assert "AI summary Gone one." in visible_lines(page)


def test_page_without_summaries_has_no_tag():
    assert 'class="ai-tag"' not in build_digest([event("1")], NOW, ["AAPL"])


# --- mark_key_numbers: each key number gets a tip with its quote -------------------

def kn(number, quote=None, source="EX-99.1"):
    return KeyNumber(number=number, quote=quote or f"It was {number}.", source=source)


def test_marks_a_key_number_with_its_source_and_quote():
    marked, not_found = mark_key_numbers(
        "Revenue was $115.8 billion.",
        [kn("$115.8 billion", "Revenue was a record $115.8 billion.", "EX-99.2")],
    )
    assert marked == (
        'Revenue was <span class="term keynum" tabindex="0" data-tip="'
        'EX-99.2: “Revenue was a record $115.8 billion.”">$115.8 billion</span>.'
    )
    assert not_found == []


def test_quote_line_breaks_become_spaces():
    marked, _ = mark_key_numbers("EPS was $2.70.", [kn("$2.70", "earnings per diluted share were $2.70 and\n   $2.68,\nrespectively.")])
    assert "were $2.70 and $2.68, respectively." in marked


def test_escapes_the_quote_and_finds_a_number_with_an_ampersand():
    # The text comes in escaped, so the number must be looked for escaped too.
    marked, not_found = mark_key_numbers(
        "The S&amp;P 500 fell.", [kn("S&P 500", 'The "S&P 500" <fell>.')],
    )
    assert not_found == []
    assert 'data-tip="EX-99.1: “The &quot;S&amp;P 500&quot; &lt;fell&gt;.”"' in marked
    assert ">S&amp;P 500</span> fell." in marked


def test_a_number_inside_a_longer_number_is_not_marked():
    marked, not_found = mark_key_numbers(
        "Fees of 12%, 1,200 shares, $2.705 each, $2x70 total.",
        [kn("2%"), kn("200"), kn("$2.70")],
    )
    assert "<span" not in marked  # "." in "$2.70" means a dot, not any character
    assert [k.number for k in not_found] == ["2%", "200", "$2.70"]


def test_a_number_at_the_end_of_a_sentence_or_before_a_comma_is_marked():
    marked, not_found = mark_key_numbers("A fee of 2%, then 2%.", [kn("2%"), kn("2%")])
    assert marked.count("<span") == 2
    assert not_found == []


def test_the_longer_of_two_numbers_that_start_alike_wins():
    # Both match at "$550": the longer one must be tried first, or "$550 million"
    # would be cut to "$550" and reported as not found.
    marked, not_found = mark_key_numbers(
        "Up to $550 million, or $550 a share.", [kn("$550"), kn("$550 million")],
    )
    assert ">$550 million</span>, or <span" in marked
    assert ">$550</span> a share." in marked
    assert not_found == []


def test_only_the_first_mention_is_marked():
    marked, _ = mark_key_numbers("It sold 2% now and 2% later.", [kn("2%")])
    assert marked.count("<span") == 1
    assert marked.endswith("and 2% later.")


def test_a_number_listed_twice_gives_each_mention_its_own_quote():
    marked, _ = mark_key_numbers(
        "A 2% fee and a 2% stake.",
        [kn("2%", "a fee of 2%", "8-K"), kn("2%", "a stake of 2%", "13D")],
    )
    assert marked.index("a fee of 2%") < marked.index("a stake of 2%")


def test_a_number_not_in_the_text_is_returned():
    missing = kn("$2.68")
    marked, not_found = mark_key_numbers("EPS was $2.70.", [kn("$2.70"), missing])
    assert not_found == [missing]
    assert marked.count("<span") == 1


def test_no_key_numbers_leaves_the_text_alone():
    assert mark_key_numbers("Nothing to see.", []) == ("Nothing to see.", [])
    # A blank number would match everywhere; it's skipped, not reported.
    assert mark_key_numbers("Nothing to see.", [kn("  ", "x")]) == ("Nothing to see.", [])


def test_the_real_nvda_summary_marks_all_five():
    # NVDA's 8-K summary as stored on Nov 20, 2026, escaped as _item will pass it.
    text = html.escape(
        "NVIDIA reported third-quarter revenue of $115.8 billion, up 103% from a year ago, "
        "with Data Center revenue of $107.6 billion. Earnings per diluted share (profit per "
        "share) were $2.70 under standard accounting. The company returned $28.1 billion to "
        "shareholders through buybacks and dividends, and expects fourth-quarter revenue of "
        "$130.0 billion, plus or minus 3%."
    )
    numbers = [kn(n) for n in ["$115.8 billion", "$107.6 billion", "$2.70", "$28.1 billion", "$130.0 billion"]]
    marked, not_found = mark_key_numbers(text, numbers)
    assert not_found == []
    assert marked.count('class="term keynum"') == 5


# --- key numbers on the page: in the summary, or after "Also in the filing:" --------

def summary_with(text, *key_numbers):
    return Summary(summary=text, key_numbers=list(key_numbers))


def test_item_marks_the_key_numbers_in_its_summary():
    line = _item(event("1", title="AAOI 8-K"), summary=summary_with(
        "It may sell up to $550 million of stock.",
        kn("$550 million", "an offering price of up to $550 million", "8-K"),
    ))
    assert line == (
        '<li><a href="https://sec.test/x">AAOI 8-K</a> <span class="date">Nov 17</span>\n'
        '<p class="summary"><span class="ai-tag">AI summary</span> It may sell up to '
        '<span class="term keynum" tabindex="0" data-tip="8-K: “an offering price of up to '
        '$550 million”">$550 million</span> of stock.</p></li>'
    )


def test_a_key_number_not_in_the_summary_goes_after_also():
    line = _item(event("1"), summary=summary_with(
        "EPS was $2.70.", kn("$2.70"), kn("$2.68", "non-GAAP EPS was $2.68"),
    ))
    assert line.endswith(
        ' <span class="also">Also in the filing: <span class="term keynum" tabindex="0" '
        'data-tip="EX-99.1: “non-GAAP EPS was $2.68”">$2.68</span></span></p></li>'
    )
    assert line.count('class="term keynum"') == 2  # $2.70 in the text, $2.68 after Also


def test_several_numbers_after_also_are_listed_in_order():
    line = _item(event("1"), summary=summary_with("Nothing in here.", kn("$1"), kn("$2"), kn("$3")))
    text = visible_lines(line)[-1]
    assert text.endswith("Nothing in here. Also in the filing: $1, $2, $3")


def test_no_also_when_every_key_number_is_in_the_summary():
    line = _item(event("1"), summary=summary_with("A 2% fee.", kn("2%")))
    assert "Also" not in line


def test_also_escapes_its_numbers():
    line = _item(event("1"), summary=summary_with("Nothing.", kn("<b>5</b>", "it was <b>5</b>")))
    assert ">&lt;b&gt;5&lt;/b&gt;</span></span>" in line
    assert "<b>" not in line


def test_a_red_flag_line_marks_its_key_numbers_too():
    line = _item(flagged("1"), summary=summary_with("Restated by $3 million.", kn("$3 million")))
    assert line.startswith('<li class="flag">')
    assert '">$3 million</span>' in line


# --- "key detail" bullets: under the summary, marked the same way ---------------

def with_changes(text, changes, *key_numbers):
    return Summary(summary=text, key_numbers=list(key_numbers), changes=list(changes))


def test_bullets_go_in_a_list_under_the_summary():
    line = _item(event("1"), summary=with_changes("Revenue rose.", ["A delay.", "A one-time item."]))
    assert line.endswith(
        "Revenue rose.</p>\n"
        '<div class="changes"><span class="changes-tag">Key details</span>'
        "<ul><li>A delay.</li><li>A one-time item.</li></ul></div></li>"
    )


def test_no_bullets_means_no_list():
    # Summaries saved before v3, and routine filings, look as they always did.
    line = _item(event("1"), summary=with_changes("Routine.", []))
    assert "changes" not in line and line.endswith("Routine.</p></li>")


def test_a_key_number_in_a_bullet_gets_its_tip_and_is_not_also_listed():
    line = _item(event("1"), summary=with_changes(
        "Revenue was $5 billion.", ["Spending was raised to $27 billion."],
        kn("$5 billion"), kn("$27 billion", "Capital expenditures: approximately $27 billion"),
    ))
    assert '<li>Spending was raised to <span class="term keynum"' in line
    assert 'data-tip="EX-99.1: “Capital expenditures: approximately $27 billion”">$27 billion</span>' in line
    assert "Also in the filing" not in line  # it's on the page already, in the bullet


def test_a_key_number_gets_one_tip_even_if_a_bullet_repeats_it():
    line = _item(event("1"), summary=with_changes("Revenue was $5 billion.", ["Again $5 billion."], kn("$5 billion")))
    assert line.count('class="term keynum"') == 1
    assert "<li>Again $5 billion.</li>" in line


def test_bullets_are_escaped():
    line = _item(event("1"), summary=with_changes("Fine.", ['A <b>bold</b> & "quoted" claim.']))
    assert "<li>A &lt;b&gt;bold&lt;/b&gt; &amp; &quot;quoted&quot; claim.</li>" in line


def test_an_empty_bullet_is_left_out():
    line = _item(event("1"), summary=with_changes("Fine.", ["  ", "Real."]))
    assert "<ul><li>Real.</li></ul>" in line


def test_page_shows_key_number_tips():
    page = build_digest(
        [event("aapl", ticker="AAPL")], NOW, ["AAPL"],
        summaries={"aapl": summary_with("Revenue was $90 billion.", kn("$90 billion", "revenue of $90 billion"))},
    )
    assert 'data-tip="EX-99.1: “revenue of $90 billion”">$90 billion</span>' in page
    assert "AI summary Revenue was $90 billion." in visible_lines(page)
