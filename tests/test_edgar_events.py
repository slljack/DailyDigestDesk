import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from daily_digest_desk.ingestion.edgar_events import (
    FORM_DESCRIPTIONS, PROSPECTUS_FORMS, WATCH_FORMS, filing_to_event, insider_line, parse_items,
)
from test_form4 import NVDA_FORM4


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
    # What Filing.xml() gives: the primary document's XML, or None. An exception
    # here is raised instead, like a failed download.
    xml_text: str | Exception | None = None
    xml_calls: int = 0

    def xml(self):
        self.xml_calls += 1
        if isinstance(self.xml_text, Exception):
            raise self.xml_text
        return self.xml_text


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


def test_foreign_current_report_title():
    f = FakeFiling(form="6-K", filing_date=date(2026, 11, 17))
    assert filing_to_event("NBIS", f).title == "NBIS 6-K: Current report (foreign company)"
    amended = FakeFiling(form="6-K/A", filing_date=date(2026, 11, 17))
    assert filing_to_event("NBIS", amended).title == "NBIS 6-K/A (amended): Current report (foreign company)"


def test_automatic_shelf_and_prospectus_titles():
    shelf = FakeFiling(form="S-3ASR", filing_date=date(2026, 11, 17))
    assert filing_to_event("KEEL", shelf).title == "KEEL S-3ASR: Shelf registration (possible dilution)"
    prospectus = FakeFiling(form="424B5", filing_date=date(2026, 11, 17))
    assert filing_to_event("AAOI", prospectus).title == "AAOI 424B5: Prospectus: securities being offered"


def test_the_company_name_is_saved_when_given():
    f = FakeFiling(form="SCHEDULE 13D/A", filing_date=date(2026, 11, 13))
    assert filing_to_event("HUT", f, "Hut 8 Corp.").fields["company"] == "Hut 8 Corp."
    # No name, no key: an event saved before names were collected looks the same.
    assert "company" not in filing_to_event("HUT", f).fields
    assert "company" not in filing_to_event("HUT", f, "").fields


def test_fetch_passes_each_tickers_name_from_its_company(monkeypatch):
    # The name comes from the Company already used to list filings: one per ticker.
    from daily_digest_desk.ingestion import edgar_events

    class FakeCompany:
        def __init__(self, ticker):
            self.name = {"HUT": "Hut 8 Corp.", "AAPL": "Apple Inc."}[ticker]

        def get_filings(self, form, filing_date):
            return [FakeFiling(form="8-K", filing_date=date(2026, 11, 13))]

    looked_up = []
    monkeypatch.setattr(edgar_events, "_company", lambda t: looked_up.append(t) or FakeCompany(t))
    events, errors = edgar_events.fetch_edgar_events(["HUT", "AAPL"], since=date(2026, 11, 1))
    assert errors == {}
    assert [e.fields["company"] for e in events] == ["Hut 8 Corp.", "Apple Inc."]
    assert looked_up == ["HUT", "AAPL"]  # once each: no second request for the name


# --- a Form 4's line, from its XML -------------------------------------------------

NVDA_LINE = (
    "On Nov 19, Teter Timothy S. (EVP, General Counsel and Sec) sold 5,651 shares, "
    "at an average of $146.93 ($146.03 to $147.24), about $830,000. "
    "Under a 10b5-1 plan. Holds 2,950,247 shares after."
)


def form4_filing(xml_text=NVDA_FORM4, form="4"):
    return FakeFiling(form=form, filing_date=date(2026, 11, 23), xml_text=xml_text)


def test_a_form4_gets_its_line():
    assert insider_line(form4_filing()) == NVDA_LINE
    assert insider_line(form4_filing(form="4/A")) == NVDA_LINE  # the XML says "4"; the form is the filing's


def test_other_forms_get_no_line_and_no_request():
    f = FakeFiling(form="8-K", filing_date=date(2026, 11, 23), xml_text=NVDA_FORM4)
    assert insider_line(f) is None
    assert f.xml_calls == 0  # no extra SEC request for an 8-K


def test_a_form4_whose_xml_fails_gets_no_line_and_says_why(caplog):
    caplog.set_level(logging.WARNING)
    for xml_text in [None, "", "<html>Not found</html>", OSError("[Errno 8] nodename nor servname")]:
        assert insider_line(form4_filing(xml_text)) is None, xml_text
    assert [r.getMessage() for r in caplog.records] == [
        "Form 4 line failed for 0000320193-26-000001: no XML in the filing",
        "Form 4 line failed for 0000320193-26-000001: no XML in the filing",
        "Form 4 line failed for 0000320193-26-000001: not a Form 4: <html> with documentType ''",
        "Form 4 line failed for 0000320193-26-000001: [Errno 8] nodename nor servname",
    ]


def test_the_line_is_saved_when_given():
    f = form4_filing()
    assert filing_to_event("NVDA", f, insider=NVDA_LINE).fields["insider"] == NVDA_LINE
    # No line, no key: like a Form 4 saved before lines were made.
    assert "insider" not in filing_to_event("NVDA", f).fields
    assert "insider" not in filing_to_event("NVDA", f, insider="").fields


def test_fetch_saves_a_form4s_line_and_a_failed_line_costs_nothing_else(monkeypatch):
    from daily_digest_desk.ingestion import edgar_events

    class FakeCompany:
        name = "NVIDIA CORP"

        def get_filings(self, form, filing_date):
            return [
                FakeFiling(form="8-K", filing_date=date(2026, 11, 23), accession_number="a"),
                FakeFiling(form="4", filing_date=date(2026, 11, 23), accession_number="b", xml_text=NVDA_FORM4),
                FakeFiling(form="4", filing_date=date(2026, 11, 23), accession_number="c", xml_text=OSError("down")),
            ]

    monkeypatch.setattr(edgar_events, "_company", lambda t: FakeCompany())
    events, errors = edgar_events.fetch_edgar_events(["NVDA"], since=date(2026, 11, 23))
    assert errors == {}  # the failed line isn't the ticker's error
    assert [(e.id, e.fields.get("insider")) for e in events] == [
        ("edgar:a", None), ("edgar:b", NVDA_LINE), ("edgar:c", None),
    ]


def test_every_watched_form_has_a_description():
    # An 8-K is titled by its items; every other watched form needs a description,
    # or its title would just repeat the form name.
    for form in WATCH_FORMS:
        base = form.removesuffix("/A")
        assert base == "8-K" or base in FORM_DESCRIPTIONS, form
    assert {"6-K", "S-3ASR", "424B5"} <= set(WATCH_FORMS) and set(PROSPECTUS_FORMS) <= set(WATCH_FORMS)
