# CrewAI Shared Tools Catalog

**Version:** 2.1
**Last Updated:** 2026-03-15
**Aligned with:** PRD v3.5y
**Purpose:** Documents every shared tool available to CrewAI agents. Each tool is a Python class that implements the CrewAI `BaseTool` interface. Agents declare which tools they use in their `tools` list parameter.

---

## Tool Architecture Pattern

All tools follow this CrewAI pattern:

```python
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

class MyToolInput(BaseModel):
    param: str = Field(description="Description for the LLM to understand how to use this parameter")

class MyTool(BaseTool):
    name: str = "my_tool"
    description: str = "One-sentence description the LLM uses to decide when to call this tool"
    args_schema: type[BaseModel] = MyToolInput

    def _run(self, param: str) -> str:
        # Implementation
        return result
```

Tools are instantiated once and passed to agents:
```python
db_tool = DatabaseQueryTool(db_path="biotech_tracker.db")
agent = Agent(role="Detective", tools=[db_tool, web_search_tool])
```

---

## Tool Index

| Tool Name | File | Used By Agents | Purpose |
|---|---|---|---|
| `DatabaseQueryTool` | `src/tools/db_tool.py` | All agents | Read from SQLite (includes `news_articles`) |
| `DatabaseWriteTool` | `src/tools/db_tool.py` | All agents (write) | REQ-004-compliant upsert to SQLite (never overwrites non-NULL with NULL) |
| `DatabasePatchTool` | `src/tools/db_tool.py` | 002 Scout | Partial JSON column update (e.g., append to `watchlist_flags`) without touching other columns |
| `LocalRAGTool` | `src/tools/local_rag_tool.py` | 002, 003, 004, 005, 006, 008, 009, 010, 011 | Semantic search over ChromaDB collections |
| `SECEdgarFetcherTool` | `src/tools/sec_edgar_tool.py` | 005, 011 | Fetch Form 4, 13G/13D filings from EDGAR |
| `ClinicalTrialsTool` | `src/tools/clinicaltrials_tool.py` | 001, 002, 003, 004, 008, 010 | Query ClinicalTrials.gov API v2 |
| `DuckDuckGoSearchTool` | `src/tools/search_tool.py` | 001, 002, 003, 004, 008 | Web search via DuckDuckGo (no API key required) |
| `OptionsChainTool` | `src/tools/options_tool.py` | 009 | Fetch live options chain from E*TRADE |
| `OllamaLLMTool` | `src/tools/ollama_tool.py` | 006 (Strategist), 010 (Partnership) | Run local LLM inference via Ollama |

---

## 1. DatabaseQueryTool

**File:** `src/tools/db_tool.py`
**Used By:** All agents
**Purpose:** Execute read-only SQL queries against the SQLite database.

```python
class DatabaseQueryInput(BaseModel):
    query: str = Field(description="SQL SELECT query to execute against biotech_tracker.db")

class DatabaseQueryTool(BaseTool):
    name: str = "database_query"
    description: str = (
        "Execute a SQL SELECT query against the biotech database. "
        "Tables: companies, trial_pipeline, studies, conditions, catalysts, "
        "entity_aliases, disease_context, partnerships, agent_profiler_findings, "
        "agent_scientific_audits, agent_insider_findings, agent_volatility_findings, "
        "agent_smart_money_findings, smart_money_positions, agent_investment_memos, "
        "options_chains, orphan, news_articles, sec_filings, interventions, "
        "collaborators, design_outcomes, company_onboarding_log, historical_prices."
    )
    args_schema: type[BaseModel] = DatabaseQueryInput
    db_path: str = "biotech_tracker.db"

    def _run(self, query: str) -> str:
        import sqlite3, json
        # SAFETY: Only allow SELECT statements
        if not query.strip().upper().startswith("SELECT"):
            return "Error: Only SELECT queries are permitted."
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query)
            rows = [dict(row) for row in cursor.fetchall()]
            return json.dumps(rows[:100])  # cap at 100 rows to prevent context overflow
```

**Usage examples by agents:**
```sql
-- Detective: find unresolved sponsors
SELECT DISTINCT lead_sponsor FROM studies
WHERE lead_sponsor NOT IN (SELECT alias FROM entity_aliases)
  AND lead_sponsor_class = 'INDUSTRY'

-- Oracle: find ACTIVE_NOT_RECRUITING Phase 3 trials (PDUFA candidates)
SELECT s.ticker, s.nct_id, s.primary_completion_date
FROM trial_pipeline s
JOIN studies st ON s.nct_id = st.nct_id
WHERE st.status = 'ACTIVE_NOT_RECRUITING' AND st.phase = 'PHASE3'
  AND st.primary_completion_date < DATE('now', '-6 months')
```

---

## 2. DatabaseWriteTool

**File:** `src/tools/db_tool.py`
**Used By:** All agents (for writing output)
**Purpose:** REQ-004-compliant upsert to SQLite. `None` values in `data` are **never written** — existing non-NULL column values are always preserved. Uses SQLite's `ON CONFLICT DO UPDATE` with a `CASE WHEN excluded.col IS NOT NULL` guard for each non-PK column.

**⚠️ REQ-004 Compliance Note:** The previous `INSERT OR REPLACE` implementation violated REQ-004 — it deleted and re-inserted the full row, wiping any columns not present in `data`. This version uses a conditional ON CONFLICT upsert so that NULL incoming values never overwrite existing data.

```python
class DatabaseWriteInput(BaseModel):
    table: str = Field(description="Target table name")
    data: dict = Field(description="Dictionary of column->value pairs to upsert. "
                                   "None/null values are ignored — will NOT overwrite existing non-NULL data (REQ-004).")

# Primary key column(s) per table. Required to build the ON CONFLICT clause.
# MUST match SCHEMA.md exactly — SQLite's ON CONFLICT clause uses the actual PK constraint.
# Composite PKs are expressed as a list. All values must be present in the data dict.
PRIMARY_KEY_MAP: dict = {
    # SCHEMA.md Section 3: Entity Resolution
    "entity_aliases":             ["alias"],                             # PK: alias TEXT
    # SCHEMA.md Section 4: Disease Epidemiology
    "disease_context":            ["condition_normalized"],              # PK: condition_normalized TEXT
    # SCHEMA.md Section 2: Clinical Trials (Linkage)
    "trial_pipeline":             ["ticker", "nct_id"],                  # PK: (ticker, nct_id)
    # SCHEMA.md Section 8: Agent Output Tables
    "agent_profiler_findings":    ["ticker", "profile_date"],            # PK: (ticker, profile_date)
    "agent_scientific_audits":    ["ticker", "nct_id", "audit_date"],    # PK: (ticker, nct_id, audit_date)
    "agent_insider_findings":     ["ticker", "scan_date"],               # PK: (ticker, scan_date)
    "catalysts":                  ["ticker", "event_type", "event_date"],# PK: (ticker, event_type, event_date)
    "agent_volatility_findings":  ["ticker", "scan_date"],               # PK: (ticker, scan_date)
    "partnerships":               ["ticker", "partner_name", "drug_asset"],# PK: (ticker, partner_name, drug_asset)
    "agent_smart_money_findings": ["ticker", "scan_date"],               # PK: (ticker, scan_date)
    "agent_investment_memos":     ["ticker", "memo_date"],               # PK: (ticker, memo_date)
    # SCHEMA.md Section 1: Core Reference
    "companies":                  ["ticker"],                            # PK: ticker TEXT
    # SCHEMA.md Section 8: Agent Output Tables (cont.)
    "smart_money_positions":      ["ticker", "institution_name", "filing_date"],  # PK: (ticker, institution_name, filing_date)
    # SCHEMA.md Section 9: News
    "news_articles":              ["id"],                                # PK: id INTEGER (auto-increment)
}

class DatabaseWriteTool(BaseTool):
    name: str = "database_write"
    description: str = (
        "REQ-004-compliant upsert: write agent findings to a database table. "
        "NULL/None values in data dict are NEVER written — existing non-NULL values are preserved. "
        "The primary key column(s) must be included in the data dict."
    )
    args_schema: type[BaseModel] = DatabaseWriteInput
    db_path: str = "biotech_tracker.db"

    def _run(self, table: str, data: dict) -> str:
        import sqlite3
        if table not in PRIMARY_KEY_MAP:
            return f"Error: Table '{table}' is not in the allowed write list."

        pk_cols = PRIMARY_KEY_MAP[table]
        for pk in pk_cols:
            if pk not in data:
                return f"Error: Primary key column '{pk}' must be present in data dict."

        # REQ-004: strip None values — never overwrite existing data with NULL
        clean_data = {k: v for k, v in data.items() if v is not None}
        if not clean_data:
            return "Warning: No non-null values to write."

        cols = ", ".join(clean_data.keys())
        placeholders = ", ".join(["?"] * len(clean_data))
        conflict_target = ", ".join(pk_cols)

        # For non-PK columns: only overwrite if the incoming value is not NULL
        non_pk_cols = [k for k in clean_data if k not in pk_cols]
        if non_pk_cols:
            set_clause = ", ".join(
                [f"{c} = CASE WHEN excluded.{c} IS NOT NULL THEN excluded.{c} ELSE {table}.{c} END"
                 for c in non_pk_cols]
            )
            sql = (f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
                   f"ON CONFLICT({conflict_target}) DO UPDATE SET {set_clause}")
        else:
            # Only PK columns provided — insert new row if not exists, no-op if exists
            sql = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(sql, list(clean_data.values()))
            conn.commit()
        return f"OK: Upserted 1 row into {table} (pk={[data[p] for p in pk_cols]}, {len(non_pk_cols)} data columns written)"
```

---

## 2b. DatabasePatchTool

**File:** `src/tools/db_tool.py`
**Used By:** Agent 002 (Scout — Task E M&A news flags)
**Purpose:** Perform a **partial update** to a single JSON column on an existing row without touching any other columns. Required for Scout Task E's `json_insert` pattern on `companies.watchlist_flags`. `DatabaseWriteTool` cannot safely do this because it must receive the full row to avoid losing data; `DatabasePatchTool` issues a targeted `UPDATE … SET json_col = json_insert(…)` directly.

```python
class DatabasePatchInput(BaseModel):
    table: str = Field(description="Target table (must be in PATCH_ALLOWED_TABLES)")
    pk_column: str = Field(description="Primary key column name (e.g., 'ticker')")
    pk_value: str = Field(description="Primary key value identifying the row to patch")
    json_column: str = Field(description="Name of the JSON column to patch (e.g., 'watchlist_flags')")
    operation: str = Field(
        description="JSON operation to perform. Options: "
                    "'append' — appends value_dict as a new element to a JSON array; "
                    "'set_key' — sets a top-level key in a JSON object to value_dict."
    )
    value_dict: dict = Field(description="Dict to append (for 'append') or {key: value} to set (for 'set_key')")

PATCH_ALLOWED_TABLES = {"companies"}

class DatabasePatchTool(BaseTool):
    name: str = "database_patch_json"
    description: str = (
        "Perform a targeted JSON column patch on a single row. "
        "Use to append a flag object to companies.watchlist_flags (Scout Task E M&A signals) "
        "without touching price, runway, or any other company column. REQ-004 safe."
    )
    args_schema: type[BaseModel] = DatabasePatchInput
    db_path: str = "biotech_tracker.db"

    def _run(self, table: str, pk_column: str, pk_value: str,
             json_column: str, operation: str, value_dict: dict) -> str:
        import sqlite3, json
        if table not in PATCH_ALLOWED_TABLES:
            return f"Error: Table '{table}' is not in the patch-allowed list."
        if operation not in ("append", "set_key"):
            return f"Error: operation must be 'append' or 'set_key'."
        value_json = json.dumps(value_dict)
        with sqlite3.connect(self.db_path) as conn:
            if operation == "append":
                # Append value_dict to the JSON array, initializing to [] if NULL
                sql = (f"UPDATE {table} SET {json_column} = "
                       f"json_insert(COALESCE({json_column}, '[]'), '$[#]', json(?)) "
                       f"WHERE {pk_column} = ?")
            else:  # set_key
                key = list(value_dict.keys())[0]
                val = list(value_dict.values())[0]
                sql = (f"UPDATE {table} SET {json_column} = "
                       f"json_set(COALESCE({json_column}, '{{}}'), '$.{key}', ?) "
                       f"WHERE {pk_column} = ?")
                value_json = json.dumps(val)
            conn.execute(sql, [value_json, pk_value])
            conn.commit()
        return f"OK: Patched {table}.{json_column} for {pk_column}={pk_value} (op={operation})"
```

**Usage by Scout Task E:**
```python
# Step 3: Write M&A rumor flag to companies.watchlist_flags
patch_tool._run(
    table="companies",
    pk_column="ticker",
    pk_value="MRNA",
    json_column="watchlist_flags",
    operation="append",
    value_dict={
        "flag": "MA_RUMOR",
        "direction": "TARGET",
        "headline": "Pfizer rumored to be eyeing Moderna acquisition",
        "source": "BioPharma Dive",
        "detected_at": "2026-03-14T08:30:00"
    }
)
```

---

## 3. LocalRAGTool

**File:** `src/tools/local_rag_tool.py`
**Used By:** Agents 002, 003, 004, 005, 006, 008, 009, 010, 011
**Purpose:** Semantic search over ChromaDB local vector store. Collections: `sec_filings`, `trial_protocols`, `agent_memos`. Embeddings generated locally via `mxbai-embed-large:latest` (Ollama). Collections must be initialized with `OllamaEmbeddingFunction(model="mxbai-embed-large:latest", url=OLLAMA_HOST)` before first use — see `scripts/init_chromadb.py`. No word/size limits — ChromaDB handles unlimited documents.

```python
class LocalRAGQueryInput(BaseModel):
    query: str = Field(
        description="Natural language question to ask the ChromaDB knowledge base. "
                    "Contains: 10-K/10-Q SEC filings, 8-K event filings, prior investment memos, "
                    "scientific audit reports, and trial protocols. "
                    "Example: 'What are the key patent risks in MRNA latest 10-K?'"
    )
    collection: str = Field(
        description="ChromaDB collection to query. Options: 'sec_filings', 'agent_memos', "
                    "'trial_protocols', or a therapeutic area (e.g., 'oncology', 'neurology'). "
                    "Use 'sec_filings' for 10-K/8-K/Form4. Use 'agent_memos' for historical recommendations."
    )
    ticker: str = Field(default=None, description="Optional: scope the search to a specific ticker")

class LocalRAGTool(BaseTool):
    name: str = "local_rag_query"
    description: str = (
        "Semantic search over the ChromaDB local knowledge base containing SEC filings, "
        "trial protocols, and prior agent memos. Specify the collection to search. "
        "Use for complex queries that SQL cannot answer: 'Find similar Phase 3 CAR-T failures in DLBCL', "
        "'What were partnership terms in comparable oncology deals?'"
    )
    args_schema: type[BaseModel] = LocalRAGQueryInput
    chroma_client: object = None  # Injected ChromaDB PersistentClient

    def _run(self, query: str, collection: str, ticker: str = None) -> str:
        import json
        scoped_query = f"[{ticker}] {query}" if ticker else query
        coll = self.chroma_client.get_collection(collection)
        results = coll.query(query_texts=[scoped_query], n_results=5)
        # Returns top-5 document chunks with metadata (source, ticker, filing_date)
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        return json.dumps([{"text": d, "source": m} for d, m in zip(docs, metas)])
```

**Critical queries by agent:**
```
# Profiler (003)
"What is the dilution history and ATM offering record for [Ticker] in the past 3 years?"
"When do [Ticker]'s key patents expire? Any biosimilar threats mentioned?"

# Oracle (008)
"Did [Ticker] file an NDA or BLA acceptance letter with the FDA? What is the PDUFA date?"
"Is [Ticker] presenting at ASCO or ASH this year? Any abstract submissions mentioned?"

# Insider (005)
"Does [Insider Name] at [Ticker] have a Rule 10b5-1 trading plan disclosed in proxy filings?"

# Partnership (010)
"What are the financial terms of [Ticker]'s collaboration agreement with [Partner]?"
```

---

## 4. SECEdgarFetcherTool

**File:** `src/tools/sec_edgar_tool.py`
**Used By:** Agents 005 (Form 4), 011 (13G/13D)
**Purpose:** Fetch raw filing text from SEC EDGAR API. Handles rate limiting and User-Agent headers.

```python
class SECEdgarFetchInput(BaseModel):
    cik: str = Field(description="Company CIK number (10-digit, zero-padded)")
    form_types: list[str] = Field(
        description="List of form types to fetch, e.g. ['4'], ['SC 13G', 'SC 13G/A', 'SC 13D']"
    )
    lookback_days: int = Field(default=90, description="Number of days to look back for filings")

class SECEdgarFetcherTool(BaseTool):
    name: str = "sec_edgar_fetch"
    description: str = (
        "Fetch SEC filings from EDGAR API for a given company CIK. "
        "Use for Form 4 (insider transactions) and SC 13G/13D (institutional ownership). "
        "Returns list of filing metadata with download URLs."
    )
    args_schema: type[BaseModel] = SECEdgarFetchInput
    user_agent: str  # e.g. "biotech-analyzer yourname@email.com" (EDGAR requirement)

    def _run(self, cik: str, form_types: list[str], lookback_days: int = 90) -> str:
        import requests, time, json
        from datetime import datetime, timedelta
        headers = {"User-Agent": self.user_agent}
        url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
        time.sleep(0.1)  # EDGAR rate limit: max 10 req/sec
        resp = requests.get(url, headers=headers)
        filings = resp.json().get("filings", {}).get("recent", {})
        cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        results = []
        for i, form in enumerate(filings.get("form", [])):
            if form in form_types and filings["filingDate"][i] >= cutoff:
                results.append({
                    "form": form,
                    "date": filings["filingDate"][i],
                    "accession": filings["accessionNumber"][i],
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{filings['accessionNumber'][i].replace('-','')}"
                })
        return json.dumps(results)
```

---

## 5. ClinicalTrialsTool

**File:** `src/tools/clinicaltrials_tool.py`
**Used By:** Agents 001, 002, 003, 004, 008, 010
**Purpose:** Query ClinicalTrials.gov API v2 for trial data.

```python
class ClinicalTrialsQueryInput(BaseModel):
    query: str = Field(description="Search terms for trial title or condition")
    sponsor: str = Field(default=None, description="Sponsor name filter")
    status: str = Field(default=None, description="Trial status filter: RECRUITING, ACTIVE_NOT_RECRUITING, COMPLETED, TERMINATED")
    phase: str = Field(default=None, description="Phase filter: PHASE1, PHASE2, PHASE3")
    max_results: int = Field(default=20, description="Maximum results to return (max 100)")

class ClinicalTrialsTool(BaseTool):
    name: str = "clinicaltrials_search"
    description: str = (
        "Search ClinicalTrials.gov API v2 for clinical trials. "
        "Use to find: trial status for a company, competitor trials in an indication, "
        "ACTIVE_NOT_RECRUITING Phase 3 trials (NDA candidates), terminated trials. "
        "Returns NCT ID, title, phase, status, sponsor, completion dates."
    )
    args_schema: type[BaseModel] = ClinicalTrialsQueryInput
    BASE_URL: str = "https://clinicaltrials.gov/api/v2/studies"

    def _run(self, query: str, sponsor: str = None, status: str = None,
             phase: str = None, max_results: int = 20) -> str:
        import requests, json
        params = {
            "query.term": query,
            "pageSize": min(max_results, 100),
            "fields": "NCTId,BriefTitle,Phase,OverallStatus,LeadSponsorName,LeadSponsorClass,PrimaryCompletionDate,Condition"
        }
        if sponsor:
            params["query.spons"] = sponsor
        if status:
            params["filter.overallStatus"] = status
        if phase:
            params["filter.advanced"] = f"AREA[Phase]{phase}"
        resp = requests.get(self.BASE_URL, params=params)
        studies = resp.json().get("studies", [])
        results = []
        for s in studies:
            ps = s.get("protocolSection", {})
            results.append({
                "nct_id": ps.get("identificationModule", {}).get("nctId"),
                "title": ps.get("identificationModule", {}).get("briefTitle"),
                "phase": ps.get("designModule", {}).get("phases", []),
                "status": ps.get("statusModule", {}).get("overallStatus"),
                "sponsor": ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name"),
                "sponsor_class": ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("class"),
                "completion_date": ps.get("statusModule", {}).get("primaryCompletionDateStruct", {}).get("date")
            })
        return json.dumps(results)
```

**⚠️ API v2 Field Mapping Note:** ClinicalTrials.gov replaced API v1 in 2024. All field paths use nested JSON format: `protocolSection.statusModule.overallStatus` (not flat `status`). See [full field list](https://clinicaltrials.gov/data-api/api-docs/).

---

## 6. DuckDuckGoSearchTool

**File:** `src/tools/search_tool.py`
**Used By:** Agents 001, 002, 003, 004, 008
**Purpose:** Execute web searches via DuckDuckGo. No API key or account required. Local Ollama models have no internet access — this tool is required for all real-time web lookups.

```python
class DuckDuckGoSearchInput(BaseModel):
    query: str = Field(description="Search query string. Be specific. Include company names, drug names, and dates.")
    num_results: int = Field(default=5, description="Number of search results to return (max 10)")

class DuckDuckGoSearchTool(BaseTool):
    name: str = "web_search"
    description: str = (
        "Search the web for current information about biotech companies, FDA decisions, "
        "clinical trial results, acquisitions, and PDUFA dates. "
        "Returns titles, URLs, and snippets from top results. No API key required."
    )
    args_schema: type[BaseModel] = DuckDuckGoSearchInput

    def _run(self, query: str, num_results: int = 5) -> str:
        import json
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=min(num_results, 10)):
                results.append({"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")})
        return json.dumps(results)
```

**Note:** All agents run on local Ollama models (100% local inference). Local models have no native internet access, so `DuckDuckGoSearchTool` is required for all real-time web lookups. No credentials needed — `duckduckgo-search` (`pip install duckduckgo-search`) is the only dependency.

---

## 7. OptionsChainTool

**File:** `src/tools/options_tool.py`
**Used By:** Agent 009 (Volatility)
**Purpose:** Fetch live options chain data from E*TRADE API for a given ticker.

```python
class OptionsChainInput(BaseModel):
    ticker: str = Field(description="Stock ticker symbol (e.g., 'CRSP')")
    max_dte: int = Field(default=45, description="Maximum days to expiration filter")
    option_type: str = Field(default="PUT", description="PUT or CALL")

class OptionsChainTool(BaseTool):
    name: str = "options_chain_fetch"
    description: str = (
        "Fetch live options chain data for a ticker. Returns put/call contracts with "
        "strike, expiration, bid, ask, IV (implied volatility), and open interest. "
        "Automatically filters to contracts with DTE <= max_dte."
    )
    args_schema: type[BaseModel] = OptionsChainInput

    def _run(self, ticker: str, max_dte: int = 45, option_type: str = "PUT") -> str:
        # E*TRADE API v3 (Morgan Stanley migration in progress)
        # Fallback: Yahoo Finance yfinance.Ticker.option_chain(date) for each expiry
        from datetime import datetime, timedelta
        import yfinance as yf, json
        tk = yf.Ticker(ticker)
        expiries = [e for e in tk.options
                    if (datetime.strptime(e, "%Y-%m-%d") - datetime.now()).days <= max_dte]
        results = []
        for exp in expiries:
            chain = tk.option_chain(exp)
            contracts = chain.puts if option_type == "PUT" else chain.calls
            for _, row in contracts.iterrows():
                results.append({
                    "expiration": exp,
                    "strike": row["strike"],
                    "bid": row["bid"],
                    "ask": row["ask"],
                    "iv": row.get("impliedVolatility"),
                    "open_interest": row.get("openInterest"),
                    "dte": (datetime.strptime(exp, "%Y-%m-%d") - datetime.now()).days
                })
        return json.dumps(results)
```

**E*TRADE Auth Reference:** `scripts/EtradePythonClient/` is the official OAuth1 reference client for E*TRADE. It demonstrates the full auth flow (`rauth.OAuth1Service` → request token → browser authorize → access token → authenticated session) and exposes `Accounts` (account list, portfolio, balance), `Market` (live quotes), and `Order` (preview, view, cancel) modules. `config.ini` in that directory holds `CONSUMER_KEY` and `CONSUMER_SECRET`. Use this as the auth pattern when implementing `ingestion/fetch_options.py`.

**⚠️ E*TRADE Deprecation Note:** E*TRADE API is being migrated to Morgan Stanley API. Until migration completes, the `yfinance` fallback provides free options data but lacks real-time bid/ask precision. For production, subscribe to CBOE DataShop or use Interactive Brokers TWS API.

---

## 8. OllamaLLMTool

**File:** `src/tools/ollama_tool.py`
**Used By:** Agents 006 (Strategist), 010 (Partnership NLP extraction)
**Purpose:** Run local LLM inference via Ollama for privacy-sensitive tasks (investment memos must stay local) and structured NLP extraction from SEC filings.

```python
class OllamaInferenceInput(BaseModel):
    prompt: str = Field(description="Full prompt to send to the local LLM")
    model: str = Field(
        default="deepseek-r1:7b-q4_K_M",
        description="Ollama model name. Options: deepseek-r1:7b-q4_K_M (reasoning), llama3.2:3b (fast extraction)"
    )
    temperature: float = Field(default=0.1, description="Temperature (0.0–1.0). Use 0.0–0.1 for structured extraction.")

class OllamaLLMTool(BaseTool):
    name: str = "ollama_inference"
    description: str = (
        "Run inference on a local Ollama LLM. Use deepseek-r1:7b-q4_K_M for chain-of-thought reasoning "
        "(investment synthesis, strategy evaluation). Use llama3.2:3b for fast structured extraction "
        "(partnership entity extraction from SEC text, JSON parsing). "
        "All data processed here stays on-premises."
    )
    args_schema: type[BaseModel] = OllamaInferenceInput
    ollama_host: str = "http://ollama-gpu0:11434"

    def _run(self, prompt: str, model: str = "deepseek-r1:7b-q4_K_M", temperature: float = 0.1) -> str:
        import requests, json
        payload = {"model": model, "prompt": prompt, "temperature": temperature, "stream": False}
        resp = requests.post(f"{self.ollama_host}/api/generate", json=payload, timeout=120)
        return resp.json().get("response", "")
```

**VRAM Notes (Tesla P4, 8GB each):**
| Model | VRAM Required | Use Case |
|---|---|---|
| `llama3.1:8b` | ~5.5 GB | Primary model: onboarding 10-K extraction, most agent tasks |
| `deepseek-r1:7b-q4_K_M` | ~4.5 GB | Strategist reasoning chains (q4 quantized) |
| `llama3.2:3b` | ~2.0 GB | Low-latency: Partnership NLP extraction, Volatility |
| `deepseek-r1:7b-q4_K_M` + `llama3.2:3b` simultaneously | ~6.5 GB | Fits on single P4 |
| `llama3.1:8b` | ~5.5 GB | Runs on GPU1 while GPU0 runs deepseek-r1 |
| `deepseek-r1:70b` | ~40+ GB | NOT FEASIBLE on P4 — do not attempt |

---

## 9. Agent-Tool Assignment Matrix

`news_articles` is accessed via `DatabaseQueryTool` — agents that use news are marked in the News column.

| Agent | DB Read | DB Write | DB Patch (JSON) | ChromaDB RAG | SEC EDGAR | ClinicalTrials | Google Search | Options | Ollama | News (via DB) |
|---|---|---|---|---|---|---|---|---|---|---|
| 001 Detective | ✓ | ✓ | | | | ✓ | ✓ | | | |
| 002 Scout | ✓ | ✓ | ✓ (Task E) | ✓ | | ✓ | ✓ | | | ✓ |
| 003 Profiler | ✓ | ✓ | | ✓ | | ✓ | ✓ | | | |
| 004 Peer Reviewer | ✓ | ✓ | | ✓ | | ✓ | ✓ | | | |
| 005 Insider | ✓ | ✓ | | ✓ | ✓ | | | | | |
| 006 Strategist | ✓ | ✓ | | ✓ | | | | | ✓ | |
| 008 Oracle | ✓ | ✓ | | ✓ | | ✓ | ✓ | | | ✓ |
| 009 Volatility | ✓ | ✓ | | ✓ | | | | ✓ | | |
| 010 Partnership | ✓ | ✓ | | ✓ | | ✓ | | | ✓ | ✓ |
| 011 Smart Money | ✓ | ✓ | | ✓ | ✓ | | | | | ✓ |

---

## 10. Error Handling Conventions

All tools should follow this error return pattern — returning an error string (not raising exceptions) keeps the agent loop running:

```python
def _run(self, ...) -> str:
    try:
        # ... tool logic ...
        return json.dumps(result)
    except requests.exceptions.Timeout:
        return "ERROR: Request timed out. Try again or use a cached result."
    except requests.exceptions.HTTPError as e:
        return f"ERROR: HTTP {e.response.status_code} — {str(e)}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {str(e)}"
```

Agents receiving an `ERROR:` prefixed response should:
1. Log the error in their output JSON `error_notes` field.
2. Use `null` for the affected data field in DB output.
3. Continue processing other inputs — never abort the entire crew run for a single tool failure.
