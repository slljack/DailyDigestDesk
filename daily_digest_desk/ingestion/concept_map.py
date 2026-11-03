"""Curated map from semantic metric names to ordered XBRL tag candidates.

The us-gaap taxonomy is standardized but companies have discretion in which
tag they use for a given economic concept. This map encodes the common
fallback chains for high-level metrics. For anything outside this list,
use `discover_tags()` to find what a specific company actually reports.
"""

from typing import NamedTuple


class TagCandidate(NamedTuple):
    tag: str
    unit: str = "USD"
    taxonomy: str = "us-gaap"


CONCEPT_MAP: dict[str, list[TagCandidate]] = {
    # Income statement
    "revenue": [
        TagCandidate("Revenues"),
        TagCandidate("RevenueFromContractWithCustomerExcludingAssessedTax"),
        TagCandidate("RevenueFromContractWithCustomerIncludingAssessedTax"),
        TagCandidate("SalesRevenueNet"),
    ],
    "gross_profit": [
        TagCandidate("GrossProfit"),
    ],
    "operating_income": [
        TagCandidate("OperatingIncomeLoss"),
    ],
    "net_income": [
        TagCandidate("NetIncomeLoss"),
        TagCandidate("ProfitLoss"),
    ],
    "eps_basic": [
        TagCandidate("EarningsPerShareBasic", unit="USD/shares"),
    ],
    "eps_diluted": [
        TagCandidate("EarningsPerShareDiluted", unit="USD/shares"),
    ],
    # Balance sheet
    "assets": [
        TagCandidate("Assets"),
    ],
    "liabilities": [
        TagCandidate("Liabilities"),
    ],
    "equity": [
        TagCandidate("StockholdersEquity"),
        TagCandidate("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    ],
    "cash": [
        TagCandidate("CashAndCashEquivalentsAtCarryingValue"),
        TagCandidate("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ],
    # Cash flow
    "operating_cash_flow": [
        TagCandidate("NetCashProvidedByUsedInOperatingActivities"),
    ],
    "capex": [
        TagCandidate("PaymentsToAcquirePropertyPlantAndEquipment"),
    ],
    # Shares
    "shares_outstanding": [
        TagCandidate("CommonStockSharesOutstanding", unit="shares"),
        TagCandidate("EntityCommonStockSharesOutstanding", unit="shares", taxonomy="dei"),
    ],
}
