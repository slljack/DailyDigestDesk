import pytest

from daily_digest_desk.holdings import Holding, load_holdings


def write(tmp_path, text):
    path = tmp_path / "holdings.toml"
    path.write_text(text)
    return path


def test_loads_in_file_order(tmp_path):
    path = write(tmp_path, """
[[position]]
ticker = "clsk"

[[position]]
ticker = " NVDA "
weight = 0.15
""")
    assert load_holdings(path) == [Holding("CLSK", 0.0), Holding("NVDA", 0.15)]


def test_a_ticker_listed_twice_is_kept_once(tmp_path):
    path = write(tmp_path, """
[[position]]
ticker = "NVDA"
weight = 0.15

[[position]]
ticker = "MSFT"

[[position]]
ticker = "nvda"
weight = 0.30
""")
    assert load_holdings(path) == [Holding("NVDA", 0.15), Holding("MSFT", 0.0)]  # first wins


def test_blank_or_missing_ticker_is_an_error(tmp_path):
    for body in ['ticker = "  "', "weight = 0.1"]:
        path = write(tmp_path, f"[[position]]\n{body}\n")
        with pytest.raises(ValueError, match="Position 1 .* has no ticker"):
            load_holdings(path)


def test_no_positions_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="No \\[\\[position\\]\\] entries"):
        load_holdings(write(tmp_path, "# nothing here\n"))
