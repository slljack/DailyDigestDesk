import pytest

from daily_digest_desk.ingestion import edgar_text
from daily_digest_desk.ingestion.edgar_text import (
    fetch_filing_documents, is_wanted, trim_documents,
)


# --- is_wanted: the rule -------------------------------------------------------------

def test_wants_the_main_document_whatever_the_form():
    # An 8-K, a 6-K (foreign company), an amendment, a prospectus with no exhibits.
    for form in ("8-K", "6-K", "8-K/A", "424B5"):
        assert is_wanted(form, form)
    assert not is_wanted("6-K", "8-K")  # another form's type isn't the main document


def test_wants_every_ex99_exhibit():
    for doc_type in ("EX-99.1", "EX-99.2", "EX-99.3"):
        assert is_wanted("8-K", doc_type)


def test_does_not_want_other_files():
    for doc_type in (
        "EX-10.1",     # contracts
        "EX-1.1",      # underwriting agreements
        "EX-31.1",     # CEO certifications
        "EX-9.1",      # voting trusts: starts like EX-99, but isn't one
        "EX-101.SCH",  # XBRL data: starts with EX-10, not EX-99
        "GRAPHIC", "XML", "ZIP", "CSS", "JS", "JSON",
    ):
        assert not is_wanted("8-K", doc_type), doc_type


# --- fetch_filing_documents: with a fake EDGAR ---------------------------------------

class FakeAttachment:
    """Stands in for an edgartools Attachment; counts its downloads."""

    def __init__(self, document_type, text):
        self.document_type, self._text, self.downloads = document_type, text, 0

    def text(self):
        self.downloads += 1
        return self._text


class FakeFiling:
    def __init__(self, attachments):
        self.attachments = attachments


# The files of a real NVDA earnings 8-K, by type, with short stand-in text.
def nvda_8k():
    return [
        FakeAttachment("8-K", "Item 2.02 Results of Operations"),
        FakeAttachment("EX-99.1", "NVIDIA Announces Financial Results"),
        FakeAttachment("EX-99.2", "CFO Commentary"),
        FakeAttachment("EX-101.SCH", "<xsd>"),
        FakeAttachment("GRAPHIC", None),  # edgartools gives None for an image
        FakeAttachment("XML", "<FilingSummary>"),
        FakeAttachment("XML", "<xbrl>"),  # a type can repeat
    ]


@pytest.fixture
def fake_edgar(monkeypatch):
    """Answers find() with the filings in `filings`, by accession number; None otherwise."""
    filings = {}
    monkeypatch.setattr(edgar_text, "_ensure_identity", lambda: None)
    monkeypatch.setattr(edgar_text, "find", lambda accession: filings.get(accession))
    return filings


def test_fetch_returns_the_wanted_files_in_order(fake_edgar):
    fake_edgar["0001045810-26-000088"] = FakeFiling(nvda_8k())
    assert fetch_filing_documents("0001045810-26-000088", "8-K") == [
        ("8-K", "Item 2.02 Results of Operations"),
        ("EX-99.1", "NVIDIA Announces Financial Results"),
        ("EX-99.2", "CFO Commentary"),
    ]


def test_fetch_never_downloads_unwanted_files(fake_edgar):
    attachments = nvda_8k()
    fake_edgar["acc"] = FakeFiling(attachments)
    fetch_filing_documents("acc", "8-K")
    assert {a.document_type: a.downloads for a in attachments} == {
        "8-K": 1, "EX-99.1": 1, "EX-99.2": 1,  # each wanted file once, not twice
        "EX-101.SCH": 0, "GRAPHIC": 0, "XML": 0,
    }


def test_fetch_gives_empty_text_for_a_wanted_file_without_text(fake_edgar):
    # An exhibit that's only pictures (a slide deck): listed, with "" instead of None.
    fake_edgar["acc"] = FakeFiling([FakeAttachment("8-K", "a"), FakeAttachment("EX-99.2", None)])
    assert fetch_filing_documents("acc", "8-K") == [("8-K", "a"), ("EX-99.2", "")]


def test_fetch_raises_for_an_unknown_accession_number(fake_edgar):
    with pytest.raises(ValueError, match="0000000000-00-000000"):
        fetch_filing_documents("0000000000-00-000000", "8-K")


# --- trim_documents: the cut ---------------------------------------------------------

def test_trim_cuts_each_document_to_its_beginning():
    docs = [("8-K", "abcdef"), ("EX-99.1", "gh")]
    # Longer than max_chars: cut. Shorter: kept whole.
    assert trim_documents(docs, max_chars=3) == {"8-K": "abc", "EX-99.1": "gh"}


def test_trim_keeps_the_order():
    docs = [("8-K", "a"), ("EX-99.1", "b"), ("EX-99.2", "c")]
    assert list(trim_documents(docs)) == ["8-K", "EX-99.1", "EX-99.2"]  # == on dicts ignores order


def test_trim_defaults_to_max_chars():
    (text,) = trim_documents([("8-K", "x" * (edgar_text.MAX_CHARS + 10))]).values()
    assert len(text) == edgar_text.MAX_CHARS


def test_trim_empty():
    assert trim_documents([]) == {}
