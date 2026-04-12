"""
Agent 002 — Scout: IPO Watcher & Scientific Context Builder

Spec: docs/AGENT_002_SCOUT.md v2.1
Crew: Data Collection (Crew 1)
Model: llama3.1:8b (Ollama GPU0)

Tasks: IPO Watch, Disease Context (4-tier waterfall), New Trial Discovery,
Ticker Onboarding Handoff, M&A News Signal.
"""

import os

from crewai import Agent, LLM, Task

from src.tools.clinicaltrials_tool import ClinicalTrialsTool
from src.tools.db_tool import DatabasePatchTool, DatabaseQueryTool, DatabaseWriteTool
from src.tools.local_rag_tool import LocalRAGTool
from src.tools.search_tool import DuckDuckGoSearchTool

_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")


def make_scout_agent() -> Agent:
    llm = LLM(model="ollama/llama3.1:8b", base_url=_GPU0)
    return Agent(
        role="IPO Watcher & Scientific Context Builder",
        goal=(
            "Discover new biotech IPOs and S-1 filings daily, enrich disease context "
            "for every indication in the watchlist, and flag M&A signals so the "
            "portfolio always has the freshest intelligence on pipeline opportunities."
        ),
        backstory=(
            "You are a biotech scout combining the instincts of a venture analyst with "
            "the discipline of a clinical epidemiologist. You know that GBD 2019 "
            "prevalence data is more reliable than LLM estimates, so you always try "
            "the local cache and official WHO data before falling back to web search. "
            "You spot IPO filings within 24 hours of SEC submission and flag any news "
            "that smells like an acquisition target."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            DatabasePatchTool(),
            DuckDuckGoSearchTool(),
            ClinicalTrialsTool(),
            LocalRAGTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=20,
    )


def make_ipo_watch_task(agent: Agent) -> Task:
    return Task(
        description=(
            "IPO WATCH — Detect new biotech IPOs from the last 7 days.\n\n"
            "STEP 1: Search SEC EDGAR for recent S-1 and S-1/A filings:\n"
            "  - DuckDuckGoSearchTool: 'biotech biopharma IPO S-1 SEC filing site:sec.gov last 7 days'\n"
            "  - DuckDuckGoSearchTool: \"site:efts.sec.gov/LATEST/search-index biotech S-1 dateRange=custom startdt={today-7d}\"\n\n"
            "STEP 2: For each discovered IPO candidate:\n"
            "  - Check LocalRAGTool (sec_filings collection) for any existing S-1 filing text.\n"
            "  - Verify it is a therapeutic company (not CDMO/CRO/platform-only).\n"
            "  - Extract: company_name, ticker (if assigned), exchange, indication, lead_asset.\n\n"
            "STEP 3: Cross-check against companies table:\n"
            "  - DatabaseQueryTool: SELECT ticker FROM companies WHERE company_name LIKE '%{name}%'.\n"
            "  - If NOT in watchlist: insert via DatabaseWriteTool with onboarding_status='PENDING'.\n"
            "  - If already present: skip.\n\n"
            "STEP 4: For each new company added, append 'IPO_WATCH' to watchlist_flags JSON:\n"
            "  - DatabasePatchTool on companies table, operation=append, "
            "    column=watchlist_flags, value='IPO_WATCH'.\n\n"
            "Return JSON: [{\"ticker\": str, \"company_name\": str, \"indication\": str, "
            "\"action\": \"ADDED\"|\"ALREADY_TRACKED\"|\"SKIPPED_NON_THERAPEUTIC\"}]"
        ),
        expected_output=(
            "JSON array of IPO candidates processed, with ticker, company_name, "
            "indication, and action taken."
        ),
        agent=agent,
    )


def make_disease_context_task(agent: Agent, tickers: list[str]) -> Task:
    tickers_str = ", ".join(tickers)
    return Task(
        description=(
            f"DISEASE CONTEXT — Enrich disease context for tickers: {tickers_str}\n\n"
            "For each ticker, find all indications in its trial pipeline and enrich "
            "with prevalence, incidence, and market size using a 4-tier waterfall:\n\n"
            "TIER 1 — Local Cache:\n"
            "  - DatabaseQueryTool: SELECT * FROM disease_context WHERE condition_normalized = '{indication}'.\n"
            "  - If found and last_updated within 90 days: use cached data, skip Tiers 2–4.\n\n"
            "TIER 2 — GBD 2019 CSV (data/gbd_2019_clean.csv):\n"
            "  - Read file and look up prevalence_us and prevalence_global by indication.\n"
            "  - Source = 'GBD_2019'. Covers top 300 conditions by global burden.\n\n"
            "TIER 3 — WHO GHO API (direct HTTP — not a web search):\n"
            "  - GET https://ghoapi.azureedge.net/api/Indicator?$filter=contains(IndicatorName,'{indication}')\n"
            "  - Extract indicator code, then GET https://ghoapi.azureedge.net/api/{indicator_code}\n"
            "  - Extract global prevalence figure (NumericValue field). Source = 'WHO_GHO'.\n"
            "  - Rate limit: 10 req/min. Only call if Tier 2 returned no result.\n\n"
            "TIER 4 — LLM Batch Search (REQ-080 — last resort, 20 conditions per call):\n"
            "  - Collect all conditions reaching Tier 4 (not found in Tiers 1–3).\n"
            "  - Batch in groups of 20 per llama3.1:8b call with this prompt:\n"
            "    'For each condition below, provide: (1) US prevalence (integer patients),\n"
            "     (2) Global prevalence (integer), (3) Is FDA Orphan (< 200,000 US patients),\n"
            "     (4) Annual mortality rate (string). Output JSON array with fields:\n"
            "     condition, prevalence_us, prevalence_global, is_orphan, mortality_rate, source.'\n"
            "  - Source = 'LLM_ESTIMATE'. Flag data_tier=4 with lower confidence.\n\n"
            "For each indication, if only global prevalence found:\n"
            "  - prevalence_us = prevalence_global × 0.043 (US share of world population)\n\n"
            "Write results via DatabaseWriteTool to disease_context table "
            "(condition_normalized, condition_raw, prevalence_us, prevalence_global, "
            "data_tier, source, last_updated).\n\n"
            "Return JSON: [{\"ticker\": str, \"indication\": str, "
            "\"prevalence_us\": int, \"prevalence_global\": int, \"source\": str}]"
        ),
        expected_output=(
            "JSON array of disease context records, one per (ticker, indication) pair, "
            "with prevalence_us, prevalence_global, and data source."
        ),
        agent=agent,
    )


def make_new_trial_discovery_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"NEW TRIAL DISCOVERY — Find trials for {ticker} not yet in the local database.\n\n"
            "STEP 1: Query local DB for known NCT IDs:\n"
            "  - DatabaseQueryTool: SELECT nct_id FROM trial_pipeline WHERE ticker = '{ticker}'.\n\n"
            "STEP 2: Query ClinicalTrials.gov via ClinicalTrialsTool:\n"
            "  - Search by sponsor name for {ticker}.\n"
            "  - Search by known drug names from trial_pipeline table.\n\n"
            "STEP 3: Compare CT.gov results vs local NCT IDs.\n"
            "  - For each NEW nct_id not in local DB:\n"
            "    - Extract: nct_id, title, phase, status, start_date, primary_completion_date.\n"
            "    - DatabaseWriteTool: upsert into studies table.\n\n"
            "STEP 4: Identify unknown INDUSTRY sponsors for Detective handoff.\n"
            "  - From new trials found in Step 3, extract all lead_sponsor strings where\n"
            "    class = 'INDUSTRY'.\n"
            "  - DatabaseQueryTool: SELECT alias FROM entity_aliases WHERE alias IN ({sponsor_list}).\n"
            "  - For any INDUSTRY sponsor NOT already in entity_aliases:\n"
            "    note it in the return JSON under 'unknown_sponsors' — Detective will resolve these.\n"
            "    (Detective runs in parallel in Crew 1 and will pick up these new unresolved sponsors.)\n\n"
            "Return JSON: {\"ticker\": str, \"new_trials_found\": int, "
            "\"nct_ids\": [str], \"unknown_sponsors\": [str]}"
        ),
        expected_output=(
            "JSON with ticker, count of newly discovered trials, list of new NCT IDs, "
            "and list of unknown INDUSTRY sponsor names needing Detective resolution."
        ),
        agent=agent,
    )


def make_ticker_onboarding_handoff_task(agent: Agent) -> Task:
    return Task(
        description=(
            "TICKER ONBOARDING HANDOFF — Identify PENDING companies that need full onboarding.\n\n"
            "STEP 1: Query for companies awaiting onboarding:\n"
            "  - DatabaseQueryTool: SELECT ticker, company_name, watchlist_flags "
            "    FROM companies WHERE onboarding_status = 'PENDING' OR onboarding_status IS NULL "
            "    ORDER BY added_by DESC LIMIT 10.\n\n"
            "STEP 2: For each PENDING ticker:\n"
            "  - Verify it is a therapeutic company (not CDMO/CRO) using:\n"
            "    DatabaseQueryTool: SELECT company_type FROM agent_profiler_findings "
            "    WHERE ticker = '{ticker}' LIMIT 1.\n"
            "  - If therapeutic or company_type is NULL (not yet profiled): queue for onboarding.\n\n"
            "STEP 3: Mark each queued ticker as IN_PROGRESS:\n"
            "  - DatabaseWriteTool: table=companies, data={ticker, onboarding_status='IN_PROGRESS'}.\n\n"
            "STEP 4: Trigger onboarding by writing a handoff record:\n"
            "  - DatabaseWriteTool: table=company_onboarding_log, data={\n"
            "    ticker, onboarding_date=today, trigger_source='SCOUT_IPO'}.\n"
            "  - This record is polled by scripts/onboard_company.py to initiate\n"
            "    the 7-step onboarding pipeline (SEC filing fetch → ChromaDB embed → trial linkage).\n\n"
            "Return JSON: {\"queued_tickers\": [str], \"skipped_non_therapeutic\": [str]}"
        ),
        expected_output=(
            "JSON with queued_tickers (list of tickers handed off to onboard_company.py) "
            "and skipped_non_therapeutic (list of tickers skipped as CDMO/CRO)."
        ),
        agent=agent,
    )


def make_ma_news_signal_task(agent: Agent) -> Task:
    return Task(
        description=(
            "M&A NEWS SIGNAL — Scan recent news for acquisition and merger signals.\n\n"
            "STEP 1: Query recent news articles from the last 48 hours:\n"
            "  - DatabaseQueryTool: SELECT headline, ticker, url FROM news_articles "
            "    WHERE category = 'm&a' AND published_at > datetime('now', '-48 hours').\n\n"
            "STEP 2: For each M&A article:\n"
            "  - Extract: acquirer, target, deal_value, deal_type (acquisition/merger/offer).\n"
            "  - Determine if target is in our watchlist.\n\n"
            "STEP 3: Flag watchlist companies:\n"
            "  - DatabasePatchTool on companies table:\n"
            "    operation=append, column=watchlist_flags,\n"
            "    value_dict={'flag': 'MA_RUMOR_FLAG', 'direction': 'TARGET'|'ACQUIRER',\n"
            "               'source': 'RSS_NEWS', 'headline': <headline_text>,\n"
            "               'source_url': <url>, 'detected_at': <ISO timestamp>}.\n"
            "  - direction='TARGET' if our company is acquisition target (bullish for CSP).\n"
            "  - direction='ACQUIRER' if our company is acquiring another.\n"
            "  - Only flag if deal_type is 'acquisition' or 'merger' (not vague 'exploring options').\n"
            "  - Do NOT flag for 'strategic review' or 'evaluating alternatives' headlines alone.\n\n"
            "STEP 4: For each confirmed M&A signal, log to output.\n\n"
            "Return JSON: [{\"ticker\": str, \"acquirer\": str, \"deal_type\": str, "
            "\"flagged\": bool}]"
        ),
        expected_output=(
            "JSON array of M&A signals found, with target ticker, acquirer, "
            "deal type, and whether the watchlist flag was applied."
        ),
        agent=agent,
    )
