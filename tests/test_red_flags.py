from daily_digest_desk.red_flags import RED_FLAG_ITEMS, is_red_flag
from conftest import make_event


def eight_k(*items, kind="8-K", source="edgar"):
    return make_event("edgar:1", kind=kind, source=source, fields={"items": list(items)})


def test_each_red_flag_item_is_a_red_flag():
    assert set(RED_FLAG_ITEMS) == {"1.03", "2.06", "3.01", "4.01", "4.02"}
    for item in RED_FLAG_ITEMS:
        assert is_red_flag(eight_k(item, "9.01")), item


def test_ordinary_8k_items_are_not():
    assert not is_red_flag(eight_k("2.02", "9.01"))  # earnings: important, not a red flag
    assert not is_red_flag(eight_k("5.02"))
    assert not is_red_flag(eight_k())  # no items listed


def test_one_red_flag_item_among_others_is_enough():
    assert is_red_flag(eight_k("2.02", "4.02", "9.01"))


def test_an_amended_8k_counts():
    assert is_red_flag(eight_k("4.02", kind="8-K/A"))


def test_only_edgar_8ks():
    # The same item numbers on another kind or source mean nothing.
    assert not is_red_flag(eight_k("4.02", kind="10-K"))
    assert not is_red_flag(eight_k("4.02", source="finnhub"))
    assert not is_red_flag(make_event("edgar:2", kind="8-K"))  # no "items" field at all
