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
- **Facts never change; opinions can.** Events are stored once and never edited. Anything the language model produces (summaries, thesis labels, scores) is stored separately, with which model and prompt version produced it. The live tables keep one per event (the newest replaces the old one); versions are compared on the eval, where every version runs on the same hand-checked events.
- **Informs, doesn't advise.** The digest explains what happened and how it relates to my thesis. It does not tell me to buy or sell.
- **Beginners can't spot errors.** That makes accuracy stricter, not looser: teaching content that doesn't change (explainers, glossary) is written once and reviewed; language-model teaching waits until summaries are checked against sources.

## 3. Roadmap

| Version | Adds | Depends on |
|---|---|---|
| **v1** | Gather events and display them on a web page, grouped by holding. Teaching: an **explainer per data type** (click "CPI" → what it is, why stocks care) and a **glossary on hover** (EPS, 8-K, yield…) | Event layer (EDGAR built) |
| **v2** | Language model writes a plain-English, **beginner-level summary** of each event, checked against the source | v1 |
| After v2 | **Themes and news**: each holding gets themes ("AI cloud", "bitcoin") and a short profile; news and free analysis come in as a new source; the language model scores how relevant each item is to each holding, with a one-line reason. Details in section 7 | v2 |
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
| **Judgment** | A language-model output about an event: summary, thesis label, (later) score. Points to its event; records model + prompt version + time | Language model | One per event; replaced when redone (the eval compares versions) |
| **Weekly review** | Per-holding review of the week's judgments against the thesis | Language model | One per week |
| **Explainer** | Plain-language page per data type (CPI, 8-K, Form 4…): what it is, why stocks care | Me (language model may help draft), reviewed | Rarely |
| **Glossary term** | Short hover definition (EPS, yield, 13G…) | Me (language model may help draft), reviewed | Rarely |

### Readings → events (signals)

Filings and monthly releases (CPI, jobs) are events by nature: they happen once. Daily values are **not** events; they're stored as readings, and rules turn them into events only when something changes:

| Signal | Example | Needs from readings |
|---|---|---|
| **Crossed above** a line | ⚠️ 10Y yield crossed above 5% | Yesterday's value (was it already above?) |
| **Crossed back below** | 10Y yield back below 5% | Yesterday's value |
| **Big move** | 10Y up 0.12 points vs the previous day | The previous reading |

One event per **change in situation**, not one per day — like a smoke alarm that beeps when smoke first appears, not every minute.

**Decided for v1:** only the big move, compared with the previous reading (1-day window). With a 1-day window each day's move is its own change, so the smoke-alarm repeat problem doesn't come up; it will for crossing rules. Thresholds: 10-year yield 0.10 points, Fed funds 0.20 points, VIX 3 points, each holding 5%. Details in FEATURES.md ("Signals"); crossing rules, multi-day drift and others are listed there under "Planned signals".

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

## 7. Event summaries (v2)

v1 titles say *that* something happened ("AAOI 8-K: Material agreement"), not *what*; explainers describe the type, the same for every 8-K. v2 adds a short summary of what each filing actually says.

**Only summarization.** No relevance scoring, news, thesis or opinions in v2; those come after (see below).

| Decision | Choice |
|---|---|
| Model | Claude Opus 5.5 (`claude-opus-5-5`), effort tuned on the eval. Cheaper models are tried only when the eval shows they do as well |
| What gets a summary | Free-text filings: 8-K / 6-K, 13D, S-3 / 424B; 10-Q / 10-K once their management discussion section can be found. Not 13G (a passive stake), big moves, FRED releases or earnings results (already clear sentences). Form 4 is structured data, so code describes it, not the model |
| What the model reads | The right part, not the whole filing: an 8-K's own text plus its press release (Exhibit 99.1) if it has one; a 10-Q / 10-K's management discussion section |
| What comes back | A fixed format: `summary` (2-3 plain-English sentences for a beginner) and `key_numbers`, each with the exact sentence from the filing it came from |
| Checking | Code checks every quote appears word for word in the filing and every number appears in its quote. A summary that fails is not shown |
| Storage | A `summaries` table: event id (the key: one summary per event, replaced if redone), summary, key numbers (JSON), model, prompt version, input and output tokens, time. Only summaries that passed the checks are saved. Facts and model output stay apart (section 2) |
| When | Inside `desk-collect`, after events are stored, before the page. Only filings from the last 7 days without a summary are sent, newest first, so a second run costs almost nothing |
| Failures | A summary that fails the checks gets one retry, told what was wrong. If that fails too it's dropped and its problems are printed with the run's errors. The page still comes out, without that summary; the next run retries, until the filing is 7 days old |
| Cost | Roughly $0.10-0.50 a day at today's holdings (5-15 new filings). A $2 cap per run, counting every token including failed calls, keeps a busy day from surprising me (the busiest day so far, 4 filings, is about $1 even if every one needs its retry). A capped filing isn't lost: it waits for the next run |
| Eval | About 20 real events, hand-checked: correct? anything important missing? anything made up? easy to understand? Rerun after every prompt or model change. The code checks only that each number is in the filing, so the eval reads every sentence: the ones with no number, and what the others say about their numbers. Cases so far: the HUT 13D/A of Nov 13, 2026 (`edgar:0001104659-26-127278`), where prompt v1 called Michael Ho a "CEO-level insider", a role the filing never states (v2 added the rule on people's roles) |

**After v2: themes, news and relevance.** The goal is everything important about my holdings in a day, including news that never names them ("Microsoft cuts data center leases" matters for NBIS and IREN).

- **Holding profiles and themes.** A short profile per holding (what it does, big customers, themes), drafted by the model from the 10-K and reviewed by me. The model reasons from the profile, not its memory.
- **Sources.** Finnhub company news, free feeds, and filings from companies that drive mine (the big AI spenders' capex). Paid content only as headline and link. Analysis is labeled as opinion.
- **Relevance.** Duplicate stories merged first, then the model gives each item a 0-3 score per holding with a one-line reason. The reason makes a bad link visible.
- **Eval.** About 50 real stories I label myself; misses count more than false alarms.
- **No fine-tuning.** Facts change daily, so they go in with each request; a general model with good context and an eval beats a fine-tuned one here, and upgrades are one line.

## 8. Weekly thesis review (v4)

Runs **weekly**. Goal: help me **maintain my thesis and manage my portfolio**. Tentative output per holding:

- **Thesis status:** intact / under pressure / broken
- **Evidence this week:** the events that strengthened or weakened it, each linked to its source
- **Questions to revisit:** what I should re-check or rewrite in my thesis

Exact format to be decided once v3 produces real labels.

## 9. Deferred on purpose

| Item | Why deferred |
|---|---|
| Quick-skim view | Needs a way to pick the top items; decide after seeing real data |
| Importance score / ranking | Same; thesis labels may replace a generic score |
| Connecting macro news to specific holdings (e.g. CPI → NVDA) | Planned after v2 (section 7): holding profiles and relevance scores |
| Red-flag rules (e.g. 8-K item 4.02 always flagged) | Open: v1 or later |
| Obsidian notes | Dropped — the web page replaces them |
| Multiple users | Later; keep holdings per person so it's possible |

## 10. Open questions

1. ~~**Web page:** local or hosted? Generated page or web app?~~ Decided — see section 6: generated page for v1, local app at v3, hosted for friends.
2. ~~**FRED and Finnhub:** which data, exactly, for v1?~~ Decided — see section 4. Daily values become events via signal rules — see section 5. Thresholds for v1 are decided there too.
3. ~~**Red flags:** in v1 or later?~~ Decided: v1 flags five rare, almost-always-bad 8-K items (1.03 bankruptcy, 2.06 impairment, 3.01 delisting notice, 4.01 auditor change, 4.02 non-reliance). They get a red "⚠️ Red flag" label, go first in their holding's section, and are counted in the page header and the pop-up. It means "read this today", not "sell". More kinds of red flag can come later.
4. **Thesis history:** when I rewrite a thesis, keep old versions?
5. ~~**Schedule:** how the two daily runs and the weekly review get triggered (manual first?).~~ Decided for the daily runs: launchd on my Mac, weekdays at 8:00am and 5:00pm New York, with a pop-up to open the page (README, Scheduling). The weekly review's trigger is still open (v4).
