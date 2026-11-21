# DailyDigestDesk

Daily investing digest for your portfolio: pull SEC filings, FRED macro data and Finnhub prices → signals → a digest page you open in the browser.

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
ANTHROPIC_API_KEY=your-key              # optional; for filing summaries; paid, https://console.anthropic.com
```

Without `FRED_API_KEY`, `FINNHUB_API_KEY` or `ANTHROPIC_API_KEY`, `desk-collect` skips that source and says so.

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
desk-collect                            # -> data/events.db and data/digests/<date-time>.html
desk-collect --lookback-days 30         # wider first-run window
```

- Watches 8-K (titled by item, e.g. "Earnings results", "Non-reliance on prior financials"), 6-K (a foreign company's 8-K, e.g. NBIS; no item numbers, so the title can't say what it's about), Form 4, 13D/G, 10-Q/10-K, and share offerings: S-3, S-3ASR (the automatic shelf for large companies) and 424B prospectuses (securities actually being offered). See `WATCH_FORMS` in [edgar_events.py](daily_digest_desk/ingestion/edgar_events.py).
- **Idempotent.** Every source emits `Event`s ([models.py](daily_digest_desk/models.py)) with a deterministic id (`edgar:<accession>`); the store ([store.py](daily_digest_desk/store.py)) ignores ids it has seen, so re-running prints only what's new.
- **Per-ticker cursors.** Each ticker remembers the date it was last fetched through; a failed ticker keeps its old cursor and retries next run without blocking the others.
- **FRED** ([fred.py](daily_digest_desk/ingestion/fred.py)). Daily series (`DFF` Fed funds rate, `DGS10` 10-year yield, `VIXCLS` VIX) are stored as readings: history for signal rules, not printed. Monthly releases (`CPIAUCSL` CPI, `PAYEMS` jobs) become one event each, e.g. "CPI (Sep 2026): +0.3% vs prior month".
- **Finnhub** ([finnhub.py](daily_digest_desk/ingestion/finnhub.py)). Each holding's closing price is stored as a reading (`finnhub:<TICKER>`), plus SPY's, an S&P 500 fund used as the market benchmark. A run during market hours (weekdays 9:30–16:00 New York) stores no prices, because the price is still moving; run it before the open or after the close. Earnings become two events: a heads-up in the 7 days before a report ("NVDA is reporting earnings on Nov 17 after market close, EPS 2.53 expected") and the result once it's out ("AAPL beat EPS estimate: 2.10 vs 2.03 expected").
- **Signals** ([signals.py](daily_digest_desk/signals.py)). After collecting, each new reading is compared with the one before it, and a big move becomes an event: "10-year Treasury yield up 0.12 points to 5.10%", "NVDA down 6.21% to $187.40". Thresholds: 0.10 points for the 10-year yield, 0.20 for Fed funds, 3 for VIX, 2% for the S&P 500 (SPY, shown under Macro & Market), 5% for each holding. Only readings from the past week are checked, so a first run doesn't report old moves.
- **Summaries** ([summarize.py](daily_digest_desk/reasoning/summarize.py)). Filings from the past week with no summary yet (8-K, 6-K, 13D, S-3, 424B; `SUMMARY_FORMS`) get 2-3 plain-English sentences from Claude, plus the key numbers, each with the exact sentence it came from. Code checks every quote is in the filing word for word and every number is in a quote; a summary that fails gets one retry, told what was wrong, and is dropped if it fails again (listed as `# ERROR summaries ...`, tried again next run). Saved in the `summaries` table. Newest first, up to `SUMMARY_CAP_DOLLARS` ($2) a run; the run prints how many it wrote and about what they cost. Not shown on the page yet.
- **Digest page** ([digest.py](daily_digest_desk/output/digest.py)). Each run ends by writing `data/digests/<New York date-time>.html` (and the same page as `latest.html`) with everything stored since the last page (the first page covers the past week). Macro & Market first, then each holding with something new; insider transactions and big-holder filings fold into one click-to-open line. Click a line's ⓘ for a short explainer card (what it is, why stocks care), written in [explainers.toml](daily_digest_desk/output/explainers.toml). Hover a dotted-underlined word (EPS, yield, guidance…) for a one-line definition from [glossary.toml](daily_digest_desk/output/glossary.toml). Red flags (an 8-K about bankruptcy, an impairment, a delisting notice, an auditor change or non-reliance on past financials) get a red ⚠️ label, go first in their section, and are counted in the header and the pop-up. The run prints the page's path; open it in a browser. `--digest-dir` writes it somewhere else.
- **Failures don't stop the run.** Each failed ticker or series is listed at the end as `# ERROR <source> <name>: <message>`, and `desk-collect` exits 1.

## Scheduling (macOS)

Run `desk-collect` automatically on weekdays at 8:00am and 5:00pm New York time (before the open, after the close), with a pop-up when the page is ready.

```bash
scripts/install-schedule.sh ~/miniconda3/envs/daily-digest-desk/bin/desk-collect
launchctl kickstart gui/$(id -u)/com.dailydigestdesk.collect     # run once now to test
```

- **launchd, not cron.** If the Mac is asleep at the scheduled time, launchd runs the job when it wakes; cron would skip it. A Mac that's shut down skips the run, but nothing is lost: the next page shows everything since the last one.
- **Weekdays only.** Weekend filings land on Monday's morning page.
- **Local time.** launchd uses the Mac's clock, so the installer converts 8:00 and 17:00 New York into this Mac's time zone (6:00 and 15:00 in Denver). Rerun it after changing time zones or moving the project folder.
- **The template is in the repo; the real job isn't.** [scripts/com.dailydigestdesk.collect.plist.template](scripts/com.dailydigestdesk.collect.plist.template) has placeholders; the installer fills in this Mac's paths and writes `~/Library/LaunchAgents/com.dailydigestdesk.collect.plist`.
- **What a run does:** [scripts/run-digest.sh](scripts/run-digest.sh) runs `desk-collect` from the project folder, appends its output to `data/logs/desk-collect.log` (API keys redacted), and shows a pop-up, "Daily digest: 13 new items" with **Later** and **Open**. Open opens the new page (or the log, if no page was written); the pop-up closes by itself after 8 hours. A run with errors says so.
- **Open the newest page with one bookmark.** Each run also saves the page as `data/digests/latest.html`. Open it once and bookmark it: `open data/digests/latest.html`.
- **Why a pop-up, not a notification.** Current macOS silently drops notifications sent by scripts (`osascript display notification`), even with Script Editor allowed, and `terminal-notifier` no longer registers for notifications. Alerts (`display alert`) still show, and they can have an Open button.
- **Remove it:** `scripts/install-schedule.sh --uninstall`.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Tests run offline: EDGAR mapping uses fake filing objects, FRED and Finnhub mapping use fake API responses, and the collectors take an injectable fetcher. Signal rules and the digest page builder are pure functions; `collect_signals` and `write_digest` run against an in-memory store (and pytest's temporary folder for the page file).

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
  signals.py              # Signal rules: readings -> Events (big moves)
  red_flags.py            # Which events are red flags (five serious 8-K items)
  forms.py                # SEC form families (13D/13G spellings, 424B), shared
  ingestion/
    edgar.py              # edgartools wrapper (filings + XBRL + sections)
    edgar_events.py       # EDGAR filings -> Events (8-K item labels, forms)
    edgar_text.py         # A filing's text for summaries (main doc + EX-99, trimmed)
    fred.py               # FRED series -> Readings (daily) and Events (monthly releases)
    finnhub.py            # Finnhub quotes -> closing-price Readings; earnings -> Events
    concept_map.py        # Semantic metric -> XBRL tag fallback chains
  reasoning/
    grounding.py          # (stub) ground claims in source filings
    summarize.py          # A filing -> a checked plain-English Summary (Claude)
  output/
    digest.py             # Events -> the digest HTML page
    explainers.py         # The ⓘ buttons and explainer cards
    explainers.toml       # Explainer text per type of item (edit wording here)
    glossary.py           # Hover tips for glossary words
    glossary.toml         # Hover definitions for words on the page
    style.css             # The page's look (light and dark)
tests/                    # offline pytest suite
scripts/                  # run-digest.sh, the launchd template, install-schedule.sh
```
