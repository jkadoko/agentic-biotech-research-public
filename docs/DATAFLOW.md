# Biotech-Analyzer v3.5 — Dataflow Document

**Version:** 3.5z
**Last Updated:** 2026-04-12
**Aligned with:** PRD v3.5z, ARCHITECTURE.md v3.5r, SCHEMA.md v3.5s

## Overview

This document maps how data moves through the Biotech-Analyzer v3.5 system — from raw external sources through ingestion, agent processing, and knowledge base sync, to the Streamlit UI. The system uses a single SQLite WAL database (`biotech_tracker.db`) as the transactional backbone, with ChromaDB as the local vector store for semantic search. All external API calls target their respective v2/current endpoints. No PostgreSQL is required.

---

## Phase 0: Ticker Onboarding (One-Time, On-Demand)

The onboarding pipeline runs **once per ticker** and re-runs whenever a new 10-K is detected. It is independent of the daily Crew 1–3 cycle and is the authoritative source of trial linkage for known tickers.

```
New Ticker (Streamlit UI entry or Scout Task D)
    │
    ▼
[Step 1] Validate ticker via yfinance
    │
    ▼
[Step 2] Fetch latest 10-K from SEC EDGAR
         → raw text + URL → sec_filings table
    │
    ▼
[Step 3] Embed 10-K into ChromaDB
         512-token overlapping chunks | Ollama embeddings
         Partitioned by therapeutic area
    │
    ▼
[Step 4] LLM structured extraction (llama3.1:8b)
         Extracts: drug names, NCT IDs, pipeline phases, officers/board,
         revenue stage, patent cliff dates, top risks, cash/burn/runway
    │
    ▼
[Step 5] Upsert into SQLite
         companies (onboarding_status = COMPLETE)
         interventions (drug names per ticker)
         company_onboarding_log (audit trail)
    │
    ▼
[Step 6] Four-pass clinical trial linkage (CT.gov API v2)
         Pass 1: NCT IDs cited verbatim in 10-K → direct lookup (10K_CITED)
         Pass 2: Drug name search → /api/v2/studies?query.term={drug} (DRUG_NAME_MATCH)
         Pass 3: Company name → sponsor + collaborator fields (COMPANY_NAME_MATCH)
         Pass 4: Detective entity_aliases → resolved ticker → all trials for that sponsor (ENTITY_RESOLVED)
                 (catches subsidiary/acquisition cases; lowest priority but highest recall)
         → trial_pipeline table
    │
    ▼
[Step 7] Drug database lookups
         FDA Orphan Drug DB → orphan table
         FDA Orange Book (small molecules) → interventions.orange_book_appl_no, patent_expiry
         FDA Purple Book (biologics) → interventions.purple_book_bla_no, patent_expiry
         Note: investigational drugs return NOT_FOUND — logged, not blocking
    │
    ▼
[COMPLETE] Ticker ready for Crew 1–3 daily cycle
```

**Trigger conditions:**
- User adds ticker via Streamlit → immediate
- Scout Task A (IPO detection) → Scout Task D triggers post-triage
- Daily scheduler 07:00 — runs for any ticker with `onboarding_status = PENDING` or `STALE`
- `fetch_sec_filings.py` detects new 10-K → sets `onboarding_status = STALE`

---

## Phase 1: Nightly Data Ingestion (APScheduler — Batch)

Python fetcher scripts run nightly, populating SQLite and ChromaDB before agents execute.

### 1A. Financial & Market Data

| Source | Data Types | Destination | Cadence |
|---|---|---|---|
| **E*TRADE API** | Live quotes, options chains (IV, OI, bid/ask) | `options_chains` table | Daily pre-market |
| **yfinance** | Historical prices, EV, Market Cap, EPS | `companies`, `historical_prices` tables | Daily |

### 1B. Clinical Trials

Two parallel paths — real-time lookup vs. bulk analytical corpus:

| Source | Data Types | Destination | Cadence |
|---|---|---|---|
| **ClinicalTrials.gov API v2** | Single-trial status, NCT detail, sponsors, outcomes | `studies`, `sponsors`, `conditions`, `trial_pipeline` | On-demand (Oracle, Detective, onboarding pipeline) |
| **AACT CSV Upsert** (6 tables) | Bulk: `studies`, `sponsors`, `conditions`, `interventions`, `design_outcomes`, `collaborators` — pipe-delimited .txt files | Upserted directly into SQLite; powers Peer Reviewer endpoint taxonomy, Profiler competitive counts | Nightly download via HTTP (no PostgreSQL required) |

**AACT routing:** Oracle and Detective use API v2 for real-time lookups. Profiler and Peer Reviewer use AACT tables via SQLite for bulk analytics.

### 1C. SEC Filings

- `fetch_sec_filings.py` polls RSS feed for new 10-K, 10-Q, 8-K, Form 4, 13G/13D filings
- **Dual-write:** metadata → `sec_filings` table (SQLite) AND full text embedded into ChromaDB (partitioned by therapeutic area via `local_rag_tool.py` write mode)
- If a new 10-K is detected → `onboarding_status = STALE` for that ticker → re-onboarding triggered at 07:00

### 1D. FDA Data

- **Orphan Drug DB:** designation dates, exclusivity → `orphan` table (weekly)
- **PDUFA Calendar:** regulatory decision deadlines → parsed by Oracle agent (weekly)
- **Orange Book / Purple Book:** queried per drug during onboarding Step 7

### 1E. Financial News (RSS Feeds)

- **Sources:** BioPharma Dive, Fierce Pharma, Endpoints News, STAT News
- **Fetcher:** `fetch_news.py` — `feedparser` parses feeds every 4h; no API key required
- **Auto-categorization:** keyword regex → category field: `m&a | conference | analyst | fda | partnership`
- **Ticker association:** company name matching against `companies` table post-fetch
- **Destination:** `news_articles` table
- **Consuming agents:** Oracle (upcoming catalysts), Scout (market signals), Partnership (deal detection), Smart Money (institutional signals)

---

## Phase 2: Daily Agent Execution (CrewAI — 3 Crews)

**APScheduler SLA (REQ-063):** Full pipeline completes by 11:00 AM local time.

```
07:00 — Onboarding: process PENDING/STALE tickers
08:00 — Crew 1 (Data Collection)  — Detective · Scout · Oracle
09:00 — Crew 2 (Analysis)         — Profiler · Peer Reviewer · Insider · Partnership · Smart Money
10:30 — Crew 3 (Strategy)         — Volatility · Strategist
```

Crews are staggered to prevent concurrent GPU-heavy tasks on the 2x Tesla P4s (8GB VRAM each).

### Data Retrieval per Crew

Each agent accesses data through two paths:

1. **SQLite (structured):** `DatabaseQueryTool` → financial metrics, trial pipeline, insider trades, options chains, news articles
2. **ChromaDB (semantic):** `LocalRAGTool` read mode → "Summarize MRNA's risk factors", "Find similar Phase 3 CAR-T failures in DLBCL"

### Crew 1 — Data Collection (Parallel)

| Agent | Primary Inputs | Primary Outputs |
|---|---|---|
| Detective (001) | `entity_aliases`, AACT sponsors, CT.gov API v2 | `entity_aliases`, `trial_pipeline` ticker updates |
| Scout (002) | SEC EDGAR S-1 (direct HTTP), ChromaDB `sec_filings`, CT.gov API v2 (Task C), `disease_context`, `news_articles` | `companies` (new tickers), `disease_context`, `watchlist_flags` patch (MA_RUMOR_FLAG), triggers onboarding |
| Oracle (008) | CT.gov API v2, FDA PDUFA calendar, `news_articles` | `catalysts` (PDUFA, conference, Phase readout events) |

### Crew 2 — Analysis (Parallel, runs after Crew 1)

| Agent | Primary Inputs | Primary Outputs |
|---|---|---|
| Profiler (003) | `companies`, `disease_context`, AACT SQLite, CT.gov v2 | `agent_profiler_findings` |
| Peer Reviewer (004) | AACT `design_outcomes`, ChromaDB trial docs, CT.gov v2 | `agent_scientific_audits` |
| Insider (005) | `sec_filings` (Form 4), `companies` | `agent_insider_findings` |
| Partnership (010) | `sec_filings` (8-K), ChromaDB, `news_articles` | `partnerships` |
| Smart Money (011) | `sec_filings` (13G/13D), `news_articles` | `agent_smart_money_findings` |

### Crew 3 — Strategy (Sequential, runs after Crew 2)

| Agent | Primary Inputs | Primary Outputs |
|---|---|---|
| Volatility (009) | `options_chains`, `catalysts`, `companies` | `agent_volatility_findings` (CSP strike, IV, rejection reason) |
| Strategist (006) | All Crew 2 outputs + Volatility findings, ChromaDB `agent_memos` (longitudinal context via `LocalRAGTool`), `options_chains` | `agent_investment_memos` (BAS score, Kelly sizing, recommendation) — local inference only (deepseek-r1:7b-q4_K_M via `OllamaLLMTool`; cloud models forbidden) |

All agent outputs are also exported to `output/{agent_name}/{ticker}_{YYYYMMDD}.json` (REQ-072).

---

## Phase 3: Knowledge Base Synchronization (Weekly)

A feedback loop feeds agent outputs back into ChromaDB to create longitudinal memory.

```
[Weekly cron: scripts/sync_local_rag.py — Sunday 02:00 UTC]
    │
    ▼
Query SQLite: new agent_investment_memos + agent_scientific_audits
             WHERE uploaded_to_rag = 0
    │
    ▼
Embed as markdown → ChromaDB `agent_memos` collection
    │
    ▼
Set uploaded_to_rag = 1 on synced records
    │
    ▼
Result: Strategist can now query historical context:
        "What did I recommend for BNTX last quarter, and was I right?"
```

**Note:** Non-10K SEC filings (8-Ks, 10-Qs) are embedded via `fetch_sec_filings.py` into the `sec_filings` ChromaDB collection on the nightly 07:00 run — not via `sync_local_rag.py`. The weekly sync handles agent output tables only.

---

## Phase 4: User Interface (Streamlit — On-Demand + Read)

Streamlit is primarily a read-only dashboard, but includes on-demand pipeline triggers for operational use.

- **Portfolio Dashboard:** Performance, Sharpe Ratio, Drawdown, active CSP positions
- **Holdings Table:** Active signals per ticker (BAS score, recommendation, upcoming catalysts)
- **Agent Reports:** Click ticker → latest Strategist investment memo
- **News Feed:** Latest `news_articles` with category filter
- **Direct RAG Query:** Free-text search directly against ChromaDB (bypasses agents)
- **Quick Onboard:** User-triggered `onboard_company.py` via Streamlit UI for manual ticker addition — this DOES run pipeline logic (LLM extraction, ChromaDB embedding). The "no agent logic in UI" rule applies to the analysis/strategy crews, not the onboarding pipeline.

---

## Phase 5: Maintenance & Pruning (Weekly)

Scheduled maintenance tasks prevent unbounded data growth.

```
[Sunday 02:00 UTC — runs alongside sync_local_rag.py]
    │
    ├── DELETE FROM news_articles WHERE published_at < date('now', '-90 days')
    │   (REQ-091: 90-day news retention window)
    │
    └── DELETE FROM companies.watchlist_flags WHERE MA_RUMOR_FLAG.detected_at > 30 days
        AND no confirming 8-K found (REQ-092: stale M&A flag cleanup)
```

Nightly passive pruning (via ingestion):
- Inactive trials: `fetch_aact_csvs.py` marks trials `TERMINATED` or `WITHDRAWN` in `studies.status` automatically during upsert — no explicit delete needed
- Historical prices: retained indefinitely (small row footprint, high analytical value)

---

## Full Dataflow Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    EXTERNAL SOURCES                      │
│  SEC EDGAR  │  CT.gov v2  │  AACT CSVs  │  E*TRADE      │
│  yfinance   │  FDA (OB/PB/Orphan/PDUFA) │  RSS Feeds    │
└──────┬──────┴──────┬───────┴──────┬──────┴──────┬────────┘
       │             │              │             │
       ▼             ▼              ▼             ▼
┌─────────────────────────────────────────────────────────┐
│               INGESTION LAYER (APScheduler)              │
│  fetch_sec_filings.py  │  fetch_clinical_trials.py       │
│  fetch_aact_csvs.py    │  fetch_market_data.py           │
│  fetch_fda_data.py     │  fetch_options.py               │
│  fetch_news.py (4h)    │  onboard_company.py             │
│  sync_local_rag.py (weekly)                              │
└──────┬────────────────────────────────────┬─────────────┘
       │                                    │
       ▼                                    ▼
┌─────────────────┐               ┌────────────────────┐
│  SQLite WAL DB  │               │   ChromaDB (local) │
│ biotech_tracker │◄─────────────►│  SEC filings text  │
│     .db         │               │  Agent memos       │
│                 │               │  Trial protocols   │
└────────┬────────┘               └─────────┬──────────┘
         │                                  │
         └──────────┬───────────────────────┘
                    │
                    ▼
        ┌───────────────────────────┐
        │  CrewAI Agent Council     │
        │                           │
        │  Crew 1 (08:00, parallel) │
        │  Detective · Scout · Oracle│
        │           │               │
        │  Crew 2 (09:00, parallel) │
        │  Profiler · Peer Reviewer │
        │  Insider · Partnership    │
        │  Smart Money              │
        │           │               │
        │  Crew 3 (10:30, sequential│
        │  Volatility · Strategist  │
        └─────────────┬─────────────┘
                      │  [reads/writes SQLite]
                      │  [queries ChromaDB]
                      │  [inference via Ollama 2x Tesla P4]
                      ▼
              ┌───────────────┐
              │  SQLite WAL   │◄── Investment memos, BAS scores,
              │  (results)    │    catalyst calendar, signals
              └───────┬───────┘
                      │
                      ▼
              ┌───────────────┐
              │  Streamlit UI │◄── User (read-only dashboard)
              └───────────────┘
```

---

## Key Design Constraints

| Constraint | Value | Source |
|---|---|---|
| APScheduler pipeline completion | ≤ 11:00 AM local | REQ-063 |
| AACT sync staleness alert | > 48h since last sync | REQ-079 |
| SEC EDGAR inter-request delay | ≥ 0.1s | REQ-081 |
| ClinicalTrials.gov inter-request delay | ≥ 0.1s | REQ-002 |
| Non-destructive upserts | NULL incoming never overwrites valid | REQ-004 |
| Agent JSON output persistence | `output/{agent}/{ticker}_{YYYYMMDD}.json` | REQ-072 |
| AACT enrollment filter | `enrollment_is_actual = 1` only for TAM | REQ-074 |
| Sponsor class filter | `agency_class = 'INDUSTRY'` only | REQ-034 |
| ChromaDB partition | By therapeutic area | ARCHITECTURE |
| Ollama call cap (Scout epidemiology) | 20 conditions per call | REQ-080 |
