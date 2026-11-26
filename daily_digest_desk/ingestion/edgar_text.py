"""The text of an EDGAR filing, for the summaries (DESIGN.md section 7).

A filing is a folder of files: the main document, exhibits, and data files for
computers. Only the main document and the EX-99 exhibits (press releases,
presentations) are worth reading for a summary, and only their beginning:
the important part of a press release or prospectus comes first.

Not for 10-Q / 10-K: they open with financial tables, and the part a summary
needs (management's discussion, MD&A) comes later. Those will read that
section by name instead (not built yet).

Three steps, each with one job:
    is_wanted               the rule: which files matter (logic only)
      (html_to_text         how one HTML file becomes text, wide enough for tables)
    fetch_filing_documents  download the wanted files' text (talks to EDGAR)
    trim_documents          cut each to its beginning (logic only)
"""

from __future__ import annotations

from edgar import Document, find
from edgar.richtools import rich_to_text

from daily_digest_desk.ingestion.edgar import _ensure_identity

# How wide a line of text is when a filing's HTML is laid out. edgartools'
# Attachment.text() uses 80 (a terminal's width), and a table too wide for it has
# its cells cut: CleanSpark's Adjusted EBITDA "(112,965)" came out as "(11…", so the
# model saw only the first digits of most earnings tables (found Nov 25, 2026: 3,558
# cut cells across the eval's earnings filings). At 200 nearly all fit.
TEXT_WIDTH = 200

# Per document. Was 40,000 (a starting guess); the v2 eval (Nov 22, 2026) found
# NBIS's shareholder letter cut at 40% of its 98,083 characters, losing its capex,
# ARR and one-off revenue news. 100,000 covers it and most filings' main text; the
# few documents past it are long contracts and financial statements.
MAX_CHARS = 100_000

# Added where a document was cut, so the model knows the rest wasn't sent and
# doesn't write "the filing doesn't say" about what may be past the cut. No
# digits, so it can't be read as a number to check. The prompt names it.
CUT_NOTE = "[Cut off here: the rest of this document was not sent.]"


def is_wanted(form: str, doc_type: str) -> bool:
    """Whether a file is worth reading for a summary: the main document (its type
    is the form itself) or an EX-99 exhibit (press releases, presentations)."""
    return doc_type == form or doc_type.startswith("EX-99")


def html_to_text(html: str, width: int = TEXT_WIDTH) -> str:
    """A filing's HTML as plain text, laid out `width` characters wide, with the
    spaces that pad each line to that width taken off. They're most of the text
    otherwise (41,294 characters for CleanSpark's release, 22,624 without), and
    they'd count against MAX_CHARS."""
    if not html.strip():
        return ""  # Document.parse gives None for it, which prints as the word "None"
    text = rich_to_text(Document.parse(html), width=width)
    return "\n".join(line.rstrip() for line in text.splitlines())


def fetch_filing_documents(accession_number: str, form: str) -> list[tuple[str, str]]:
    """The wanted files of a filing as (document_type, text), in the filing's order.

    The check comes before the download: each file's text is its own request
    to SEC, and most files in a filing (XBRL data, images) are never needed.
    A wanted file with no text (an exhibit that's only pictures) gets "", not None.
    An HTML file goes through html_to_text, so its tables aren't cut; any other
    file keeps edgartools' own text().
    """
    _ensure_identity()
    filing = find(accession_number)
    if filing is None:
        raise ValueError(f"No filing found for accession number {accession_number}")

    docs = []
    for attachment in filing.attachments:
        if is_wanted(form, attachment.document_type):
            if attachment.is_html():
                text = html_to_text(attachment.content)
            else:
                text = attachment.text() or ""  # None (no text) becomes ""
            docs.append((attachment.document_type, text))
    return docs


def trim_documents(
    documents: list[tuple[str, str]],
    max_chars: int = MAX_CHARS,
) -> dict[str, str]:
    """The documents by type, each cut to its first `max_chars`, in the same order.
    A document that was cut ends with CUT_NOTE on a line of its own.

    Example: trim_documents([("8-K", "abcdef"), ("EX-99.1", "gh")], max_chars=3)
    gives {"8-K": "abc\n\n" + CUT_NOTE, "EX-99.1": "gh"}.
    """
    return {
        doc_type: text if len(text) <= max_chars else f"{text[:max_chars]}\n\n{CUT_NOTE}"
        for doc_type, text in documents
    }
