"""SEC form names that come in families, as they appear in EDGAR's index.

Shared by the EDGAR source (which forms to fetch) and the digest page (how to
group and explain them). No other imports, so the page can use these without
loading edgartools.
"""

from __future__ import annotations

# Big-holder filings. SEC renamed them in 2024 ("SC 13D" -> "SCHEDULE 13D") when it
# moved them to structured XML; both spellings appear.
SCHEDULE_13D_FORMS: tuple[str, ...] = ("SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A")
SCHEDULE_13G_FORMS: tuple[str, ...] = ("SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A")

# 424B prospectuses: the numbered rule each was filed under. 424B5 is the common
# one for selling shares off a shelf; 424B3 often updates an earlier prospectus.
PROSPECTUS_FORMS: tuple[str, ...] = (
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8",
)
