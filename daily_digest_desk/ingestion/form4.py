"""Read a Form 4 (an insider's change in holdings) from its XML.

A Form 4 is data, not prose: each row says what kind of change it was (the
transaction code), how many shares, at what price, and what's held after. So the
page's line for one is built from these fields by code, with no model: it's free,
and it has nothing to make up.

Two steps here, logic only:
    parse_form4  XML text in, a Form4 out. A form it can't read raises
                 Form4Error, and the caller keeps the title-only line.
    describe     a Form4 in, one plain sentence or two out, for the page.

The stdlib parser is fine for XML from EDGAR: it never fetches external entities,
and the expat it ships with (2.4+) stops entity-expansion bombs.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation


class Form4Error(ValueError):
    """The XML isn't a Form 4 this module can read."""


@dataclass(frozen=True)
class Owner:
    name: str               # as filed, last name first: "Teter Timothy S."
    roles: tuple[str, ...]  # "Director", the officer title, "10% owner", or the "other" text


@dataclass(frozen=True)
class Row:
    """One row of either table: a transaction, or a holding with no change (code None)."""

    security: str              # "Common Stock", "Stock Option (Right to Buy)"
    code: str | None           # P, S, M, F, A, G, ...; None for a holding row
    on: date | None            # None for a holding row
    shares: Decimal | None     # None for a holding row
    price: Decimal | None      # None when the form gives none (an award, a gift)
    acquired: bool | None      # True for A (acquired), False for D (disposed)
    owned_after: Decimal | None
    direct: bool               # False: held through someone else (a trust, a spouse)
    held_by: str               # how, when not direct: "By Trust"; "" when direct
    derivative: bool           # an option or other right, not the shares themselves


@dataclass(frozen=True)
class Form4:
    form: str                  # "4", or "4/A" for an amendment
    owners: tuple[Owner, ...]  # usually one; joint filers (a person and their fund) list several
    rows: tuple[Row, ...]      # the shares table first, then the derivative table, in filed order
    plan_10b5_1: bool | None   # the 10b5-1 checkbox; None on forms from before it (April 2023)


def _text(el: ET.Element, path: str) -> str:
    return (el.findtext(path) or "").strip()


def _flag(el: ET.Element, path: str) -> bool:
    # Filers write both: "1"/"0" and "true"/"false".
    return _text(el, path).lower() in ("1", "true")


def _number(el: ET.Element, path: str) -> Decimal | None:
    text = _text(el, path)
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise Form4Error(f"not a number at {path}: {text!r}") from None


def _date(el: ET.Element, path: str) -> date | None:
    text = _text(el, path)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])  # some filers add a time zone: 2026-11-19-05:00
    except ValueError:
        raise Form4Error(f"not a date at {path}: {text!r}") from None


def _owner(el: ET.Element) -> Owner:
    rel = el.find("reportingOwnerRelationship")
    roles: list[str] = []
    if rel is not None:
        if _flag(rel, "isDirector"):
            roles.append("Director")
        if _flag(rel, "isOfficer"):
            roles.append(_text(rel, "officerTitle") or "Officer")
        if _flag(rel, "isTenPercentOwner"):
            roles.append("10% owner")
        if _flag(rel, "isOther"):
            roles.append(_text(rel, "otherText") or "Other")
    return Owner(_text(el, "reportingOwnerId/rptOwnerName"), tuple(roles))


def _row(el: ET.Element, derivative: bool) -> Row:
    # A holding row has no coding, date or amounts, so those come out None.
    acquired_or_disposed = _text(el, "transactionAmounts/transactionAcquiredDisposedCode/value")
    return Row(
        security=_text(el, "securityTitle/value"),
        code=_text(el, "transactionCoding/transactionCode") or None,
        on=_date(el, "transactionDate/value"),
        shares=_number(el, "transactionAmounts/transactionShares/value"),
        price=_number(el, "transactionAmounts/transactionPricePerShare/value"),
        acquired={"A": True, "D": False}.get(acquired_or_disposed),
        owned_after=_number(el, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
        direct=_text(el, "ownershipNature/directOrIndirectOwnership/value") != "I",
        held_by=_text(el, "ownershipNature/natureOfOwnership/value"),
        derivative=derivative,
    )


def parse_form4(xml: str | bytes) -> Form4:
    """The owners, rows and 10b5-1 checkbox of a Form 4 (or 4/A). Raises Form4Error."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise Form4Error(f"not XML: {exc}") from None
    form = _text(root, "documentType")
    if root.tag != "ownershipDocument" or form not in ("4", "4/A"):
        raise Form4Error(f"not a Form 4: <{root.tag}> with documentType {form!r}")

    rows = [_row(el, derivative=False) for el in root.findall("nonDerivativeTable/*")]
    rows += [_row(el, derivative=True) for el in root.findall("derivativeTable/*")]
    checkbox = root.find("aff10b5One")
    return Form4(
        form=form,
        owners=tuple(_owner(el) for el in root.findall("reportingOwner")),
        rows=tuple(rows),
        plan_10b5_1=None if checkbox is None else _flag(root, "aff10b5One"),
    )


# --- describe: the page's line ------------------------------------------------------

# What each transaction code did, for the shares table. P and S are the trading
# decisions; the rest are mostly pay mechanics (options, awards, tax). A code not
# here is said plainly with its letter, so nothing is hidden or guessed.
_DID = {
    ("P", True): "bought {} shares on the open market",
    ("S", False): "sold {} shares",
    ("M", True): "got {} shares by exercising options",
    ("X", True): "got {} shares by exercising options",
    ("C", True): "got {} shares by converting another security",
    ("A", True): "received {} shares as an award",
    ("F", False): "had {} shares withheld to pay tax",
    ("G", False): "gave away {} shares as a gift",
    ("G", True): "received {} shares as a gift",
    ("D", False): "returned {} shares to the company",
}
_PRICED = {"P", "S"}  # the codes whose price and total are worth saying


def _shares(n: Decimal) -> str:
    return f"{n.normalize():,f}"  # 30000 -> "30,000", 100.50 -> "100.5"


def _about(dollars: Decimal) -> str:
    """Rounded for reading: "$4.5 million", "$829,000". The page says "about"."""
    if dollars >= 1_000_000_000:
        return f"${dollars / 1_000_000_000:.1f} billion"
    if dollars >= 1_000_000:
        return f"${dollars / 1_000_000:.1f} million"
    if dollars >= 1_000:
        return f"${round(dollars, -3):,.0f}"
    return f"${dollars:,.0f}"


def _day(d: date) -> str:
    return f"{d:%b} {d.day}"  # "Nov 19", no leading zero


def _prices(rows: list[Row]) -> str:
    """", at an average of $151.43 ($146.03 to $157.49), about $4.5 million", or "" with no prices.

    Each row's price is often itself an average (the footnotes give its range), so
    the range here is from the lowest row's average to the highest's.
    """
    priced = [r for r in rows if r.price is not None and r.shares]
    if not priced:
        return ""
    total = sum(r.shares * r.price for r in priced)
    average = total / sum(r.shares for r in priced)
    low, high = min(r.price for r in priced), max(r.price for r in priced)
    spread = f" (${low:,.2f} to ${high:,.2f})" if low != high else ""
    return f", at an average of ${average:,.2f}{spread}, about {_about(total)}"


def _who(owners: tuple[Owner, ...]) -> str:
    named = [f"{o.name} ({'; '.join(o.roles)})" if o.roles else o.name for o in owners]
    if not named:
        return "An insider"
    if len(named) <= 2:
        return " and ".join(named)
    return f"{named[0]} and {len(named) - 1} other filers"


def _held_after(rows: list[Row]) -> Decimal | None:
    """Shares held after, across every way they're held (direct, a trust, ...).

    The last row of each holding counts, in filed order. None if the form covers
    more than one kind of share (Class A and B don't add up) or gives no totals.
    """
    last: dict[tuple[str, bool, str], Decimal] = {}
    for r in rows:
        if r.owned_after is not None:
            last[(r.security, r.direct, r.held_by)] = r.owned_after
    if not last or len({security for security, _, _ in last}) > 1:
        return None
    return sum(last.values())


def describe(form: Form4) -> str:
    """One plain sentence or two from a Form 4's fields:

    On Nov 19, Teter Timothy S. (EVP, General Counsel and Sec) sold 30,000 shares, at
    an average of $151.43 ($146.03 to $157.49), about $4.5 million. Under a 10b5-1
    plan. Holds 2,925,898 shares after.

    Only the shares table is added up. The derivative table (options) is only
    mentioned: an exercise shows in both tables, and counting both would double it.
    """
    shares = [r for r in form.rows if not r.derivative]
    moves = [r for r in shares if r.code is not None and r.shares is not None]
    options_changed = any(r.derivative and r.code is not None for r in form.rows)

    # Rows with the same code and direction add up, in the order first filed.
    groups: dict[tuple[str, bool | None], list[Row]] = {}
    for r in moves:
        groups.setdefault((r.code, r.acquired), []).append(r)
    clauses = []
    for (code, acquired), rows in groups.items():
        n = _shares(sum(r.shares for r in rows))
        if (code, acquired) in _DID:
            clause = _DID[(code, acquired)].format(n)
        else:
            verb = {True: "acquired", False: "disposed of"}.get(acquired, "reported")
            clause = f"{verb} {n} shares (code {code})"
        clauses.append(clause + (_prices(rows) if code in _PRICED else ""))
    if options_changed:
        clauses.append("changed holdings of options or other rights" if not clauses
                       else "with changes to options or other rights")
    if not clauses:
        clauses.append("reported no transactions")

    dates = sorted({r.on for r in form.rows if r.on is not None})
    when = ""
    if dates:
        when = f"On {_day(dates[0])}, " if len(dates) == 1 else f"From {_day(dates[0])} to {_day(dates[-1])}, "

    sentences = [f"{when}{_who(form.owners)} {'; '.join(clauses)}."]
    if any(r.code in _PRICED for r in moves):
        if form.plan_10b5_1 is True:
            sentences.append("Under a 10b5-1 plan.")
        elif form.plan_10b5_1 is False:
            sentences.append("Not marked as a 10b5-1 plan.")
    held = _held_after(shares)
    if held is not None:
        sentences.append(f"Holds {_shares(held)} shares after.")
    line = " ".join(sentences)
    return f"Amended filing: {line}" if form.form == "4/A" else line
