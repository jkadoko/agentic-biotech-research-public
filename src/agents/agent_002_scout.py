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
            "IPO WATCH — Detect new biotech IPOs from the last 30 days.\n\n"
            "STEP 1: Search SEC EDGAR for recent S-1 and S-1/A filings:\n"
            "  - DuckDuckGoSearchTool: 'biotech biopharma IPO S-1 SEC filing site:sec.gov last 30 days'\n"
            "  - DuckDuckGoSearchTool: 'biotech IPO 2025 NASDAQ NYSE upcoming'\n\n"
            "STEP 2: For each discovered IPO candidate:\n"
            "  - Check LocalRAGTool (sec_filings collection) for any existing S-1 filing text.\n"
            "  - Verify it is a therapeutic company (not CDMO/CRO/platform-only).\n"
            "  - Extract: company_name, ticker (if assigned), exchange, indication, lead_asset.\n\n"
            "STEP 3: Cross-check against companies table:\n"
            "  - DatabaseQueryTool: SELECT ticker FROM companies WHERE company_name LIKE '%{name}%'.\n"
            "  - If NOT in watchlist: insert via DatabaseWriteTool with onboarding_status='PENDING'.\n"
            "  - If already present: skip.\n\n"
            "STEP 4: For each new company added, append 'IPO_WATCH' to watchlist_flags JSON:\n"
            "  - DatabasePatchTool on companies table, operation=append_json_array, "
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
            "  - DatabaseQueryTool: SELECT * FROM disease_context WHERE indication_name = '{indication}'.\n"
            "  - If found and last_updated within 90 days: use cached data, skip Tiers 2–4.\n\n"
            "TIER 2 — GBD 2019 CSV (data/gbd_2019_clean.csv via DatabaseQueryTool or file read):\n"
            "  - Look up prevalence_per_100k and incidence_per_100k by indication.\n"
            "  - Source = 'GBD_2019'.\n\n"
            "TIER 3 — WHO GHO API:\n"
            "  - DuckDuckGoSearchTool: 'WHO GHO {indication} prevalence statistics site:who.int'\n"
            "  - Extract global prevalence figure. Source = 'WHO_GHO'.\n\n"
            "TIER 4 — LLM Estimate (last resort):\n"
            "  - Use your own knowledge to estimate prevalence.\n"
            "  - Source = 'LLM_ESTIMATE'. Flag with lower confidence.\n\n"
            "For each indication, calculate:\n"
            "  - us_prevalence = global_prevalence × 0.043 (US share)\n"
            "  - tam_usd = us_prevalence × annual_treatment_cost_usd × 0.30 (penetration)\n\n"
            "Write results via DatabaseWriteTool to disease_context table.\n\n"
            "Return JSON: [{\"ticker\": str, \"indication\": str, "
            "\"prevalence_per_100k\": float, \"tam_usd\": float, \"source\": str}]"
        ),
        expected_output=(
            "JSON array of disease context records, one per (ticker, indication) pair, "
            "with prevalence_per_100k, tam_usd, and data source."
        ),
        agent=agent,
    )


def make_new_trial_discovery_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"NEW TRIAL DISCOVERY — Find trials for {ticker} not yet in the local database.\n\n"
            "STEP 1: Query local DB for known NCT IDs:\n"
            "  - DatabaseQueryTool: SELECT nct_id FROM studies WHERE ticker = '{ticker}'.\n\n"
            "STEP 2: Query ClinicalTrials.gov via ClinicalTrialsTool:\n"
            "  - Search by sponsor name for {ticker}.\n"
            "  - Search by known drug names from trial_pipeline table.\n\n"
            "STEP 3: Compare CT.gov results vs local NCT IDs.\n"
            "  - For each NEW nct_id not in local DB:\n"
            "    - Extract: nct_id, brief_title, phase, status, start_date, primary_completion_date.\n"
            "    - DatabaseWriteTool: upsert into studies table.\n\n"
            "Return JSON: {\"ticker\": str, \"new_trials_found\": int, "
            "\"nct_ids\": [str]}"
        ),
        expected_output=(
            "JSON with ticker, count of newly discovered trials, and list of new NCT IDs."
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
            "    operation=append_json_array, column=watchlist_flags, value='MA_TARGET_SIGNAL'.\n"
            "  - Only flag if deal_type is 'acquisition' or 'merger' (not rumor).\n\n"
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
