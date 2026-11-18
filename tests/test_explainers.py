import re

from daily_digest_desk.output.digest import _item, _section, build_digest
from daily_digest_desk.output.explainers import (
    _EXPLAINER_BY_KIND, _EXPLAINER_BY_SERIES, explainer_key, load_explainers,
)
from digest_helpers import NOW, big_move, event, visible_lines


# --- explainers ------------------------------------------------------------------

EXPLAINERS = {  # a small stand-in for explainers.toml, so tests don't depend on wording
    "8-K": {"name": "8-K", "what": "A current report.", "why": "News.", "look_for": "The item."},
    "cpi": {"name": "CPI", "what": "Prices.", "why": "Rates."},  # no look_for
    "vix": {"name": "VIX <fear>", "what": "Swings.", "why": "Stress."},
}


def test_explainer_key_by_kind():
    assert explainer_key(event("1", kind="8-K/A")) == "8-K"
    assert explainer_key(event("1", kind="4")) == "form-4"
    assert explainer_key(event("1", kind="SCHEDULE 13D/A")) == "13d"
    assert explainer_key(event("1", kind="SC 13G")) == "13g"
    assert explainer_key(event("1", kind="CPIAUCSL", ticker=None)) == "cpi"
    assert explainer_key(event("1", kind="6-K/A")) == "6-K"
    assert explainer_key(event("1", kind="S-3ASR")) == "S-3"
    assert explainer_key(event("1", kind="424B5")) == "424B"
    assert explainer_key(event("1", kind="something new")) is None


def test_explainer_key_big_moves_by_series():
    assert explainer_key(big_move("finnhub:NVDA", "NVDA")) == "stock-big-move"
    assert explainer_key(big_move("fred:VIXCLS")) == "vix"
    assert explainer_key(big_move("fred:DGS10")) == "10-year-yield"
    assert explainer_key(big_move("fred:DGS2")) is None  # a series with no explainer yet
    assert explainer_key(big_move("finnhub:SPY")) == "sp500-move"  # not "a holding"


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
