"""SEC EDGAR ingestion (edgartools wrapper).

Public surface:
    fetch_filings(ticker, forms, limit)
    fetch_document(filing)
    fetch_section(filing, item)
    fetch_company_facts(ticker)              -> EntityFacts (cached)
    fetch_concept(ticker, tag, ...)          -> DataFrame
    fetch_metric(ticker, metric, as_of)      -> (tag_used, DataFrame)
    extract_metric(facts, metric, as_of)     -> (tag_used, DataFrame)
    discover_tags(ticker, keyword)           -> DataFrame
    latest_per_period(df)                    -> DataFrame
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import cast

import pandas as pd
from edgar import Company, set_identity

from daily_digest_desk.ingestion.concept_map import CONCEPT_MAP

# edgartools DataFrame column names. Adjust these constants if edgartools
# changes its schema in a future release.
COL_VALUE = "numeric_value"
COL_END = "period_end"
COL_START = "period_start"
COL_FILED = "filing_date"
COL_FY = "fiscal_year"
COL_FP = "fiscal_period"
COL_FORM = "form_type"
COL_UNIT = "unit"
COL_CONCEPT = "concept"


_identity_set = False


def _ensure_identity() -> None:
    global _identity_set
    if _identity_set:
        return
    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        raise RuntimeError(
            "Set EDGAR_USER_AGENT to e.g. 'Your Name your@email.com' (SEC requires this)."
        )
    set_identity(ua)
    _identity_set = True


@lru_cache(maxsize=128)
def _company(ticker: str) -> Company:
    _ensure_identity()
    return Company(ticker)


def fetch_filings(
    ticker: str,
    forms: tuple[str, ...] | list[str] = ("10-K", "10-Q", "8-K"),
    limit: int | None = None,
):
    """Recent filings for a ticker, filtered by form."""
    filings = _company(ticker).get_filings(form=list(forms))
    if limit is not None:
        filings = filings.head(limit)
    return filings


def fetch_document(filing) -> str:
    """Return the filing's primary document as plain text."""
    return filing.text()


def fetch_section(filing, item: str) -> str:
    """Return one item section of a 10-K/10-Q (e.g. 'Item 1A', 'Item 7').

    Replaces the section-parser we were going to write ourselves.
    """
    return filing.obj()[item]


@lru_cache(maxsize=128)
def fetch_company_facts(ticker: str):
    """Return the EntityFacts object for a company. Cached per-ticker.

    Use facts.query() / facts.time_series(...) for ad-hoc queries, or
    pass the result to extract_metric() to use the concept map.
    """
    facts = _company(ticker).get_facts()
    if facts is None:
        raise ValueError(f"No XBRL facts available for {ticker}")
    return facts


def _query_concept(facts, tag: str, taxonomy: str = "us-gaap") -> pd.DataFrame:
    """Run facts.query().by_concept(...).to_dataframe() with a taxonomy-qualified name."""
    qualified = tag if ":" in tag else f"{taxonomy}:{tag}"
    df = facts.query().by_concept(qualified).to_dataframe()
    if df is None:
        return pd.DataFrame()
    return df


def _filter(df: pd.DataFrame, unit: str, as_of: str | None) -> pd.DataFrame:
    if df.empty:
        return df
    if COL_UNIT in df.columns:
        df = cast(pd.DataFrame, df.loc[df[COL_UNIT] == unit, :])
    if as_of and COL_FILED in df.columns:
        df = cast(pd.DataFrame, df.loc[df[COL_FILED].astype(str) <= as_of, :])
    if COL_FILED in df.columns:
        df = df.sort_values(COL_FILED, ascending=False).reset_index(drop=True)
    return df


def fetch_concept(
    ticker: str,
    tag: str,
    unit: str = "USD",
    taxonomy: str = "us-gaap",
    as_of: str | None = None,
) -> pd.DataFrame:
    """Time series for one XBRL concept as a DataFrame, newest first.

    `as_of` (ISO date) restricts to facts already filed by that date.
    """
    facts = fetch_company_facts(ticker)
    df = _query_concept(facts, tag, taxonomy=taxonomy)
    return _filter(df, unit, as_of)


def fetch_metric(
    ticker: str,
    metric: str,
    as_of: str | None = None,
) -> tuple[str, pd.DataFrame]:
    """Resolve a semantic metric via the concept_map fallback chain.

    Returns (tag_used, DataFrame). `tag_used` is the audit trail.
    """
    facts = fetch_company_facts(ticker)
    return extract_metric(facts, metric, as_of=as_of)


def extract_metric(
    facts,
    metric: str,
    as_of: str | None = None,
) -> tuple[str, pd.DataFrame]:
    """Resolve a metric against an in-memory EntityFacts (no extra HTTP)."""
    candidates = CONCEPT_MAP.get(metric)
    if candidates is None:
        raise ValueError(
            f"Unknown metric '{metric}'. Known: {sorted(CONCEPT_MAP)}"
        )
    for c in candidates:
        df = _query_concept(facts, c.tag)
        df = _filter(df, c.unit, as_of)
        if not df.empty:
            return c.tag, df
    raise ValueError(
        f"No candidate tag found for '{metric}'"
        + (f" as of {as_of}" if as_of else "")
        + f". Tried: {[c.tag for c in candidates]}. "
          f"Run discover_tags() to see what this company actually reports."
    )


def latest_per_period(df: pd.DataFrame) -> pd.DataFrame:
    """Dedupe to one row per (start, end), keeping the latest filing.

    Resolves comparative-column duplicates and restatements. Default DataFrame
    return preserves all rows; use this when you want "current truth".
    """
    if df.empty:
        return df
    group_cols = []
    if COL_START in df.columns:
        group_cols.append(COL_START)
    group_cols.append(COL_END)
    deduped = (
        df.sort_values(COL_FILED)
        .groupby(group_cols, as_index=False, dropna=False)
        .tail(1)
        .sort_values(COL_END, ascending=False)
        .reset_index(drop=True)
    )
    return deduped


def discover_tags(ticker: str, keyword: str | None = None) -> pd.DataFrame:
    """List concepts the company reports, with fact counts.

    Use when CONCEPT_MAP doesn't cover what you need.
    """
    facts = fetch_company_facts(ticker)
    df = facts.query().to_dataframe()
    if df is None or df.empty:
        return pd.DataFrame(columns=[COL_CONCEPT, "n"])
    if COL_CONCEPT not in df.columns:
        # Fallback: try 'tag' or whatever edgartools uses for the concept name.
        for alt in ("tag", "name"):
            if alt in df.columns:
                df = df.rename(columns={alt: COL_CONCEPT})
                break
    if keyword and COL_CONCEPT in df.columns:
        df = df[df[COL_CONCEPT].str.contains(keyword, case=False, na=False)]
    return (
        df.groupby(COL_CONCEPT)
        .size()
        .sort_values(ascending=False)
        .reset_index(name="n")
    )
