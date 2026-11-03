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
    with open(path, "rb") as f:
        data = tomllib.load(f)
    holdings = [
        Holding(ticker=p["ticker"].upper(), weight=float(p.get("weight", 0.0)))
        for p in data.get("position", [])
    ]
    if not holdings:
        raise ValueError(f"No [[position]] entries in {path}")
    return holdings
