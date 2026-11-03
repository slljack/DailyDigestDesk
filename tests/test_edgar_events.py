from dataclasses import dataclass
from datetime import date, datetime, timezone

from daily_digest_desk.ingestion.edgar_events import filing_to_event, parse_items


@dataclass
class FakeFiling:
    """Mirrors the EntityFiling attributes we read (edgartools 5.x)."""

    form: str
    filing_date: date
    accession_number: str = "0000320193-26-000001"
    items: str = ""
    acceptance_datetime: datetime | None = None
    homepage_url: str = "https://www.sec.gov/index.html"
    filing_url: str = "https://www.sec.gov/doc.htm"


def test_parse_items():
    assert parse_items("2.02,9.01") == ["2.02", "9.01"]
    assert parse_items(" 5.02 , ") == ["5.02"]
    assert parse_items("") == []
    assert parse_items(None) == []
    assert parse_items(["1.01"]) == ["1.01"]


def test_8k_title_uses_item_labels_and_drops_exhibits():
    f = FakeFiling(form="8-K", filing_date=date(2026, 10, 30), items="2.02,9.01")
    e = filing_to_event("aapl", f)
    assert e.id == "edgar:0000320193-26-000001"
    assert e.ticker == "AAPL"
    assert e.title == "AAPL 8-K: Earnings results"
    assert e.fields["items"] == ["2.02", "9.01"]


def test_8k_unknown_item_falls_back_to_number():
    f = FakeFiling(form="8-K", filing_date=date(2026, 10, 30), items="6.99")
    assert filing_to_event("AAPL", f).title == "AAPL 8-K: Item 6.99"


def test_amended_form_is_marked():
    f = FakeFiling(form="8-K/A", filing_date=date(2026, 10, 30), items="5.02")
    assert filing_to_event("AAPL", f).title == "AAPL 8-K/A (amended): Director / officer change"


def test_form4_uses_form_description():
    f = FakeFiling(form="4", filing_date=date(2026, 10, 30))
    assert filing_to_event("AAPL", f).title == "AAPL 4: Insider transaction"


def test_occurred_at_prefers_acceptance_time():
    accepted = datetime(2026, 10, 29, 22, 30, 14, tzinfo=timezone.utc)
    f = FakeFiling(form="4", filing_date=date(2026, 10, 29), acceptance_datetime=accepted)
    assert filing_to_event("AAPL", f).occurred_at == accepted


def test_occurred_at_falls_back_to_filing_date():
    f = FakeFiling(form="4", filing_date=date(2026, 10, 29))
    assert filing_to_event("AAPL", f).occurred_at == datetime(2026, 10, 29, tzinfo=timezone.utc)


def test_url_falls_back_to_filing_url():
    f = FakeFiling(form="4", filing_date=date(2026, 10, 29), homepage_url="")
    assert filing_to_event("AAPL", f).url == "https://www.sec.gov/doc.htm"
