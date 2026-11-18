"""Portfolio holdings, loaded from a TOML file (see holdings.example.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Holding:
    ticker: str
    weight: float  # fraction of portfolio, 0..1; used later for relevance scoring


def load_holdings(path: str | Path) -> list[Holding]:
    """The positions in file order, tickers upper-cased.

    A ticker listed twice is kept once (the first), so nothing is fetched twice.
    A missing or blank ticker is an error, not a silent empty fetch.
    """
    with open(path, "rb") as f:
        data = tomllib.load(f)
    holdings: list[Holding] = []
    seen: set[str] = set()
    for i, position in enumerate(data.get("position", []), start=1):
        ticker = str(position.get("ticker", "")).strip().upper()
        if not ticker:
            raise ValueError(f"Position {i} in {path} has no ticker")
        if ticker in seen:
            continue
        seen.add(ticker)
        holdings.append(Holding(ticker=ticker, weight=float(position.get("weight", 0.0))))
    if not holdings:
        raise ValueError(f"No [[position]] entries in {path}")
    return holdings
