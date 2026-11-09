# DailyDigestDesk

Daily investing digest for your portfolio: pull SEC filings, FRED macro data and Finnhub prices → rank what matters → grounded summaries → Obsidian notes.

Built on top of [edgartools](https://github.com/dgunning/edgartools) for SEC ingestion, with a curated semantic-metric layer and CLI on top.

## Setup

Pick **one** environment manager — venv or conda — not both.

**venv (stdlib):**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**conda:**
```bash
conda create -n daily-digest-desk python=3.11 -y
conda activate daily-digest-desk
pip install -e .
```

Re-run `pip install -e .` whenever `pyproject.toml` changes.

The install pulls in `edgartools` and `pandas` — heavier than a hand-rolled HTTP client, but you get filing parsing, section extraction, XBRL queries, rate limiting, and CIK resolution for free.

## Configuration

SEC requires every API caller to identify themselves via `User-Agent`. Put yours in `.env`:

```
EDGAR_USER_AGENT=Your Name your@email.com
FRED_API_KEY=your-32-character-key      # optional; free at https://fred.stlouisfed.org/docs/api/api_key.html
FINNHUB_API_KEY=your-key                # optional; free at https://finnhub.io/register
```

Without `FRED_API_KEY` or `FINNHUB_API_KEY`, `desk-collect` skips that source and says so.

`main.py` loads `.env` automatically and passes the value to edgartools' `set_identity()` on first use. If you import the modules from a notebook, call `load_dotenv()` yourself first, or export the variable in your shell.

## CLI

The `desk` command has four modes: filings (default), metric, concept, and discover.

### Filings mode

```bash
desk AAPL                       # last 10 filings (10-K, 10-Q, 8-K)
desk AAPL --limit 5
desk MSFT --forms 10-K          # annual reports only
desk NVDA --forms 10-K 10-Q     # annuals + quarterlies
```

Output is one line per filing: `<filing_date>  <form>  <url>`.

### Metric mode (recommended)

XBRL concept names aren't standardized — companies pick from several candidate tags for the same economic metric. `--metric` uses a curated fallback chain.

```bash
desk CLSK --metric revenue
desk CLSK --metric revenue --form-filter 10-K          # annuals only
desk AAPL --metric net_income --limit 20
desk AAPL --metric eps_diluted
```

Available metrics: `revenue`, `gross_profit`, `operating_income`, `net_income`, `eps_basic`, `eps_diluted`, `assets`, `liabilities`, `equity`, `cash`, `operating_cash_flow`, `capex`, `shares_outstanding`. Defined in [daily_digest_desk/ingestion/concept_map.py](daily_digest_desk/ingestion/concept_map.py).

The first line of output is `# tag=...` — your audit trail showing which tag actually matched.

### Concept mode (exact tag)

```bash
desk CLSK --concept Revenues
desk AAPL --concept EarningsPerShareDiluted --unit USD/shares
```

Flags:
- `--concept TAG` — XBRL tag.
- `--unit UNIT` — defaults to `USD`. EPS-style needs `USD/shares`.
- `--taxonomy TAX` — defaults to `us-gaap`. Use `dei` for entity-level tags.
- `--form-filter FORM` — only show facts from this form.

### Discover mode

```bash
desk CLSK --discover                  # all concepts (long)
desk CLSK --discover Revenue          # only concepts containing "Revenue"
desk CLSK --discover Mining --limit 5
```

Output: `<concept>  n=<count>`. Pick a concept from the list, then run `--concept <tag>`.

### Shared flags (concept and metric modes)

- `--dedupe` — keep one row per `(start, end)`, latest filing wins. Resolves restatements and prior-year comparatives.
- `--as-of YYYY-MM-DD` — restrict to facts already filed by this date (point-in-time view, avoids lookahead bias).

### Output format (concept and metric modes)

```
<period_start>..<period_end>  FY<year><period>  <form>  <value> <unit>  filed=<date>
```

`period_start` is blank for instant facts (balance-sheet items like `Assets`).

## Daily collection (`desk-collect`)

First layer of the daily digest: pull new EDGAR filings, closing prices and earnings for your holdings, plus FRED macro data, into a local SQLite store.

```bash
cp holdings.example.toml holdings.toml     # edit tickers/weights (gitignored)
desk-collect                            # -> data/events.db, prints new events
desk-collect --lookback-days 30         # wider first-run window
```

- Watches 8-K (titled by item, e.g. "Earnings results", "Non-reliance on prior financials"), Form 4, 13D/G, 10-Q/10-K, S-3. See `WATCH_FORMS` in [edgar_events.py](daily_digest_desk/ingestion/edgar_events.py).
- **Idempotent.** Every source emits `Event`s ([models.py](daily_digest_desk/models.py)) with a deterministic id (`edgar:<accession>`); the store ([store.py](daily_digest_desk/store.py)) ignores ids it has seen, so re-running prints only what's new.
- **Per-ticker cursors.** Each ticker remembers the date it was last fetched through; a failed ticker keeps its old cursor and retries next run without blocking the others.
- **FRED** ([fred.py](daily_digest_desk/ingestion/fred.py)). Daily series (`DFF` Fed funds rate, `DGS10` 10-year yield, `VIXCLS` VIX) are stored as readings: history for signal rules, not printed. Monthly releases (`CPIAUCSL` CPI, `PAYEMS` jobs) become one event each, e.g. "CPI (Sep 2026): +0.3% vs prior month".
- **Finnhub** ([finnhub.py](daily_digest_desk/ingestion/finnhub.py)). Each holding's closing price is stored as a reading (`finnhub:<TICKER>`). A run during market hours (weekdays 9:30–16:00 New York) stores no prices, because the price is still moving; run it before the open or after the close. Earnings become two events: a heads-up in the 7 days before a report ("NVDA is reporting earnings on Nov 17 after market close, EPS 2.53 expected") and the result once it's out ("AAPL beat EPS estimate: 2.10 vs 2.03 expected").
- **Failures don't stop the run.** Each failed ticker or series is listed at the end as `# ERROR <source> <name>: <message>`, and `desk-collect` exits 1.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Tests run offline: EDGAR mapping uses fake filing objects, FRED and Finnhub mapping use fake API responses, and the collectors take an injectable fetcher.

## Library usage

The CLI is a thin wrapper over [daily_digest_desk/ingestion/edgar.py](daily_digest_desk/ingestion/edgar.py), which itself wraps edgartools. All XBRL functions return `pandas.DataFrame`s.

### Filings

```python
from daily_digest_desk.ingestion.edgar import fetch_filings, fetch_document, fetch_section

filings = fetch_filings("AAPL", forms=["10-K"], limit=3)
for f in filings:
    print(f.filing_date, f.form, f.accession_number)

# Raw filing text
text = fetch_document(filings[0])

# Or extract just one section — Item 1A Risk Factors, Item 7 MD&A, etc.
risk_factors = fetch_section(filings[0], "Item 1A")
mda          = fetch_section(filings[0], "Item 7")
```

`fetch_section` is the bridge to narrative-extraction work in [reasoning/grounding.py](daily_digest_desk/reasoning/grounding.py): edgartools cleanly splits a 10-K into items, so we don't need a custom HTML parser.

### Structured XBRL data

```python
from daily_digest_desk.ingestion.edgar import (
    extract_metric,
    fetch_company_facts,
    fetch_concept,
    fetch_metric,
)

# One specific XBRL tag, full history
df = fetch_concept("AAPL", "Revenues")
print(df.head())

# Semantic metric via concept_map fallback chain
tag_used, df = fetch_metric("CLSK", "revenue")
print(f"matched: {tag_used}")

# Pull the whole facts object once, extract many metrics locally (no extra HTTP)
facts = fetch_company_facts("CLSK")
rev_tag, revenue   = extract_metric(facts, "revenue")
ni_tag,  net_inc   = extract_metric(facts, "net_income")
```

DataFrame columns include `numeric_value`, `period_start`, `period_end`, `fiscal_year`, `fiscal_period`, `form_type`, `filing_date`, `unit`, `concept`, `label`. Column-name constants are exported from `edgar.py` (`COL_VALUE`, `COL_END`, etc.) so you can reference them robustly even if edgartools' schema shifts.

### Why XBRL data has duplicates

The same fiscal period can appear up to ~7 times. Three drivers:

1. **GAAP comparative columns.** A 10-K must show 3 years; a 10-Q reports current Q + prior-year Q + YTD + prior-year YTD. Every cell is a separate XBRL fact.
2. **Quarterlies report two spans.** A 10-Q tags both the 3-month quarter alone and the cumulative YTD (e.g. 9 months for Q3). Same `period_end`, different `period_start`.
3. **Restatements.** Same period re-reported with a corrected `numeric_value` in a later filing.

Use `latest_per_period` (or CLI `--dedupe`) to keep one row per period, latest filing wins:

```python
from daily_digest_desk.ingestion.edgar import fetch_concept, latest_per_period

clean = latest_per_period(fetch_concept("AAPL", "Revenues"))
```

For audit/forensics — **don't** dedupe. Differing `numeric_value` at the same `(period_start, period_end)` is the restatement signal.

### Point-in-time queries (`as_of`)

Answer "what was knowable on this date" — essential for backtesting:

```python
tag, df = fetch_metric("CLSK", "revenue", as_of="2024-09-30")
```

Filters facts where `filing_date <= as_of` *before* dedupe. Combined with `--dedupe`, this is the right setup to avoid lookahead bias.

### Caching

`fetch_company_facts` and the underlying `Company` objects are both `lru_cache`'d for the process lifetime. Pulling many metrics for one ticker is one effective fetch. The cache resets when the process exits — no on-disk persistence — and a stale cache won't pick up new filings that drop mid-session.

## Caveats

- **`fetch_filings` window.** edgartools queries SEC's submissions endpoint, which returns the recent filing window (~last year for frequent filers). For deeper filing-metadata history, edgartools has its own pagination strategies — consult their docs if you hit the limit. `fetch_concept` / `fetch_company_facts` return full multi-year XBRL history regardless.
- **DataFrame column names depend on edgartools' schema.** Column-name constants are centralized in [edgar.py](daily_digest_desk/ingestion/edgar.py) (`COL_VALUE`, `COL_END`, etc.). If edgartools changes schema, update them in one place.
- **Cache staleness.** New filings published mid-session won't be visible until the process restarts.
- **Narrative content vs structured data.** XBRL covers financial statements thoroughly. MD&A, Risk Factors, and 8-K narrative are mostly prose with light tagging — get them via `fetch_section(filing, "Item ...")` and pass to LLM extraction in [reasoning/grounding.py](daily_digest_desk/reasoning/grounding.py) (still a stub).

## Project layout

```
daily_digest_desk/
  main.py                 # CLI entry point (`desk`)
  collect.py              # Daily collector (`desk-collect`)
  models.py               # Data models: Event, Reading
  store.py                # SQLite store: events, readings, cursors
  holdings.py             # holdings.toml loader
  ingestion/
    edgar.py              # edgartools wrapper (filings + XBRL + sections)
    edgar_events.py       # EDGAR filings -> Events (8-K item labels, forms)
    fred.py               # FRED series -> Readings (daily) and Events (monthly releases)
    finnhub.py            # Finnhub quotes -> closing-price Readings; earnings -> Events
    concept_map.py        # Semantic metric -> XBRL tag fallback chains
  reasoning/
    grounding.py          # (stub) ground claims in source filings
  output/
    obsidian.py           # (stub) write Obsidian notes
tests/                    # offline pytest suite
```
