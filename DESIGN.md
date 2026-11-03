# DailyDigestDesk — Design

A daily investing digest that gathers news and data about my holdings, explains it, and checks it against my investment thesis for each stock.

Status: design agreed 2026-11-02. Decisions below; open questions at the end.

---

## 1. Requirements

| | Requirement |
|---|---|
| **Users** | Investing beginners: me first, maybe friends later, so holdings are per person, never hard-coded. |
| **Teaching** | The digest should **teach, not just report**: every term and data type has a plain-language explanation and a "why this matters". |
| **Timing** | Two editions a day: **before market open** and **after close**. Each covers what's new since the last edition. |
| **Output** | A **web page**. |
| **Depth** | Built for a deep read. A quick skim is wanted too, but later. |
| **Coverage** | My holdings, macro news, and the broader market — but **everything refers back to my holdings**. |
| **Thesis** | I write an investment thesis for each holding. The system checks events against it and reviews it weekly. |

## 2. Principles

- **Facts come from sources, not the language model.** Every number comes from structured data (EDGAR, FRED, Finnhub). The language model only writes prose about facts it was given, and cites them.
- **The database is the single source of truth.** The web page is a view of it, rebuilt from it.
- **Facts never change; opinions can.** Events are stored once and never edited. Anything the language model produces (summaries, thesis labels, scores) is stored separately, with which model and prompt version produced it, so versions can be compared.
- **Informs, doesn't advise.** The digest explains what happened and how it relates to my thesis. It does not tell me to buy or sell.
- **Beginners can't spot errors.** That makes accuracy stricter, not looser: teaching content that doesn't change (explainers, glossary) is written once and reviewed; language-model teaching waits until summaries are checked against sources.

## 3. Roadmap

| Version | Adds | Depends on |
|---|---|---|
| **v1** | Gather events and display them on a web page, grouped by holding. Teaching: an **explainer per data type** (click "CPI" → what it is, why stocks care) and a **glossary on hover** (EPS, 8-K, yield…) | Event layer (EDGAR built) |
| **v2** | Language model writes a plain-English, **beginner-level summary** of each event, checked against the source | v1 |
| **v3** | I write a **thesis** per holding. Language model labels each event: *strengthens / weakens / breaks / neutral*, with a reason | v2 + stored theses |
| **v4** | **Weekly thesis review** per holding, plus a **weekly lesson** built from that week's real events | v3 |
| Later | Quick-skim view; importance scoring / ranking | Real data from v1–v4 |

## 4. Data sources

| Source | What kind of info | How often it updates | Example item |
|---|---|---|---|
| **EDGAR** | Company filings: 8-K events, insider trades (Form 4), big-holder stakes (13D/G), quarterly/annual reports | Whenever a company files, any hour | *AAPL 8-K: Earnings results, Oct 30 4:30pm ET, link* |
| **FRED** | Macro series (starter set below) | Daily or monthly, depending on series | *CPI for September released: +0.3% vs prior month* |
| **Finnhub** | Daily price change, earnings calendar with estimates, earnings surprises (actual vs. estimate) | Prices: every trading day. Earnings: around report dates | *NVDA beat EPS estimate by 8%* |

**FRED starter set (proposed):**

| Series | What it is | Updates | Why it matters (plain language) |
|---|---|---|---|
| `CPIAUCSL` | Inflation | Monthly | Fast-rising prices keep the Fed's rates high, which tends to hurt stock prices |
| `PAYEMS` | Jobs added | Monthly | Shows whether the economy is growing or slowing; big mover on release day |
| `DFF` | Fed funds rate | Daily | The Fed's base rate; almost every other rate follows it |
| `DGS10` | 10-year Treasury yield | Daily | When it rises, future profits are worth less today; hits growth stocks hardest |
| `VIXCLS` | VIX "fear index" | Daily | Higher = more nervous market, bigger daily swings |

**Finnhub (chosen):** quote (daily price change), earnings calendar (dates + consensus estimates), earnings surprises. Check which endpoints the free tier includes.

Both need a free API key (signed up by me), stored in `.env`.

## 5. Data model (concepts, not tables yet)

| Thing | What it is | Written by | Changes? |
|---|---|---|---|
| **Reading** | One value of something that changes constantly (a daily closing price, the 10Y yield, VIX). Kept quietly as history; never shown directly | Fetched from sources | Never |
| **Event** | One thing that became public (a filing, a data release, an earnings result), or a signal made **from readings** by a rule | Fetched from sources, or derived by rules | Never |
| **Holding** | A stock I own, with its weight | Me | Sometimes |
| **Thesis** | Why I own a holding | Me | Over time |
| **Judgment** | A language-model output about an event: summary, thesis label, (later) score. Points to its event; records model + prompt version + time | Language model | New versions added, old ones kept |
| **Weekly review** | Per-holding review of the week's judgments against the thesis | Language model | One per week |
| **Explainer** | Plain-language page per data type (CPI, 8-K, Form 4…): what it is, why stocks care | Me (language model may help draft), reviewed | Rarely |
| **Glossary term** | Short hover definition (EPS, yield, 13G…) | Me (language model may help draft), reviewed | Rarely |

### Readings → events (signals)

Filings and monthly releases (CPI, jobs) are events by nature: they happen once. Daily values are **not** events; they're stored as readings, and rules turn them into events only when something changes:

| Signal | Example | Needs from readings |
|---|---|---|
| **Crossed above** a line | ⚠️ 10Y yield crossed above 5% | Yesterday's value (was it already above?) |
| **Crossed back below** | 10Y yield back below 5% | Yesterday's value |
| **Big move** | 10Y up 0.4% vs last week | Last week's value |

One event per **change in situation**, not one per day — like a smoke alarm that beeps when smoke first appears, not every minute. Thresholds (which lines, how big a "big move") to be decided per series.

## 6. Web page

### How it's built and served — grows in stages

| Stage | Option | Why then |
|---|---|---|
| **v1** | **Generated page**: each run writes one `digest.html`; I open it in my browser | v1 only displays. No server needed; collapsing and hover glossary still work |
| **v3** | **Local web app** (e.g. `localhost:8000`) reading the database on each visit | I need a form to write my thesis |
| **Friends** | **Hosted web app** with logins | Friends see their own holdings. Brings logins, privacy, hosting cost |

Start with the simplest option today's requirements need; move up when a requirement forces it. To make moving up cheap: keep "read data → build page" separate from "write file", so the same page-building code can later be served by the app.

### Layout (v1)

Grouped **by holding**, with one shared section on top. Same-kind items collapse into one line with a count.

```
▸ Macro & Market (affects everyone)
    CPI release · 10Y yield · S&P move
▸ AAPL
    8-K: Earnings results
    Insider transactions (4) ▸          ← collapsed group
▸ CLSK
    Big-holder stake amendments (3) ▸   ← near-duplicates folded
```

Later, sorted within each holding — likely by thesis label first ("breaks" at the top).

## 7. Weekly thesis review (v4)

Runs **weekly**. Goal: help me **maintain my thesis and manage my portfolio**. Tentative output per holding:

- **Thesis status:** intact / under pressure / broken
- **Evidence this week:** the events that strengthened or weakened it, each linked to its source
- **Questions to revisit:** what I should re-check or rewrite in my thesis

Exact format to be decided once v3 produces real labels.

## 8. Deferred on purpose

| Item | Why deferred |
|---|---|
| Quick-skim view | Needs a way to pick the top items; decide after seeing real data |
| Importance score / ranking | Same; thesis labels may replace a generic score |
| Connecting macro news to specific holdings (e.g. CPI → NVDA) | Needs the language model and per-holding context |
| Red-flag rules (e.g. 8-K item 4.02 always flagged) | Open: v1 or later |
| Obsidian notes | Dropped — the web page replaces them |
| Multiple users | Later; keep holdings per person so it's possible |

## 9. Open questions

1. ~~**Web page:** local or hosted? Generated page or web app?~~ Decided — see section 6: generated page for v1, local app at v3, hosted for friends.
2. ~~**FRED and Finnhub:** which data, exactly, for v1?~~ Decided — see section 4. Daily values become events via signal rules — see section 5. Still open: the actual thresholds per series.
3. **Red flags:** in v1 or later?
4. **Thesis history:** when I rewrite a thesis, keep old versions?
5. **Schedule:** how the two daily runs and the weekly review get triggered (manual first?).
