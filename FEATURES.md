# Features and design decisions

A reference for what's been built, why, and which known XBRL / SEC-data gotchas have been addressed.

The ingestion layer is now a thin wrapper over [edgartools](https://github.com/dgunning/edgartools). edgartools handles the HTTP plumbing (User-Agent, rate limiting, CIK resolution, pagination); we own the semantic-metric layer, dedupe/as-of helpers, and the CLI.

---

## Implemented features

### Ingestion ([edgar.py](daily_digest_desk/ingestion/edgar.py))

| Function | Returns | Purpose |
|---|---|---|
| `fetch_filings(ticker, forms, limit)` | `EntityFilings` | List recent filings via `Company.get_filings` |
| `fetch_document(filing)` | `str` | Plain-text body of a filing (`filing.text()`) |
| `fetch_section(filing, item)` | `str` | One item section of a 10-K/10-Q (`filing.obj()["Item 1A"]`) |
| `fetch_company_facts(ticker)` | `EntityFacts` | edgartools facts object, cached per-ticker |
| `fetch_concept(ticker, tag, unit, taxonomy, as_of)` | `pd.DataFrame` | Time series for one XBRL concept |
| `fetch_metric(ticker, metric, as_of)` | `(tag, pd.DataFrame)` | Resolve a semantic metric via the concept map |
| `extract_metric(facts, metric, as_of)` | `(tag, pd.DataFrame)` | Same, against an in-memory facts object |
| `discover_tags(ticker, keyword)` | `pd.DataFrame` | List concepts the company reports |
| `latest_per_period(df)` | `pd.DataFrame` | Dedupe to one row per (start, end), latest filing wins |

Column-name constants (`COL_VALUE`, `COL_END`, `COL_START`, `COL_FILED`, `COL_FY`, `COL_FP`, `COL_FORM`, `COL_UNIT`, `COL_CONCEPT`) are exported so callers can reference DataFrame columns symbolically — single point of change if edgartools' schema shifts.

### Concept map ([concept_map.py](daily_digest_desk/ingestion/concept_map.py))

Curated dict of ~13 metric names → ordered XBRL tag candidates. Unchanged in the migration:

`revenue`, `gross_profit`, `operating_income`, `net_income`, `eps_basic`, `eps_diluted`, `assets`, `liabilities`, `equity`, `cash`, `operating_cash_flow`, `capex`, `shares_outstanding`.

`fetch_metric` walks candidates in order, returns the first one with data plus `tag_used` as audit trail.

### CLI ([main.py](daily_digest_desk/main.py))

Four mutually-exclusive modes plus shared flags:

| Mode | Flag | Behavior |
|---|---|---|
| Filings | (default) | List recent filings with URLs |
| Concept | `--concept TAG` | Pull one XBRL tag |
| Metric | `--metric NAME` | Pull a semantic metric via concept map |
| Discover | `--discover [KEYWORD]` | List concepts the company reports |

Shared: `--limit`, `--forms`, `--unit`, `--taxonomy`, `--form-filter`, `--dedupe`, `--as-of`.

### Event layer ([models.py](daily_digest_desk/models.py), [store.py](daily_digest_desk/store.py), [collect.py](daily_digest_desk/collect.py))

Foundation for the daily digest. Design decisions:

- **One `Event` model for every source.** Scoring, summarization and output only see Events, so a new source (FRED, Finnhub) is one mapper.
- **Deterministic ids → idempotent ingestion.** `edgar:<accession>`; `INSERT OR IGNORE`; `store.add()` returns only the new rows. Overlapping fetch windows are safe, so cursors can be coarse (dates, inclusive).
- **UTC-only timestamps.** `occurred_at` must be tz-aware and is normalized to UTC; stored as ISO strings so SQL string order equals time order.
- **Per-ticker cursors** (`edgar:<TICKER>`). New tickers get their own lookback; a failing ticker keeps its cursor and is retried next run.
- **Fetch vs. map split.** `filing_to_event` is pure and unit-tested with fakes; `collect_edgar` takes an injectable fetcher.
- **Errors grouped by source.** `main()` keeps one errors dict per source (`errors["edgar"]`, `errors["finnhub_earnings"]`, …), so NVDA failing in two sources keeps both messages instead of the second overwriting the first. Each prints as `# ERROR <source> <ticker or series>: <message>`, and the run exits 1 if anything failed, so a scheduler can tell.
- **One `today` per run.** `main()` reads the date once and passes it to every collector, so a run that crosses midnight doesn't mix two dates.

- **Readings** (`Reading` model, `readings` table). Daily values (yields, closing prices) are history, not news; signal rules will turn them into Events. Key is `(series, date)` — no built id, since sources don't provide one. The composite primary key doubles as the index for "series X since date Y" (`EXPLAIN QUERY PLAN` → `SEARCH ... USING INDEX`). `add_readings` is first-value-wins, so a later revision of the same day is ignored.

### FRED source ([fred.py](daily_digest_desk/ingestion/fred.py), `collect_fred` in [collect.py](daily_digest_desk/collect.py))

- **Two kinds of series.** Daily (`DFF`, `DGS10`, `VIXCLS`) → Readings. Monthly releases (`CPIAUCSL`, `PAYEMS`) → one Event per release with the change vs the prior month (% for CPI, thousands of jobs for payrolls). Event id `fred:<SERIES>:<period>`.
- **Missing days.** FRED marks them `"."`; the mapper skips them. Anything else non-numeric fails loudly in `Reading`.
- **Two requests per release.** A normal request returns today's (revised) values, but stamps every row's `realtime_start` with *today*, so it can't tell when a month came out. A second request with `output_type=4` ("initial release only") over the whole real-time history (`realtime_start=1776-07-04`) gives each month's first release day. Values come from the first request, the date from the second. (Found on the first live run: an earlier version read the normal request's `realtime_start` as the release date, and its fake test data had the same wrong assumption built in.)
- **Only the newest month is news.** One event per release, for the newest month, if it was first released on or after the cursor.
- **Release time.** FRED gives the release day, not the time; `occurred_at` is the start of that day in New York.
- **Cursors.** Daily series: the newest date FRED returned, not today, because FRED posts daily values a day or so late. Releases: today. A failed series keeps its cursor.
- **No new dependency.** Plain `urllib`; the API key is in the URL, and urllib's errors don't include the URL, so the key isn't printed.
- **Skipped without a key.** `desk-collect` runs EDGAR alone if `FRED_API_KEY` is unset.

### Finnhub source ([finnhub.py](daily_digest_desk/ingestion/finnhub.py), `collect_finnhub` and `collect_finnhub_earnings` in [collect.py](daily_digest_desk/collect.py))

Quotes:

- **Closing prices as Readings.** One Reading per holding per day, `finnhub:<TICKER>`, value = the quote's `c`. A price is history, not news; signal rules will turn big moves into Events.
- **Market-hours guard.** `/quote` returns the latest price, which is still moving while the market is open. So a run on a weekday between 9:30 and 16:00 New York stores nothing. Any other time, `c` is the latest close: before the open, it's the previous trading day's; on weekends and holidays, the last trading day's.
- **Date from `t`, not from the run.** The reading's date is the New York day of the quote's timestamp `t`. A Thursday 8 am run stores Wednesday's close under Wednesday.
- **`now` in any time zone.** `quote_to_reading` converts `now` to New York before the guard. `main()` passes `datetime.now(timezone.utc)`, and a test checks that a UTC time isn't read as a New York one.
- **Unknown ticker raises.** For a ticker it doesn't know, Finnhub answers with all zeros (`t = 0`) instead of an error. `fetch_quote` raises `ValueError`, so a typo in holdings shows up as `# ERROR finnhub_quotes <TICKER>` instead of a reading dated 1969-12-31.
- **No cursor.** `/quote` has no "since" parameter, and a repeat run's reading has the same `(series, date)` key, so the store ignores it.
- **One ticker's failure is isolated.** Fetch and mapping both run inside the per-ticker `try`; the error is logged and recorded, and the next ticker runs.
- **No history on the free tier.** Candles are premium, so a missed day can't be backfilled. Two runs a day (before the open and after the close) give each close two chances.
- **Skipped without a key.** `desk-collect` skips Finnhub (quotes and earnings) if `FINNHUB_API_KEY` is unset.

Earnings (`/calendar/earnings`):

- **Two events per report.** A heads-up (`earnings_headsup`) in the 7 days before, with the analyst EPS estimate, and a result (`earnings_result`) once Finnhub fills in the actual, titled beat / missed / matched. Ids are `finnhub:earnings:<TICKER>:<fiscal year>Q<quarter>:headsup|result`, built from the fiscal quarter, not the date, so a report that moves keeps its id.
- **Fiscal, not calendar, quarters.** NVDA's "2027 Q3" is reported in Nov 2026; the id and fields use Finnhub's fiscal `year` and `quarter`.
- **Four cases per row.** Actual filled in → result. Report day passed but no actual yet → nothing; a later run retries. More than 7 days away → nothing yet. Otherwise (up to and including report day) → heads-up.
- **Compared in cents.** Actual and estimate are both rounded to 2 decimals before comparing, so the verdict matches the numbers in the title (2.53 vs 2.5263 is "matched").
- **`0.0` is a real EPS.** Checks use `is None`, not truthiness, so a break-even quarter isn't treated as missing.
- **`occurred_at`.** A result is dated at the report time: before the open = 8:00, after the close = 16:00 New York, during market hours or not announced = midnight. A heads-up is dated the day it's created (the run's `today`), since that's when it becomes news.
- **No cursor.** Each run asks for a window from 7 days back (to catch actuals Finnhub fills in late) to 7 days ahead (heads-ups); the store dedupes on event id. An unknown ticker returns an empty list, not an error.
- **All or nothing per ticker.** A ticker's rows are all mapped before any are stored, so one broken row stores nothing for that ticker, and the whole ticker is retried next run.
- **Free-tier limit.** About a month of history, so missed reports can't be backfilled far. The url is the company's EDGAR filing list; Finnhub has no page per report.

Not yet: Form 4 parsing (transaction code P vs S, size), signal rules, scoring.

### Configuration

- `EDGAR_USER_AGENT` env var → passed to `edgar.set_identity()` on first use.
- Loaded from `.env` automatically in CLI; library users call `load_dotenv()` themselves.

---

## Caveats addressed

### 1. SEC requires a User-Agent — *handled by edgartools*

`set_identity()` is called once per process from `EDGAR_USER_AGENT`. We raise a clear error if unset rather than silently failing.

### 2. Rate limit (≤10 req/s) — *handled by edgartools*

edgartools' rate limiting is configurable internally. We no longer maintain a hand-rolled token bucket.

### 3. CIK formats differ between endpoints — *handled by edgartools*

`Company(ticker)` resolves to whatever internal representation each endpoint needs.

### 4. XBRL tag names aren't standardized — *resolved (concept_map + audit trail)*

The us-gaap taxonomy is fixed, but **which** tag a company uses for a given economic concept varies (ASC 606 transition, segment granularity, extension tags).

Strategy:
- **Concept map** — ordered candidate chains for the 13 high-level metrics.
- **Audit trail** — `fetch_metric` returns `(tag_used, df)` so callers know which tag matched.
- **`discover_tags()`** — escape hatch for tags outside the curated list, including company-specific extension tags.

### 5. XBRL data has duplicates — *resolved (latest_per_period + --dedupe)*

Three reasons (GAAP comparative columns, Q-vs-YTD, restatements) detailed in the README. Resolution unchanged from pre-migration:

- `period_start` is preserved in the DataFrame (distinguishes 3-month from 9-month spans).
- `latest_per_period(df)` groups by `(period_start, period_end)` and keeps the latest `filing_date`.
- CLI `--dedupe` flag, opt-in. Default keeps all rows so restatements remain visible.

### 6. Restatements / lookahead bias — *resolved (--as-of + --dedupe combo)*

`as_of` filters facts where `filing_date <= as_of` *before* dedupe. With `--dedupe` it gives "what was known at that date." Diffing against today's view surfaces restatements programmatically.

### 7. Recent-window pagination — *delegated to edgartools*

edgartools handles paginated submissions internally for `Company.get_filings`. The pre-migration limitation (only seeing the recent window) no longer applies the same way; deeper history is available subject to edgartools' own behavior.

### 8. HTTP redundancy — *resolved (lru_cache layers)*

- `Company(ticker)` cached per-ticker (avoid re-instantiating).
- `fetch_company_facts(ticker)` cached per-ticker. Multi-metric workflows pay one effective fetch.

In-memory only, no on-disk persistence, will serve stale data if a new filing drops mid-session — documented for awareness.

### 9. Filing section parsing — *now resolved by edgartools*

Pre-migration this was an open caveat (would have required hand-rolling a section parser for 10-K Items). edgartools' `filing.obj()["Item 1A"]` style indexing gives clean section text. Exposed as `fetch_section(filing, item)`. This unlocks the LLM-extraction path in `reasoning/grounding.py` without us building the parser.

---

## Caveats not yet addressed

### Narrative grounding pipeline

[reasoning/grounding.py](daily_digest_desk/reasoning/grounding.py) is still a stub. Plan: pipe `fetch_section(filing, item)` output to Claude for structured claim extraction with citations back to (filing accession, section, span). Use Anthropic's prompt caching: section text as the cacheable system prompt, the extraction query as the variable part.

### Obsidian output

[output/obsidian.py](daily_digest_desk/output/obsidian.py) is still a stub. Planned: write filing summaries / extracted claims as Markdown notes with backlinks for cross-filing navigation.

### Column-schema fragility

We rely on edgartools' DataFrame schema (column names like `numeric_value`, `period_end`, `filing_date`). Centralized via the `COL_*` constants in `edgar.py`, but the assumption is unverified until first run. Verify after first `pip install -e .` and a test query.

---

## Project layout

```
daily_digest_desk/
  main.py                 # CLI entry point
  ingestion/
    edgar.py              # edgartools wrapper (filings + XBRL + sections)
    concept_map.py        # Semantic metric -> XBRL tag fallback chains
  reasoning/
    grounding.py          # (stub) ground claims in source filings
  output/
    obsidian.py           # (stub) write Obsidian notes
```
