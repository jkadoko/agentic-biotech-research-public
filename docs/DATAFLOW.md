# Biotech-Analyzer v3.0 - Dataflow Document

## Overview
This document explicitly maps how data moves through the Biotech-Analyzer v3.0 system. The transition from v16.5 batch processing to v3.0 event-driven processing using CrewAI, Local Multi-Notebook RAG, and a single SQLite WAL database ensures maximum leverage and minimal complexity.

## Phase 1: Ingestion (Data Collection)
Raw data from various sources is gathered and normalized before being consumed by the agents.

*   **Financial & Market Data:** E*TRADE, Yfinance
    *   **Data Types:** Real-time stock prices, options chains (IV, bid/ask), enterprise value, historical price charts.
    *   **Destination:** `companies` table, `historical_prices` table in `biotech_tracker.db` (SQLite).
*   **Clinical Trial Data:** ClinicalTrials.gov API v2
    *   **Data Types:** Trial status, phase, conditions, completion dates, interventions.
    *   **Destination:** `studies`, `conditions`, `interventions`, `collaborators` tables. A junction table `trial_pipeline` links NCT IDs to tickers. Unresolved sponsor names go to `entity_aliases` via Detective (001).
*   **Regulatory & SEC Filings:** SEC EDGAR RSS Feeds
    *   **Data Types:** 10-K, 10-Q, 8-K filings, Form 4 (insider trades), Form 13G/D (institutional ownership).
    *   **Destination:** Uploaded directly to **Local Multi-Notebook RAG** via a Local Vector Router API. Documents are automatically partitioned to avoid the 500k words / 200MB limits per notebook. Essential metadata (filing date, ticker, type) is logged in `biotech_tracker.db`.
*   **Epidemiology & Orphan Drug Data:** FDA, GBD, WHO GHO API
    *   **Data Types:** Disease prevalence, orphan exclusivity dates. Sourced via Scout Agent's 4-tier waterfall (local cache → GBD CSV → WHO API → Local model search).
    *   **Destination:** `disease_context`, `orphan` tables.

## Phase 2: Orchestration & AI Council (CrewAI Execution)
The AI agents analyze the ingested data to generate actionable insights. The complex web of 23 custom scripts is replaced by a single, orchestrated CrewAI workflow.

1.  **Trigger:** A daily cron job (`scripts/scheduler.py`) kicks off the `run_daily_automation` process.
2.  **Data Retrieval (LangChain Tools):**
    *   Agents pull raw financial/clinical data from the SQLite database using custom tools (e.g., `DatabaseQueryTool`).
    *   Agents query Local Multi-Notebook RAG for complex historical or semantic analysis using the `LocalRAGTool` (e.g., "Summarize the risks section of MRNA's latest 10-K" or "Find similar Phase 3 trial failures in oncology").
3.  **Agent Processing (Ollama GPU):**
    *   **Crew 1 — Data Collection (Detective, Scout, Oracle):** Entity resolution, IPO discovery, catalyst calendar population.
    *   **Crew 2 — Analysis (Profiler, Peer Reviewer, Insider, Partnership, Smart Money):** Deep dive into specific domains. The Peer Reviewer audits trial design; Insider/Smart Money track trading signals; Profiler scores company fundamentals.
    *   **Crew 3 — Strategy (Volatility, Strategist):** Volatility calculates safe CSP strike prices; Strategist synthesizes all Crew 2 outputs into a final Investment Memo with BAS score, Kelly sizing, and a BUY/HOLD/SELL/REDUCE recommendation.
4.  **Data Storage:** Agent outputs are stored in `biotech_tracker.db` (e.g., `agent_investment_memos`, `agent_volatility_findings`, `partnerships`).

## Phase 3: Knowledge Base Synchronization (Local Multi-Notebook RAG)
A crucial loop in v3.0 is feeding agent outputs *back* into Local Multi-Notebook RAG. This provides the system with "memory" of its past decisions.

*   **Action:** A weekly cron job (`scripts/sync_local_rag.py`) runs.
*   **Process:** It queries the SQLite database for new `agent_investment_memos` and `agent_scientific_audits` that haven't been synced yet (`uploaded_to_rag = 0`).
*   **Upload:** It pushes these documents to the appropriate Local Multi-Notebook RAG partition as markdown files.
*   **Result:** The Strategist Agent can now query: "What did I recommend for BNTX last quarter, and was I right?"

## Phase 4: User Interface (Streamlit)
The Streamlit dashboard acts as the single pane of glass for the user. It is strictly read-only regarding the core database to prevent locking issues.

*   **Display:** Renders the portfolio performance, current holdings, and active signals (e.g., "High Dilution Risk for MRNA").
*   **Interaction:** Clicking a ticker fetches its static data, financial metrics, and the latest Strategist memo from SQLite.
*   **Direct RAG Query:** Provides an input box allowing the user to bypass the agents and query Local Multi-Notebook RAG directly ("Show me all Phase 3 failures for CAR-T in 2023").

## Data Flow Diagram

```text
[External APIs] ---> [Python Fetchers]
       |                    |
       | (SEC PDFs)         | (JSON/CSV)
       v                    v
[Local Vector Router API]  [SQLite DB]
       |                    |
[Local Multi-Notebook RAG] <---> [CrewAI Agents] <---> [Ollama GPU]
       ^                    |
       | (Weekly Sync)      | (Memos & Alerts)
       |                    v
[Sync Script] <------ [SQLite DB]
                            |
                      [Streamlit UI] <--- [User]
```
