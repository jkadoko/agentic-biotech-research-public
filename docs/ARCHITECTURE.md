# Biotech-Analyzer v3.0 - System Architecture

## Overview
Biotech-Analyzer v3.0 is a streamlined, AI-driven investment platform designed for maximum leverage and minimal complexity. It transitions from a complex, script-heavy desktop application to a containerized, web-based system powered by **CrewAI**, **Local Multi-Notebook RAG**, and **Streamlit**.

## Core Philosophy: Maximum Leverage, Minimum Complexity
The system leverages existing polished tools rather than reinventing the wheel:
1. **Ollama:** Local GPU inference (2x Tesla P4s) for AI Agents (llama3.2, deepseek-r1).
2. **Local Multi-Notebook RAG:** Self-hosted partitioned document retrieval system.
3. **CrewAI:** Orchestration framework for agentic workflows.
4. **Streamlit:** Python-native web dashboard for visualizing data and agent outputs.
5. **SQLite (WAL):** A robust, single-file database for transactional data.

## System Components

### 1. Data Ingestion Layer
*   **Purpose:** Fetching raw financial and clinical data.
*   **Sources:** Hybrid financial data model balancing **E*TRADE** (requires frequent auth, highly reliable fresh data like live options chains) and **yfinance** (historical prices, stale EPS/fundamentals, rate-limited). Also uses ClinicalTrials.gov API v2 (trial pipeline), SEC EDGAR (10-K/10-Q/8-K/Form 4/13G/13D filings), FDA Orange Book (small molecule patent/exclusivity), and FDA Purple Book (biologics BLA/exclusivity).
*   **Mechanism:** Python scripts executed by a Dockerized cron scheduler.
*   **Storage:** Data is normalized and stored directly in `biotech_tracker.db` (SQLite). SEC filings and trial protocols are pushed to the Local Multi-Notebook RAG system.

### 2. The Agent Council (CrewAI)
*   **Purpose:** Analyzing the raw data to extract actionable investment intelligence.
*   **Roster (10 agents across 3 crews, reduced from 23 in v16.5):**
    *   **Crew 1 — Data Collection (Parallel):** Detective/001 (Entity Resolution), Scout/002 (IPO Watch + Disease Context), Oracle/008 (PDUFA + Catalyst Calendar).
    *   **Crew 2 — Analysis (Parallel, waits for Crew 1):** Profiler/003 (Company Intelligence), Peer Reviewer/004 (Scientific Validation), Insider/005 (Form 4 Insider Tracking), Partnership/010 (BD&L Intelligence), Smart Money/011 (13G/13D Institutional Tracking).
    *   **Crew 3 — Strategy (Sequential, waits for Crew 2):** Volatility/009 (CSP Strike Selection), Strategist/006 (Final Investment Memo + Kelly Sizing).
*   **Execution:** Agents are defined as CrewAI classes with shared tools (DatabaseQueryTool, LocalRAGTool, etc. — see CREWAI_TOOLS.md). Crews 1 and 2 use parallel execution; Crew 3 is sequential (Volatility feeds Strategist).

### 3. Knowledge Base (ChromaDB Local RAG)
*   **Purpose:** Semantic search, historical context, and deep document analysis without cloud dependencies.
*   **Integration:** Utilizes CrewAI's `RagTool` configured with ChromaDB as the persistent local vector store. Embeddings are generated locally (e.g., via Ollama or BAAI). This architecture completely bypasses arbitrary word/size limits associated with notebook UI wrappers.
*   **Workflow:** When the Strategist Agent needs to know "What were the partnership terms for similar-stage biotech companies in 2023?", it queries ChromaDB via the RAG tool instead of executing complex SQL joins.

### 4. User Interface (Streamlit)
*   **Purpose:** A single-page web dashboard replacing the legacy CustomTkinter app.
*   **Features:**
    *   Portfolio Dashboard (Performance, Sharpe Ratio, Drawdown).
    *   Holdings Table and Active Signals.
    *   Interactive Agent Reports (Click a ticker to see the Strategist's memo).
    *   Direct natural language queries to Local Multi-Notebook RAG.

## Architecture Diagram (Logical Flow)

```mermaid
flowchart TD
    subgraph ext["External Sources"]
        SEC["SEC EDGAR"]
        CTv2["ClinicalTrials.gov API v2"]
        AACT["AACT Table CSVs (6 tables)"]
        MKT["E*TRADE (live) and yfinance (historical)"]
        FDA["FDA Orphan Drug DB and PDUFA Calendar"]
        OB["FDA Orange Book\n(small molecules)"]
        PB["FDA Purple Book\n(biologics)"]
    end

    subgraph ingest["Data Ingestion (Scheduler + On-Demand)"]
        IngestScripts["Python Fetchers\n(nightly batch)"]
        SyncLocalRAG["Local Vector Ingestion"]
        Onboard["onboard_company.py\n(ticker-first onboarding)"]
    end

    subgraph storage["Storage"]
        DB[("SQLite WAL")]
        NLM[("Local Multi-Notebook RAG\nKnowledge Base")]
    end

    subgraph crews["Agent Orchestration (CrewAI)"]
        DataCrew["Crew 1 — Data Collection\nDetective · Scout · Oracle"]
        AnalysisCrew["Crew 2 — Analysis\nProfiler · Peer Reviewer · Insider\nPartnership · Smart Money"]
        StrategyCrew["Crew 3 — Strategy\nVolatility · Strategist"]
        Ollama["Ollama GPU Backend\n2x Tesla P4"]
    end

    subgraph ui["User Interface"]
        UI["Streamlit Web App"]
    end

    SEC & CTv2 & AACT & MKT & FDA --> IngestScripts
    %% Onboarding pipeline: ticker → 10-K → ChromaDB embed → LLM extract → SQLite upsert → trial linkage via drug names/NCT IDs/company name
    SEC --> Onboard
    CTv2 --> Onboard
    OB & PB --> Onboard

    IngestScripts --> DB
    IngestScripts --> SyncLocalRAG
    Onboard --> DB
    Onboard --> SyncLocalRAG
    SyncLocalRAG --> NLM

    DB <--> DataCrew
    DB <--> AnalysisCrew
    DB <--> StrategyCrew

    NLM <--> AnalysisCrew
    NLM <--> StrategyCrew

    Ollama <--> DataCrew
    Ollama <--> AnalysisCrew
    Ollama <--> StrategyCrew

    StrategyCrew -->|"Investment Memos"| DB
    DB --> UI
```

## Onboarding Pipeline (Ticker-First)

The onboarding pipeline is the entry point for adding a new company to the system. It runs once per ticker and re-runs automatically when a new 10-K is filed. It operates independently of the daily Crew 1–3 cycle.

```
New Ticker (user entry or Scout Task D post-IPO)
    │
    ▼
[Step 1] Validate ticker (yfinance)
    │
    ▼
[Step 2] Fetch latest 10-K from SEC EDGAR
         SECEdgarFetcherTool → raw text + URL → sec_filings table
    │
    ▼
[Step 3] Embed 10-K into ChromaDB (LocalRAGTool write mode)
         512-token overlapping chunks, local Ollama embeddings
         Partitioned by therapeutic area
    │
    ▼
[Step 4] LLM structured extraction (llama3.1:8b)
         Extracts: drug names, NCT IDs, pipeline phases, officers, board,
         revenue stage, patent cliff dates, top risks, cash/burn/runway
    │
    ▼
[Step 5] Upsert into SQLite
         companies (financial fields, onboarding_status = COMPLETE)
         interventions (drug names per ticker)
         company_onboarding_log (audit record)
    │
    ▼
[Step 6] Link clinical trials — three-pass approach
         Pass 1: NCT IDs cited verbatim in 10-K → direct CT.gov lookup (10K_CITED)
         Pass 2: Drug name search → CT.gov /api/v2/studies?query.term={drug_name} (DRUG_NAME_MATCH)
         Pass 3: Company name → CT.gov search by sponsor OR collaborator name (COMPANY_NAME_MATCH)
                 (captures trials where company is an industry collaborator, e.g., in a partnership)
         Priority: 10K_CITED > DRUG_NAME_MATCH > COMPANY_NAME_MATCH
    │
    ▼
[Step 7] Drug database lookups
         FDA Orphan Drug DB → orphan table
         FDA Orange Book (small molecules) → interventions.orange_book_appl_no + patent_expiry
         FDA Purple Book (biologics) → interventions.purple_book_bla_no + patent_expiry
         Note: investigational drugs return NOT_FOUND — logged, does not block completion
```

**Design rationale:** Drug names (e.g., "lecanemab") and NCT IDs are stable identifiers that do not change due to M&A, subsidiary naming, or bankruptcy. Searching CT.gov by company name alone yielded a ~30% Zero Trial failure rate in legacy testing. The 10-K provides a legally mandated, authoritative inventory of all active pipeline assets with frequent NCT ID citations.

**Trigger conditions:**
- User adds ticker via Streamlit UI → Scout Task D triggers immediately
- Scout Task A (IPO detection) → Scout Task D triggers post-triage
- Daily scheduler (07:00): runs `onboard_company.py` for any ticker with `onboarding_status = PENDING` or `STALE`
- `fetch_sec_filings.py` detects new 10-K filing → sets `onboarding_status = STALE` for that ticker

## Deployment (Docker Compose)
The entire stack is orchestrated via a single `docker-compose.yml` file, ensuring identical environments between development (Google Antigravity IDE) and production (Dell R730 server).

*   **biotech-app:** Runs the Streamlit UI and exposes port 8501.
*   **ollama-gpu0 / ollama-gpu1:** Two dedicated instances managing model inference across the two Tesla P4s.
*   **chromadb:** Connects to the ChromaDB local vector store.
*   **scheduler:** Runs the daily ingestion and agent execution tasks.

## Security & Best Practices
*   **Environment Variables:** API keys (E*TRADE) are injected securely via `.env`.
*   **Volumes:** `biotech_tracker.db` and LLM models are mounted as persistent volumes outside the container lifecycle.
*   **Access Control:** The Streamlit dashboard runs on a local port, accessible only via secure VPN or reverse proxy (e.g., Nginx + OAuth).
