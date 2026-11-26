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
      (html_to_text         how one HTML file becomes text, widened until no table is cut)
    fetch_filing_documents  download the wanted files' text (talks to EDGAR)
    trim_documents          cut each to its beginning (logic only)
"""

from __future__ import annotations

import html as html_lib
import logging
import re

from edgar import Document, find
from edgar.richtools import rich_to_text

from daily_digest_desk.ingestion.edgar import _ensure_identity

log = logging.getLogger(__name__)

# How wide a line of text can be when a filing's HTML is laid out, tried in order.
# edgartools' Attachment.text() uses 80 (a terminal's width), and a table too wide
# for it has its cells cut: CleanSpark's Adjusted EBITDA "(112,965)" came out as
# "(11…", so the model saw only the first digits of most earnings tables (found
# Nov 25, 2026: 3,558 cut cells across the eval's earnings filings). At 200, 21 of
# the eval's 22 filings fit; NVDA's securities table needed 250 (Nov 26). 300 and
# 400 are room for wider tables. Not one big width for all: tables stretch to fill
# it, which costs characters (NVDA's EX-99.3 is 14% longer at 400 than at 200).
WIDTHS = (200, 250, 300, 400)

# What a cut table cell ends in. It's an ordinary character, so a filing can use
# it too (Word turns "..." into it as you type); own_marks counts those.
CUT_MARK = "…"

# The parts of a filing's HTML that are never shown, so no "…" in them is laid
# out: tags (with their attributes), scripts, styles, and the hidden XBRL header.
_UNSHOWN = re.compile(r"(?is)<(script|style|ix:header)\b.*?</\1\s*>|<[^>]*>")

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


def own_marks(html: str) -> int:
    """How many CUT_MARKs the filing itself shows, before any layout: written as
    the character, or as &hellip; / &#8230; / &#x2026;. Tags are taken off first,
    then the entities read, so "&lt;b&gt;" can't turn into a tag.

    Example: own_marks('<p title="…">strong &hellip; and rising</p>') is 1."""
    return html_lib.unescape(_UNSHOWN.sub(" ", html)).count(CUT_MARK)


def html_to_text(html: str, widths: tuple[int, ...] = WIDTHS, name: str = "an HTML file") -> str:
    """A filing's HTML as plain text, laid out at the first of `widths` that cuts
    nothing. If every width cuts something, the narrowest with the fewest cuts, and
    a warning that names `name`.

    A cut is a CUT_MARK the layout added: the filing's own (own_marks) don't count,
    so a "…" in a sentence never makes the text wider, nor sets off the warning.

    The spaces that pad each line to its width are taken off. They're most of the
    text otherwise (41,294 characters for CleanSpark's release, 22,624 without),
    and they'd count against MAX_CHARS."""
    if not html.strip():
        return ""  # Document.parse gives None for it, which prints as the word "None"
    document = Document.parse(html)  # once: it can be laid out at any width after
    own = own_marks(html)
    best_cuts, best_text, best_width = None, "", 0
    for width in widths:
        laid_out = rich_to_text(document, width=width)
        text = "\n".join(line.rstrip() for line in laid_out.splitlines())
        cuts = max(text.count(CUT_MARK) - own, 0)
        if cuts == 0:
            return text
        if best_cuts is None or cuts < best_cuts:  # a tie keeps the narrower
            best_cuts, best_text, best_width = cuts, text, width
    log.warning(
        "%s: %d cut table cell(s) left at every width up to %d; kept width %d",
        name, best_cuts, widths[-1], best_width,
    )
    return best_text


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
                text = html_to_text(attachment.content, name=f"{accession_number} {attachment.document_type}")
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
