from datetime import date
from decimal import Decimal

import pytest

from daily_digest_desk.ingestion.form4 import Form4, Form4Error, Owner, Row, _about, describe, parse_form4

# NVDA's Form 4 filed Nov 23, 2026 (0001696841-26-000016), trimmed: the first 2 of
# its 12 sales, the holding row, and no address or footnotes. Spacing as filed.
NVDA_FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
    <schemaVersion>X0609</schemaVersion>
    <documentType>4</documentType>
    <periodOfReport>2026-11-19</periodOfReport>
    <issuer>
        <issuerCik>0001045810</issuerCik>
        <issuerName>NVIDIA CORP</issuerName>
        <issuerTradingSymbol>NVDA</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0001696841</rptOwnerCik>
            <rptOwnerName>Teter Timothy S.</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>0</isDirector>
            <isOfficer>1</isOfficer>
            <isTenPercentOwner>0</isTenPercentOwner>
            <isOther>0</isOther>
            <officerTitle>EVP, General Counsel and Sec</officerTitle>
            <otherText></otherText>
        </reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>1</aff10b5One>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-11-19</value></transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>S</transactionCode>
                <equitySwapInvolved>0</equitySwapInvolved>
                <footnoteId id="F1"/>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>1434</value></transactionShares>
                <transactionPricePerShare><value>146.0298</value><footnoteId id="F2"/></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction><value>2686226</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>
                <natureOfOwnership><value>By Trust</value><footnoteId id="F3"/></natureOfOwnership>
            </ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-11-19</value></transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>S</transactionCode>
                <equitySwapInvolved>0</equitySwapInvolved>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>4217</value></transactionShares>
                <transactionPricePerShare><value>147.2413</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction><value>2682009</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>
                <natureOfOwnership><value>By Trust</value></natureOfOwnership>
            </ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeHolding>
            <securityTitle><value>Common Stock</value></securityTitle>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction><value>268238</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
            </ownershipNature>
        </nonDerivativeHolding>
    </nonDerivativeTable>
    <derivativeTable></derivativeTable>
</ownershipDocument>
"""


def test_the_nvda_form4():
    form = parse_form4(NVDA_FORM4)
    assert form.form == "4"
    assert form.owners == (Owner("Teter Timothy S.", ("EVP, General Counsel and Sec",)),)
    assert form.plan_10b5_1 is True
    assert form.rows == (
        Row("Common Stock", "S", date(2026, 11, 19), Decimal("1434"), Decimal("146.0298"),
            False, Decimal("2686226"), False, "By Trust", False),
        Row("Common Stock", "S", date(2026, 11, 19), Decimal("4217"), Decimal("147.2413"),
            False, Decimal("2682009"), False, "By Trust", False),
        # The holding row: shares held directly, with no change on this form.
        Row("Common Stock", None, None, None, None, None, Decimal("268238"), True, "", False),
    )


def test_bytes_with_an_encoding_declaration():
    # What a download gives: bytes, often declaring UTF-8.
    xml = NVDA_FORM4.replace('<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-8"?>')
    assert parse_form4(xml.encode()).owners[0].name == "Teter Timothy S."


# --- the other shapes a Form 4 comes in -----------------------------------------

def form4(body: str, document_type: str = "4", checkbox: str | None = "0") -> str:
    """A small Form 4: `body` is the owners and tables. `checkbox` None leaves it out."""
    aff = "" if checkbox is None else f"<aff10b5One>{checkbox}</aff10b5One>"
    return f"<ownershipDocument><documentType>{document_type}</documentType>{aff}{body}</ownershipDocument>"


def owner(name="Doe Jane", director="0", officer="0", ten="0", other="0", title="", other_text=""):
    return (
        f"<reportingOwner><reportingOwnerId><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>"
        "<reportingOwnerRelationship>"
        f"<isDirector>{director}</isDirector><isOfficer>{officer}</isOfficer>"
        f"<isTenPercentOwner>{ten}</isTenPercentOwner><isOther>{other}</isOther>"
        f"<officerTitle>{title}</officerTitle><otherText>{other_text}</otherText>"
        "</reportingOwnerRelationship></reportingOwner>"
    )


def transaction(tag="nonDerivativeTransaction", code="P", shares="100", price="10", ad="A",
                after="1100", direct="D", nature="", on="2026-11-19"):
    price_xml = "" if price is None else f"<transactionPricePerShare><value>{price}</value></transactionPricePerShare>"
    return (
        f"<{tag}><securityTitle><value>Common Stock</value></securityTitle>"
        f"<transactionDate><value>{on}</value></transactionDate>"
        f"<transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>"
        f"<transactionAmounts><transactionShares><value>{shares}</value></transactionShares>{price_xml}"
        f"<transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode></transactionAmounts>"
        f"<postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value>"
        "</sharesOwnedFollowingTransaction></postTransactionAmounts>"
        f"<ownershipNature><directOrIndirectOwnership><value>{direct}</value></directOrIndirectOwnership>"
        f"<natureOfOwnership><value>{nature}</value></natureOfOwnership></ownershipNature></{tag}>"
    )


def test_every_role_in_order_and_true_false_flags():
    xml = form4(owner(director="true", officer="1", ten="1", other="1", title="CEO", other_text="Founder"))
    assert parse_form4(xml).owners[0].roles == ("Director", "CEO", "10% owner", "Founder")


def test_a_role_with_no_title_gets_a_plain_name():
    xml = form4(owner(officer="1", other="1"))
    assert parse_form4(xml).owners[0].roles == ("Officer", "Other")


def test_joint_filers_are_all_owners():
    # A person and the fund they run, on one form.
    xml = form4(owner("Fund LP", ten="1") + owner("Doe Jane", director="1"))
    assert [o.name for o in parse_form4(xml).owners] == ["Fund LP", "Doe Jane"]


def test_the_derivative_table_comes_after_the_shares_table():
    # An option exercise: the option row (M, disposed) and the shares it became.
    xml = form4(
        "<derivativeTable>"
        + transaction("derivativeTransaction", code="M", price="25", ad="D", after="0")
        + "</derivativeTable><nonDerivativeTable>"
        + transaction(code="M", price="25", ad="A")
        + "</nonDerivativeTable>"
    )
    rows = parse_form4(xml).rows
    assert [(r.code, r.acquired, r.derivative) for r in rows] == [("M", True, False), ("M", False, True)]


def test_a_row_without_a_price_has_none():
    # An award (A) or a gift (G) often gives no price at all.
    rows = parse_form4(form4(f"<nonDerivativeTable>{transaction(code='G', price=None, ad='D')}</nonDerivativeTable>")).rows
    assert rows[0].price is None and rows[0].shares == Decimal("100")


def test_a_date_with_a_time_zone():
    xml = form4(f"<nonDerivativeTable>{transaction(on='2026-11-19-05:00')}</nonDerivativeTable>")
    assert parse_form4(xml).rows[0].on == date(2026, 11, 19)


@pytest.mark.parametrize(("checkbox", "expected"), [("1", True), ("true", True), ("0", False), ("false", False), (None, None)])
def test_the_10b5_1_checkbox(checkbox, expected):
    # None: a form from before April 2023, when the checkbox didn't exist yet.
    assert parse_form4(form4("", checkbox=checkbox)).plan_10b5_1 is expected


def test_an_amendment():
    assert parse_form4(form4("", document_type="4/A")).form == "4/A"


# --- what it can't read ------------------------------------------------------------

@pytest.mark.parametrize("xml", [
    "",
    "not xml at all",
    "<ownershipDocument><documentType>4</documentType>",  # cut off
    form4("", document_type="3"),                          # a Form 3, not a 4
    form4("", document_type="5"),
    "<html><body>Not found</body></html>",                 # an error page
    "<edgarSubmission><documentType>4</documentType></edgarSubmission>",  # another form's root
])
def test_anything_else_is_a_form4_error(xml):
    with pytest.raises(Form4Error):
        parse_form4(xml)


@pytest.mark.parametrize("bad", [
    transaction(shares="1,434"),
    transaction(price="$146.03"),
    transaction(on="11/19/2026"),
])
def test_a_number_or_date_it_cant_read_is_a_form4_error(bad):
    with pytest.raises(Form4Error):
        parse_form4(form4(f"<nonDerivativeTable>{bad}</nonDerivativeTable>"))


# --- describe: the page's line ------------------------------------------------------

def test_describe_the_nvda_form4():
    # The trimmed copy: 2 of the 12 sales. Average (1434 x 146.0298 + 4217 x 147.2413)
    # / 5651 = 146.93; total $830,323. Held after: the trust's last row plus the direct holding.
    assert describe(parse_form4(NVDA_FORM4)) == (
        "On Nov 19, Teter Timothy S. (EVP, General Counsel and Sec) sold 5,651 shares, "
        "at an average of $146.93 ($146.03 to $147.24), about $830,000. "
        "Under a 10b5-1 plan. Holds 2,950,247 shares after."
    )


def row(code="S", shares="100", price="10", acquired=False, after=None, security="Common Stock",
        direct=True, held_by="", derivative=False, on=date(2026, 11, 19)):
    return Row(security, code, on, None if shares is None else Decimal(shares),
               None if price is None else Decimal(price), acquired,
               None if after is None else Decimal(after), direct, held_by, derivative)


JANE = (Owner("Doe Jane", ("CEO",)),)


def line(*rows, owners=JANE, plan=None, form="4"):
    return describe(Form4(form, owners, rows, plan))


def test_an_option_exercise_is_counted_once_and_the_codes_keep_their_order():
    # Exercise 1,000 options, sell 600, 400 withheld for tax. The exercise is in both
    # tables; only the shares table adds up.
    assert line(
        row("M", "1000", "20", acquired=True),
        row("S", "600", "50"),
        row("F", "400", "50"),
        row("M", "1000", "20", acquired=False, derivative=True, security="Stock Option (Right to Buy)"),
        plan=True,
    ) == (
        "On Nov 19, Doe Jane (CEO) got 1,000 shares from their Stock Option (Right to Buy); "
        "sold 600 shares, at an average of $50.00, about $30,000; "
        "had 400 shares withheld to pay tax; with changes to options or other rights. "
        "Under a 10b5-1 plan."
    )


def test_a_purchase_not_under_a_plan():
    assert line(row("P", "2000", "12.5", acquired=True, after="5000"), plan=False) == (
        "On Nov 19, Doe Jane (CEO) bought 2,000 shares on the open market, at an average "
        "of $12.50, about $25,000. Not marked as a 10b5-1 plan. Holds 5,000 shares after."
    )


def test_no_plan_sentence_without_a_trade_or_a_checkbox():
    # An award isn't a trade, so the checkbox isn't mentioned; a pre-2023 sale has none.
    assert "10b5-1" not in line(row("A", "500", None, acquired=True), plan=False)
    assert "10b5-1" not in line(row("S", "500", "10"), plan=None)


def test_an_award_has_no_price_even_if_one_is_given():
    # Awards are often filed at $0: a price there isn't news.
    assert line(row("A", "500", "0", acquired=True)) == "On Nov 19, Doe Jane (CEO) received 500 shares as an award."


def test_gifts_both_ways():
    assert "gave away 10 shares as a gift" in line(row("G", "10", None, acquired=False))
    assert "received 10 shares as a gift" in line(row("G", "10", None, acquired=True))


def test_a_code_it_doesnt_know_is_said_with_its_letter():
    assert "acquired 7 shares (code J)" in line(row("J", "7", None, acquired=True))
    assert "disposed of 7 shares (code J)" in line(row("J", "7", None, acquired=False))
    assert "reported 7 shares (code J)" in line(row("J", "7", None, acquired=None))
    # A known code going the unexpected way isn't forced into its usual words.
    assert "acquired 7 shares (code S)" in line(row("S", "7", "1", acquired=True))


def test_rows_without_a_price_are_left_out_of_the_average():
    text = line(row("S", "100", "10"), row("S", "300", None))
    assert "sold 400 shares, at an average of $10.00, about $1,000." in text


def test_one_price_has_no_range_and_a_fraction_of_a_share_stays():
    assert "sold 100.5 shares, at an average of $10.00, about $1,000." in line(row("S", "100.50", "10"))


def test_dates_over_several_days():
    text = line(row(on=date(2026, 11, 17)), row(on=date(2026, 11, 19)), row(on=date(2026, 11, 18)))
    assert text.startswith("From Nov 17 to Nov 19, Doe Jane")


def test_owners():
    fund, plain = Owner("Fund LP", ("10% owner",)), Owner("Roe Rick", ())
    assert line(row(), owners=(fund, JANE[0])).startswith("On Nov 19, Fund LP (10% owner) and Doe Jane (CEO) sold")
    assert line(row(), owners=(fund, plain, JANE[0])).startswith("On Nov 19, Fund LP (10% owner) and 2 other filers sold")
    assert line(row(), owners=(plain,)).startswith("On Nov 19, Roe Rick sold")
    assert line(row(), owners=()).startswith("On Nov 19, An insider sold")
    # Several roles: a title may hold a comma, so roles are split by "; ".
    both = Owner("Doe Jane", ("Director", "EVP, General Counsel"))
    assert "Doe Jane (Director; EVP, General Counsel) sold" in line(row(), owners=(both,))


def test_only_options_or_nothing_at_all():
    assert line(row("A", "1000", "0", acquired=True, derivative=True)) == (
        "On Nov 19, Doe Jane (CEO) changed holdings of options or other rights."
    )
    assert line(row(None, None, None, acquired=None, after="900", on=None)) == (
        "Doe Jane (CEO) reported no transactions. Holds 900 shares after."
    )


def test_held_after_adds_the_last_row_of_each_holding():
    assert "Holds 1,500 shares after." in line(
        row(after="1100", direct=False, held_by="By Trust"),
        row(after="1000", direct=False, held_by="By Trust"),  # the trust's last row
        row(None, None, None, acquired=None, after="500"),    # held directly, no change
    )


def test_held_after_is_left_out_for_two_kinds_of_share_or_no_totals():
    assert "Holds" not in line(row(after="100", security="Class A"), row(after="100", security="Class B"))
    assert "Holds" not in line(row(after=None))
    # Options held after don't count as shares.
    assert "Holds" not in line(row("M", "1", "1", acquired=False, after="50", derivative=True))


def test_an_amendment_says_so():
    assert line(row(), form="4/A").startswith("Amended filing: On Nov 19, Doe Jane")


@pytest.mark.parametrize(("dollars", "text"), [
    ("999", "$999"),
    ("1000", "$1,000"),
    ("829499", "$829,000"),
    ("999999", "$1,000,000"),
    ("1000000", "$1.0 million"),
    ("4542781", "$4.5 million"),
    ("1250000000", "$1.2 billion"),  # half to even, as Decimal rounds
])
def test_about(dollars, text):
    assert _about(Decimal(dollars)) == text


def test_shares_from_stock_units_say_so():
    # HUT, Nov 2026: code M from Performance Stock Units, not options.
    assert line(
        row("M", "1517367", "0", acquired=True, after="7843779"),
        row("M", "505789", "0", acquired=False, after="0", derivative=True, security="Performance Stock Units"),
    ) == (
        "On Nov 19, Doe Jane (CEO) got 1,517,367 shares from their Performance Stock Units; "
        "with changes to options or other rights. Holds 7,843,779 shares after."
    )


def test_shares_from_two_kinds_of_derivative_name_both_once():
    text = line(
        row("M", "300", "0", acquired=True),
        row("M", "100", "0", acquired=False, derivative=True, security="Restricted Stock Units"),
        row("M", "200", "5", acquired=False, derivative=True, security="Stock Options"),
        row("M", "50", "0", acquired=False, derivative=True, security="Restricted Stock Units"),
    )
    assert "got 300 shares from their Restricted Stock Units and Stock Options;" in text


def test_without_a_derivative_row_the_words_dont_guess():
    # M: exercise or conversion, so both. X is an option exercise by definition.
    assert "got 10 shares by exercising or converting options or stock units." in line(row("M", "10", "0", acquired=True))
    assert "got 10 shares by exercising options." in line(row("X", "10", "5", acquired=True))
    # A derivative row of another code doesn't name the source of an M.
    text = line(row("M", "10", "0", acquired=True), row("A", "99", "0", acquired=True, derivative=True, security="Stock Options"))
    assert "by exercising or converting options or stock units" in text


def test_extra_spaces_in_a_title_dont_split_a_holding():
    # KEEL, Nov 2026: "Common  Shares" then "Common Shares", in one form.
    xml = form4(
        "<nonDerivativeTable>"
        + transaction(code="P", after="94568").replace("Common Stock", "Common  Shares")
        + transaction(code="P", after="109568").replace("Common Stock", "Common Shares")
        + "</nonDerivativeTable>"
    )
    form = parse_form4(xml)
    assert {r.security for r in form.rows} == {"Common Shares"}
    assert describe(form).endswith("Holds 109,568 shares after.")
