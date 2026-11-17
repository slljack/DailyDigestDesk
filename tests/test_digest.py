import re
from datetime import datetime, timedelta, timezone

from daily_digest_desk.models import Event
from daily_digest_desk.output.digest import (
    _EXPLAINER_BY_KIND, _EXPLAINER_BY_SERIES, _item, _section, build_digest,
    Glossary, _explainer_cards, explainer_key, load_explainers, load_glossary,
)

NOW = datetime(2026, 11, 17, 13, 0, 12, tzinfo=timezone.utc)  # Tue 8:00am New York


def event(id, kind="8-K", ticker="AAPL", hours_ago=1, title=None, url="https://sec.test/x"):
    return Event(
        id=id, source="test", kind=kind, ticker=ticker,
        occurred_at=NOW - timedelta(hours=hours_ago),
        title=title or f"{ticker} {kind} {id}", url=url,
    )


def visible_lines(page: str) -> list[str]:
    """The page's text, one line per tag line, with the tags stripped: what you'd read."""
    lines = (re.sub(r"<[^>]+>", "", line).strip() for line in page.splitlines())
    return [line for line in lines if line]


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


# --- explainers ------------------------------------------------------------------

EXPLAINERS = {  # a small stand-in for explainers.toml, so tests don't depend on wording
    "8-K": {"name": "8-K", "what": "A current report.", "why": "News.", "look_for": "The item."},
    "cpi": {"name": "CPI", "what": "Prices.", "why": "Rates."},  # no look_for
    "vix": {"name": "VIX <fear>", "what": "Swings.", "why": "Stress."},
}


def big_move(series, ticker=None):
    return event(f"signal:{series}", kind="big_move", ticker=ticker).model_copy(
        update={"fields": {"series": series}},
    )


def test_explainer_key_by_kind():
    assert explainer_key(event("1", kind="8-K/A")) == "8-K"
    assert explainer_key(event("1", kind="4")) == "form-4"
    assert explainer_key(event("1", kind="SCHEDULE 13D/A")) == "13d"
    assert explainer_key(event("1", kind="SC 13G")) == "13g"
    assert explainer_key(event("1", kind="CPIAUCSL", ticker=None)) == "cpi"
    assert explainer_key(event("1", kind="something new")) is None


def test_explainer_key_big_moves_by_series():
    assert explainer_key(big_move("finnhub:NVDA", "NVDA")) == "stock-big-move"
    assert explainer_key(big_move("fred:VIXCLS")) == "vix"
    assert explainer_key(big_move("fred:DGS10")) == "10-year-yield"
    assert explainer_key(big_move("fred:DGS2")) is None  # a series with no explainer yet


def test_item_button_opens_its_explainer_card():
    assert _item(event("1", title="t"), EXPLAINERS) == (
        '<li><a href="https://sec.test/x">t</a> <span class="date">Nov 17</span>'
        ' <button type="button" class="info" popovertarget="explain-8-K"'
        ' title="What is this? 8-K" aria-label="What is this? 8-K">ⓘ</button></li>'
    )


def test_item_without_an_explainer_has_no_link():
    assert 'class="info"' not in _item(event("1", kind="10-K"), EXPLAINERS)  # not in EXPLAINERS
    assert 'class="info"' not in _item(event("1"))  # no explainers given


def test_items_in_a_fold_link_too():
    page = _section("AAPL", [event("1", kind="4"), event("2", kind="4")],
                    {"form-4": {"name": "Form 4", "what": "w", "why": "y"}})
    assert page.count('class="info" popovertarget="explain-form-4"') == 2


def test_page_has_one_hidden_card_per_explainer_used_in_file_order():
    page = build_digest([
        big_move("fred:VIXCLS"),
        event("a", kind="8-K"),
        event("b", kind="8-K/A"),  # same explainer as the 8-K: one card
        event("c", kind="10-K"),   # no explainer: no card, no error
    ], NOW, ["AAPL"], EXPLAINERS)
    cards = page[page.index('<div class="explainer"'):]
    assert visible_lines(cards) == [
        "✕", "8-K", "What it is. A current report.", "Why stocks care. News.",
        "What to look for. The item.",
        "✕", "VIX &lt;fear&gt;", "What it is. Swings.", "Why stocks care. Stress.",  # escaped, no look_for
    ]
    # `popover` keeps a card hidden until its ⓘ is clicked; ✕ hides it again.
    assert '<div class="explainer" id="explain-8-K" popover>' in cards
    assert ('<button type="button" class="close" popovertarget="explain-vix"'
            ' popovertargetaction="hide" aria-label="Close">✕</button>') in cards
    assert cards.index('id="explain-8-K"') < cards.index('id="explain-vix"')  # file order
    assert 'id="explain-cpi"' not in page  # CPI isn't on this page


def test_every_info_button_has_its_card():
    page = build_digest(
        [event("a", kind="8-K"), big_move("fred:VIXCLS"), event("c", kind="CPIAUCSL", ticker=None)],
        NOW, ["AAPL"], EXPLAINERS,
    )
    opened = set(re.findall(r'class="info" popovertarget="([^"]+)"', page))
    cards = set(re.findall(r'<div class="explainer" id="([^"]+)" popover>', page))
    assert opened == cards == {"explain-8-K", "explain-vix", "explain-cpi"}


def test_page_with_no_explained_types_has_no_cards():
    page = build_digest([event("1", kind="10-K")], NOW, ["AAPL"], EXPLAINERS)
    assert '<div class="explainer"' not in page and "popovertarget" not in page


def test_page_uses_explainers_toml_by_default():
    page = build_digest([event("1", kind="8-K")], NOW, ["AAPL"])
    assert '<div class="explainer" id="explain-8-K" popover>' in page
    assert "<h3>8-K: a current report</h3>" in page


# --- explainers.toml itself --------------------------------------------------------

def test_every_explainer_has_the_required_text():
    for key, entry in load_explainers().items():
        assert set(entry) <= {"name", "what", "why", "look_for"}, key  # catches typos
        assert entry["name"] and entry["what"] and entry["why"], key


def test_explainers_toml_and_code_agree():
    # Every key the code can pick exists in the file, and every entry in the file is used.
    in_code = set(_EXPLAINER_BY_KIND.values()) | set(_EXPLAINER_BY_SERIES.values()) | {"stock-big-move"}
    assert in_code == set(load_explainers())


# --- glossary --------------------------------------------------------------------

GLOSSARY = Glossary({
    "eps": {"words": ["EPS"], "definition": "Earnings per share."},
    "expected": {"words": ["expected", "estimate"], "definition": "What analysts expect."},
    "shelf": {"words": ["shelf"], "definition": "Permission to sell later."},
    "shelf-reg": {"words": ["shelf registration"], "definition": "The full filing."},
    "sp500": {"words": ["S&P 500"], "definition": 'The "market".'},
})


def tip(word, definition):
    return f'<span class="term" tabindex="0" data-tip="{definition}">{word}</span>'


def test_glossary_marks_whole_words_in_any_case_keeping_the_original():
    assert GLOSSARY.mark("AAPL beat eps", set()) == "AAPL beat " + tip("eps", "Earnings per share.")
    for text in ["STEPS up", "EPS-1", "EPS2"]:  # inside a bigger word: no match
        assert GLOSSARY.mark(text, set()) == text, text


def test_glossary_marks_each_entry_once():
    # "estimate" and "expected" are one entry: only the first is marked.
    marked = GLOSSARY.mark("EPS estimate: 2.10 vs 2.03 expected, EPS up", set())
    assert marked == (
        tip("EPS", "Earnings per share.") + " " + tip("estimate", "What analysts expect.")
        + ": 2.10 vs 2.03 expected, EPS up"
    )


def test_glossary_seen_carries_across_texts():
    seen = set()
    first = GLOSSARY.mark("EPS", seen)
    assert GLOSSARY.mark("EPS again", seen) == "EPS again"
    assert first.startswith("<span") and seen == {"eps"}


def test_glossary_prefers_the_longer_match():
    assert GLOSSARY.mark("a shelf registration", set()) == "a " + tip("shelf registration", "The full filing.")


def test_glossary_works_on_escaped_text():
    # Matches "S&P 500" as it appears after html.escape, and escapes the definition.
    assert GLOSSARY.mark("S&amp;P 500 fell", set()) == tip("S&amp;P 500", "The &quot;market&quot;.") + " fell"
    # Never inside an entity: "amp" in "&amp;" is not a word.
    amp = Glossary({"amp": {"words": ["amp"], "definition": "x"}})
    assert amp.mark("A &amp; B", set()) == "A &amp; B"


def test_glossary_keys_in():
    assert GLOSSARY.keys_in("EPS vs estimate, no shelf") == {"eps", "expected", "shelf"}
    assert Glossary({}).keys_in("EPS") == set()


def test_item_title_gets_tips_but_stays_escaped():
    e = event("1", title="<b> EPS estimate", url="https://sec.test/x")
    assert _item(e, glossary=GLOSSARY) == (
        '<li><a href="https://sec.test/x">&lt;b&gt; '
        + tip("EPS", "Earnings per share.") + " " + tip("estimate", "What analysts expect.")
        + '</a> <span class="date">Nov 17</span></li>'
    )


def test_each_line_marks_its_own_first_mention():
    page = _section("AAPL", [event("1", title="EPS one"), event("2", title="EPS two")], glossary=GLOSSARY)
    assert page.count('class="term"') == 2


def test_card_marks_once_and_skips_its_own_subject():
    explainers = {"eps-card": {
        "name": "EPS explained",
        "what": "EPS is profit per share; the estimate is the guess.",
        "why": "An EPS beat vs the estimate moves prices.",
        "look_for": "A shelf.",
    }}
    _EXPLAINER_BY_KIND["test-kind"] = "eps-card"
    try:
        cards = _explainer_cards([event("1", kind="test-kind")], explainers, GLOSSARY)
    finally:
        del _EXPLAINER_BY_KIND["test-kind"]
    assert "<h3>EPS explained</h3>" in cards  # headings are never marked
    assert 'data-tip="Earnings per share."' not in cards  # the card is about EPS already
    assert cards.count('data-tip="What analysts expect."') == 1  # once across the card
    assert cards.count('data-tip="Permission to sell later."') == 1  # in look_for


def test_page_uses_glossary_toml_by_default():
    page = build_digest([event("1", kind="earnings_result", title="AAPL beat EPS estimate")], NOW, ["AAPL"])
    assert 'tabindex="0" data-tip="Earnings per share: the company' in page


# --- glossary.toml itself -----------------------------------------------------------

def test_every_glossary_entry_is_complete():
    for key, entry in load_glossary().items():
        assert set(entry) == {"words", "definition"}, key  # catches typos and missing fields
        assert entry["words"] and all(entry["words"]) and entry["definition"], key


def test_no_word_belongs_to_two_entries():
    owner = {}
    for key, entry in load_glossary().items():
        for word in entry["words"]:
            assert word.lower() not in owner, f"{word!r} is in {owner.get(word.lower())} and {key}"
            owner[word.lower()] = key


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
