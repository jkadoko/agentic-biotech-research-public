# CrewAI Shared Tools Catalog

**Version:** 1.0
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
agent = Agent(role="Detective", tools=[db_tool, google_search_tool])
```

---

## Tool Index

| Tool Name | File | Used By Agents | Purpose |
|---|---|---|---|
| `DatabaseQueryTool` | `tools/db_tool.py` | All agents | Read from SQLite |
| `DatabaseWriteTool` | `tools/db_tool.py` | All agents (write) | Write/upsert to SQLite |
| `LocalRAGTool` | `tools/local_rag_tool.py` | 003, 004, 005, 006, 008, 010, 011 | Semantic search over partitioned notebooks |
| `SECEdgarFetcherTool` | `tools/sec_edgar_tool.py` | 005, 011 | Fetch Form 4, 13G/13D filings from EDGAR |
| `ClinicalTrialsTool` | `tools/clinicaltrials_tool.py` | 001, 003, 004, 008, 010 | Query ClinicalTrials.gov API v2 |
| `GoogleSearchTool` | `tools/search_tool.py` | 001, 002, 003, 008 | Web search via Local model/Google Search API |
| `OptionsChainTool` | `tools/options_tool.py` | 009 | Fetch live options chain from E*TRADE |
| `OllamaLLMTool` | `tools/ollama_tool.py` | 006 (Strategist), 010 (Partnership) | Run local LLM inference via Ollama |

---

## 1. DatabaseQueryTool

**File:** `tools/db_tool.py`
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
        "agent_smart_money_findings, agent_investment_memos, options_chains, orphan."
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

**File:** `tools/db_tool.py`
**Used By:** All agents (for writing output)
**Purpose:** Upsert agent findings to SQLite. Wraps a parameterized INSERT OR REPLACE.

```python
class DatabaseWriteInput(BaseModel):
    table: str = Field(description="Target table name")
    data: dict = Field(description="Dictionary of column->value pairs to upsert")

class DatabaseWriteTool(BaseTool):
    name: str = "database_write"
    description: str = "Upsert a record into a database table. Provide table name and data dict."
    args_schema: type[BaseModel] = DatabaseWriteInput

    def _run(self, table: str, data: dict) -> str:
        import sqlite3
        # Whitelist tables to prevent injection
        ALLOWED_TABLES = {
            "entity_aliases", "disease_context", "agent_profiler_findings",
            "agent_scientific_audits", "agent_insider_findings", "catalysts",
            "agent_volatility_findings", "partnerships", "agent_smart_money_findings",
            "agent_investment_memos", "companies"
        }
        if table not in ALLOWED_TABLES:
            return f"Error: Table '{table}' is not in the allowed write list."
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(sql, list(data.values()))
            conn.commit()
        return f"OK: Upserted 1 row into {table}"
```

---

## 3. LocalRAGTool

**File:** `tools/local_rag_tool.py`
**Used By:** Agents 003, 004, 005, 006, 008, 010, 011
**Purpose:** Query the Local Multi-Notebook RAG knowledge base (containing all SEC filings, trial protocols, and prior agent memos) using natural language.

```python
class LocalRAGQueryInput(BaseModel):
    query: str = Field(
        description="Natural language question to ask the Local RAG knowledge base. "
                    "The KB is partitioned into multiple notebooks. "
                    "Contains: 10-K/10-Q SEC filings, 8-K event filings, prior memos, "
                    "and scientific audit reports. "
                    "Example: 'What are the key patent risks in MRNA latest 10-K?'"
    )
    notebook_category: str = Field(description="The partition to query (e.g., Notebook_SEC_Filings)")
    ticker: str = Field(default=None, description="Optional: scope the search to a specific ticker")

class LocalRAGTool(BaseTool):
    name: str = "local_rag_query"
    description: str = (
        "Query the Local Multi-Notebook RAG semantic knowledge base containing SEC filings and prior agent memos. "
        "Must specify the notebook_category to respect 500k words / 200MB limits per notebook."
    )
    args_schema: type[BaseModel] = LocalRAGQueryInput
    local_rag_client: object = None  # Injected Local RAG client

    def _run(self, query: str, notebook_category: str, ticker: str = None) -> str:
        scoped_query = f"[{ticker}] {query}" if ticker else query
        response = self.local_rag_client.query(scoped_query, notebook=notebook_category)
        # response contains cited passages with source references
        return response.text  # Returns cited answer as string
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

**File:** `tools/sec_edgar_tool.py`
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

**File:** `tools/clinicaltrials_tool.py`
**Used By:** Agents 001, 003, 004, 008, 010
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

## 6. GoogleSearchTool

**File:** `tools/search_tool.py`
**Used By:** Agents 001, 002, 003, 008
**Purpose:** Execute web searches via Google Programmable Search API or Local model's built-in search capability.

```python
class GoogleSearchInput(BaseModel):
    query: str = Field(description="Search query string. Be specific. Include company names, drug names, and dates.")
    num_results: int = Field(default=5, description="Number of search results to return (max 10)")

class GoogleSearchTool(BaseTool):
    name: str = "google_search"
    description: str = (
        "Search the web for current information about biotech companies, FDA decisions, "
        "clinical trial results, acquisitions, and PDUFA dates. "
        "Returns titles, URLs, and snippets from top results."
    )
    args_schema: type[BaseModel] = GoogleSearchInput
    api_key: str  # Google Custom Search API key or Local model API key
    cse_id: str   # Custom Search Engine ID (for Google CSE) or None for Local model

    def _run(self, query: str, num_results: int = 5) -> str:
        import requests, json
        params = {"key": self.api_key, "cx": self.cse_id, "q": query, "num": num_results}
        resp = requests.get("https://www.googleapis.com/customsearch/v1", params=params)
        items = resp.json().get("items", [])
        results = [{"title": i["title"], "url": i["link"], "snippet": i["snippet"]} for i in items]
        return json.dumps(results)
```

**Note for Local model-native agents (001, 002, 003, 008):** When using `llama3.1:8b` with search enabled via Vertex AI, this tool is often unnecessary — Local model's native grounding tool handles search inline. Use this tool only when the agent model is a local Ollama model without internet access.

---

## 7. OptionsChainTool

**File:** `tools/options_tool.py`
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

**⚠️ E*TRADE Deprecation Note:** E*TRADE API is being migrated to Morgan Stanley API. Until migration completes, the `yfinance` fallback provides free options data but lacks real-time bid/ask precision. For production, subscribe to CBOE DataShop or use Interactive Brokers TWS API.

---

## 8. OllamaLLMTool

**File:** `tools/ollama_tool.py`
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
| `deepseek-r1:7b-q4_K_M` | ~4.5 GB | Strategist reasoning chains |
| `llama3.2:3b` | ~2.0 GB | Partnership NLP extraction |
| `deepseek-r1:7b-q4_K_M` + `llama3.2:3b` simultaneously | ~6.5 GB | Fits on single P4 |
| `deepseek-r1:70b` | ~40+ GB | NOT FEASIBLE on P4 |

---

## 9. Agent-Tool Assignment Matrix

| Agent | DB Read | DB Write | Local Multi-Notebook RAG | SEC EDGAR | ClinicalTrials | Google Search | Options | Ollama |
|---|---|---|---|---|---|---|---|---|
| 001 Detective | ✓ | ✓ | | | ✓ | ✓ | | |
| 002 Scout | ✓ | ✓ | | | ✓ | ✓ | | |
| 003 Profiler | ✓ | ✓ | ✓ | | ✓ | ✓ | | |
| 004 Peer Reviewer | ✓ | ✓ | ✓ | | ✓ | | | |
| 005 Insider | ✓ | ✓ | ✓ | ✓ | | | | |
| 006 Strategist | ✓ | ✓ | ✓ | | | | | ✓ |
| 008 Oracle | ✓ | ✓ | ✓ | | ✓ | ✓ | | |
| 009 Volatility | ✓ | ✓ | | | | | ✓ | |
| 010 Partnership | ✓ | ✓ | ✓ | | ✓ | | | ✓ |
| 011 Smart Money | ✓ | ✓ | ✓ | ✓ | | | | |

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
