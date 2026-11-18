from daily_digest_desk.output.digest import _item, _section, build_digest
from daily_digest_desk.output.explainers import _EXPLAINER_BY_KIND, explainer_cards
from daily_digest_desk.output.glossary import Glossary, load_glossary
from digest_helpers import NOW, event


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
        cards = explainer_cards([event("1", kind="test-kind")], explainers, GLOSSARY)
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
