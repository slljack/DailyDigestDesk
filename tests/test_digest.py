from datetime import datetime, timezone

from daily_digest_desk.ingestion.edgar_events import WATCH_FORMS
from daily_digest_desk.output.digest import _GROUP_LABELS, _item, _section, build_digest
from daily_digest_desk.output.explainers import explainer_key, load_explainers
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
