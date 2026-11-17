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

- **Readings** (`Reading` model, `readings` table). Daily values (yields, closing prices) are history, not news; signal rules turn them into Events. Key is `(series, date)` — no built id, since sources don't provide one. The composite primary key doubles as the index for "series X since date Y" (`EXPLAIN QUERY PLAN` → `SEARCH ... USING INDEX`). `add_readings` is first-value-wins, so a later revision of the same day is ignored.

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

- **Closing prices as Readings.** One Reading per holding per day, `finnhub:<TICKER>`, value = the quote's `c`. A price is history, not news; signal rules turn big moves into Events.
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

### Signals ([signals.py](daily_digest_desk/signals.py), `collect_signals` in [collect.py](daily_digest_desk/collect.py))

v1 has one rule, the **big move**: a reading compared with the one before it in the same series.

| Series | Threshold | Why |
|---|---|---|
| `fred:DGS10` 10-year yield | 0.10 points | a normal day is ~0.04 |
| `fred:DFF` Fed funds rate | 0.20 points | it only moves when the Fed does |
| `fred:VIXCLS` VIX | 3 points | a normal day is ~0.6 |
| `finnhub:<TICKER>` each holding | 5% | same for every holding in v1 |

- **Rules are data.** A `BigMove` holds the series, the title name, the threshold, and whether the move is in points (`diff`) or percent (`pct`). Macro rules are a fixed list, `MACRO_RULES`; stock rules are built from the holdings by `stock_rules(tickers)`. Thresholds are roughly 3–5× a normal day's move on Oct–Nov 2026 data, to be tuned after a few weeks of digests.
- **Pure rule, separate collector.** `big_move_event(rule, previous, latest)` has no store and no network, so its tests are just two readings. `collect_signals` does the store work: it finds each reading's previous and stores the event.
- **Rounded before the check.** 5.00 − 4.90 is 0.09999999999999964 in floats; rounding to 2 decimals first means it fires at 0.10, matching the title. Two tests (one diff, one pct) fail if the rounding is removed.
- **Up and down both count.** The threshold is checked against the size of the move; the title says `up` or `down`, and `fields["move"]` keeps the sign.
- **No percent change from 0.** A `pct` rule with a previous value of 0 returns nothing. `diff` rules have no division, so Fed funds going from 0 to 0.25 still fires.
- **Titles.** "10-year Treasury yield up 0.12 points to 5.10%", "VIX down 5.72 points to 19.92", "NVDA down 6.21% to $187.40". The url is the FRED series page for macro rules and the Yahoo quote page for stocks.
- **Missed runs are named.** A stock's close is only stored by a run, so after days with no run, the previous close can be a week old. If the previous reading is more than 4 days back (longer than Friday to Tuesday over a holiday), the title says so: "NVDA down 12.75% since Nov 6 to $162.85".
- **Id and date.** `signal:big_move:<series>:<date>`, dated 16:00 New York on the reading's day, so a rerun makes the same id and the store ignores it.
- **Runs last, on this run's new readings.** `main()` calls `collect_signals` after the FRED and Finnhub collectors have stored their readings, so a backlog (Thursday and Friday arriving together on Monday) compares Friday with Thursday.
- **Previous from the store.** The newest stored reading before this one, within 10 days. None found (a holding's first day) → no event.
- **Only the past week is checked.** A first run backfills weeks of FRED history, and a big move from three weeks ago isn't news. Readings older than 7 days are stored but not checked, so they can still be the previous reading for a newer one. The cost: after more than a week with no runs, the moves in that gap are never reported.
- **No rule is not an error.** A series kept only as history (a benchmark, or a series whose rule isn't written yet) is skipped.
- **One reading's failure is isolated.** It's recorded as `# ERROR signals <series> <date>: <message>`, and the other readings are still checked.

Not yet: Form 4 parsing (transaction code P vs S, size), scoring, more signal rules (see "Planned signals" below).

### Digest page ([output/digest.py](daily_digest_desk/output/digest.py), `write_digest` in [collect.py](daily_digest_desk/collect.py), `events_inserted_since` in [store.py](daily_digest_desk/store.py))

Each `desk-collect` run ends by writing one HTML page: `data/digests/<New York date-time>.html`, e.g. `2026-11-17-080012.html`. Open it in a browser; no server needed.

- **New since the last page, by when we stored it.** The page shows events with `inserted_at` after the `digest:last` cursor, not `occurred_at`: an earnings result Finnhub fills in two days late keeps its real date but is still news to us. Strictly after (`>`), so an event stored exactly at the cursor was on the last page. The first page, with no cursor, covers the past week.
- **The cursor moves only after the file is written.** It's set to the time taken just before the query, so an event stored in between lands on two pages at worst, never on none. A failed page is `# ERROR digest page: ...`, and the next run's page shows the same events.
- **One file per run.** A missed 8am page isn't overwritten by the 5pm one. The name has seconds, so a rerun in the same minute doesn't overwrite either.
- **`latest.html`** is a copy of the newest page under a fixed name, so one browser bookmark always opens it.
- **Layout.** "Macro & Market" (events with no ticker) first, then each holding in `holdings.toml` order. A holding with nothing new is left out. A ticker no longer in holdings still shows, after the holdings, A to Z. Newest first within a section, each line a title link plus its New York day.
- **Only noisy kinds fold.** Two or more insider transactions (Form 4) or big-holder stake filings (all 13D/13G variants) fold into one click-to-open line with a count, using HTML's `<details>`. Everything else, 8-Ks included, gets its own line, so an earnings 8-K is never hidden.
- **Safe with outside text.** Titles, urls and headings go through `html.escape`, so a `<script>` in a title is shown, not run. Only `http(s)` urls become links, because escaping can't make a `javascript:` url safe to click.
- **Pure builder, separate writer.** `build_digest(events, now, tickers)` is text in, text out, with no store, files or clock, so its tests are plain strings. `write_digest` does the cursor and the file and takes the builder as a parameter.
- **HTML and CSS only.** Python writes the page; no JavaScript, no dependencies. Light and dark mode follow the system.

Not yet: explainers per data type, glossary on hover, upcoming earnings list, company names next to tickers. Scheduling is in the README (macOS launchd).

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

### Column-schema fragility

We rely on edgartools' DataFrame schema (column names like `numeric_value`, `period_end`, `filing_date`). Centralized via the `COL_*` constants in `edgar.py`, but the assumption is unverified until first run. Verify after first `pip install -e .` and a test query.

---

## Planned signals (after the big move)

The big move (one series, today vs the previous reading) is the first rule. These come after it. The numbers are starting points, to be checked against real data when each one is built.

- **More maturities: DGS2, the 2-year Treasury yield.** It reacts most to what the market expects the Fed to do next; the 10-year reacts more to long-run growth and inflation. It needs to go into both `fred.DAILY_SERIES` and `MACRO_RULES`. Other maturities (1-year, 5-year) mostly move with these two, so they'd fire on the same days.
- **Crossing rules, a second rule type.** The news is a level being crossed, not a big one-day change, so `BigMove` can't express it. Avoiding a repeat event every day while the level stays crossed is an open design question.
  - **Yield curve, `T10Y2Y`** (the 10-year yield minus the 2-year, on FRED). It's normally positive: lending for longer pays more. Below zero is an "inversion", a well-known recession warning. It was inverted from mid-2022 to late 2024. Crossing in either direction is news. It's a warning, not a timing tool.
  - **VIX above 30.** VIX's long-run average is about 20, so crossing 20 happens too often to be news. Above 30 is market stress.
- **Multi-day drift.** For example, down 10% or more over a week with no single 5% day. The window is per rule, which the big move put off for v1.
- **Big move right after earnings.** Link a stock's big move to that ticker's earnings result from the day before or the same day: "NVDA down 8% the day after earnings". This gives context, not just a number.
- **Compared with the market.** NVDA −5% means less on a day the whole market fell 4%. Store a benchmark (SPY, an S&P 500 fund) as one more Finnhub series, even though it isn't a holding, and show it in the title: "NVDA down 6% (S&P 500 down 1%)".
- **Intraday alerts (much later).** This is the Robinhood style: an alert at 5%, 10%, 15% and 20% during the day, each level once. It needs polling during market hours, and those prices can't be stored as Readings: `add_readings` is first-value-wins per day, so an 11:00 price would block that day's real close. It's a different product from a twice-a-day digest.

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
    digest.py             # Events -> the digest HTML page
```
