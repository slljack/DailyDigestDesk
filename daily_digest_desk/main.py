"""Entry point for the desk CLI (filings and XBRL queries)."""

import argparse

import pandas as pd
from dotenv import load_dotenv

from daily_digest_desk.ingestion.concept_map import CONCEPT_MAP
from daily_digest_desk.ingestion.edgar import (
    COL_END,
    COL_FILED,
    COL_FORM,
    COL_FP,
    COL_FY,
    COL_START,
    COL_UNIT,
    COL_VALUE,
    discover_tags,
    fetch_concept,
    fetch_filings,
    fetch_metric,
    latest_per_period,
)


def _print_facts(df: pd.DataFrame, tag_used: str | None = None, limit: int | None = None) -> None:
    if tag_used:
        print(f"# tag={tag_used}")
    if df.empty:
        print("(no rows)")
        return
    if limit:
        df = df.head(limit)
    for _, row in df.iterrows():
        start = row.get(COL_START) or "          "
        fp = row.get(COL_FP) or ""
        print(
            f"{start}..{row.get(COL_END, '')}  "
            f"FY{row.get(COL_FY, '')}{fp:>3s}  "
            f"{row.get(COL_FORM, ''):5s}  "
            f"{row.get(COL_VALUE, 0.0):>20,.2f} {row.get(COL_UNIT, '')}  "
            f"filed={row.get(COL_FILED, '')}"
        )


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Pull SEC filings or XBRL data for a ticker."
    )
    parser.add_argument("ticker", help="e.g. AAPL")
    parser.add_argument("--limit", type=int, default=10)

    # Filings mode (default)
    parser.add_argument(
        "--forms",
        nargs="+",
        default=["10-K", "10-Q", "8-K"],
        help="Filing forms to include (default: 10-K 10-Q 8-K).",
    )

    # Mode flags (mutually exclusive)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--concept",
        help="Exact XBRL tag, e.g. Revenues, NetIncomeLoss.",
    )
    mode.add_argument(
        "--metric",
        choices=sorted(CONCEPT_MAP),
        help="Semantic metric name (uses concept_map fallback chain).",
    )
    mode.add_argument(
        "--discover",
        nargs="?",
        const="",
        metavar="KEYWORD",
        help="List all tags this company reports. Optional keyword filter.",
    )

    parser.add_argument("--unit", default="USD", help="XBRL unit (default: USD).")
    parser.add_argument("--taxonomy", default="us-gaap")
    parser.add_argument(
        "--form-filter",
        help="Only show facts from this form (e.g. 10-K).",
    )
    parser.add_argument(
        "--dedupe",
        action="store_true",
        help="Keep one row per (start, end) period, latest filing wins. "
             "Resolves restatements and prior-year comparatives.",
    )
    parser.add_argument(
        "--as-of",
        metavar="YYYY-MM-DD",
        help="Restrict to facts already filed by this date "
             "(point-in-time view, avoids lookahead bias).",
    )

    args = parser.parse_args()

    if args.metric:
        tag_used, df = fetch_metric(args.ticker, args.metric, as_of=args.as_of)
        if args.form_filter and COL_FORM in df.columns:
            df = df.loc[df[COL_FORM] == args.form_filter, :]
        if args.dedupe:
            df = latest_per_period(df)
        _print_facts(df, tag_used=tag_used, limit=args.limit)

    elif args.concept:
        df = fetch_concept(
            args.ticker,
            args.concept,
            unit=args.unit,
            taxonomy=args.taxonomy,
            as_of=args.as_of,
        )
        if args.form_filter and COL_FORM in df.columns:
            df = df.loc[df[COL_FORM] == args.form_filter, :]
        if args.dedupe:
            df = latest_per_period(df)
        _print_facts(df, limit=args.limit)

    elif args.discover is not None:
        df = discover_tags(args.ticker, keyword=args.discover or None)
        if df.empty:
            print("(no tags matched)")
            return
        if args.limit:
            df = df.head(args.limit)
        for _, row in df.iterrows():
            print(f"{row.iloc[0]:60s}  n={row['n']}")

    else:
        filings = fetch_filings(args.ticker, forms=tuple(args.forms), limit=args.limit)
        for f in filings:
            url = getattr(f, "filing_url", None) or getattr(f, "primary_doc_url", "")
            print(f"{f.filing_date}  {f.form:5s}  {url}")


if __name__ == "__main__":
    main()
