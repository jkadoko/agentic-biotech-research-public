"""
Agent 001 — Detective: Entity Resolution Specialist

Spec: docs/AGENT_001_DETECTIVE.md v2.2
Crew: Data Collection (Crew 1)
Model: llama3.1:8b (Ollama GPU0)

Resolves ClinicalTrials.gov INDUSTRY sponsor names → public ticker symbols.
5-step logic: alias cache → exact match → fuzzy → web search → alias registration.
"""

import os

from crewai import Agent, LLM, Task

from src.tools.clinicaltrials_tool import ClinicalTrialsTool
from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.search_tool import DuckDuckGoSearchTool
from src.tools.sec_edgar_tool import SECEdgarFetcherTool

_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")


def make_detective_agent() -> Agent:
    llm = LLM(model="ollama/llama3.1:8b", base_url=_GPU0)
    return Agent(
        role="Entity Resolution Specialist",
        goal=(
            "Map every ClinicalTrials.gov INDUSTRY sponsor name to its public ticker "
            "symbol with ≥90% resolution rate and ≤1% false positives. "
            "Maintain the entity_aliases cache so future lookups are instant."
        ),
        backstory=(
            "You are a biotech M&A genealogist with encyclopedic knowledge of "
            "pharmaceutical acquisitions. You know that Kite Pharma → GILD, "
            "Immunomedics → GILD, Celgene → BMY, Spark → RHHBY, and hundreds more. "
            "You always check the alias cache first and only do expensive web searches "
            "when the cache and fuzzy matching both fail. You never map NIH, NCI, "
            "or academic sponsors to tickers — they are not investable entities."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            DuckDuckGoSearchTool(),
            ClinicalTrialsTool(),
            SECEdgarFetcherTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=15,
    )


def make_detective_task(agent: Agent, sponsor_names: list[str]) -> Task:
    sponsors_str = "\n".join(f"- {s}" for s in sponsor_names)
    return Task(
        description=(
            f"Resolve the following ClinicalTrials.gov INDUSTRY sponsor names to public "
            f"ticker symbols:\n{sponsors_str}\n\n"
            "STEP 1 — Pre-Triage (Rule-Based):\n"
            "  - For sponsors where sponsor_class is NIH, FED, NETWORK, or OTHER:\n"
            "    Write to entity_aliases immediately: alias=sponsor, ticker=null,\n"
            "    relationship_type=GOVERNMENT (for NIH/FED/NETWORK) or ACADEMIC (for OTHER),\n"
            "    confidence=1.00, resolution_method=ALIAS_TABLE_EXACT.\n"
            "    Then SKIP Steps 2–4 for these — they are not investable entities.\n"
            "  - For INDUSTRY sponsors: check cache first:\n"
            "    SELECT ticker FROM entity_aliases WHERE LOWER(alias) = LOWER(sponsor).\n"
            "  - If found with confidence >= 0.85: use it (resolution_method=ALIAS_TABLE_EXACT), skip Steps 2–3.\n"
            "  - Query: SELECT ticker FROM companies WHERE LOWER(company_name) = LOWER(sponsor).\n"
            "  - If exact match: use it (confidence=1.00, resolution_method=EXACT_MATCH).\n\n"
            "STEP 2 — Fuzzy Pre-Screen (thefuzz library, only if Step 1 missed):\n"
            "  - Use thefuzz.process.extractOne(sponsor_name, company_list, "
            "    scorer=fuzz.token_sort_ratio) to score against companies.company_name.\n"
            "    Score is returned as 0–100; divide by 100 for 0.0–1.0 float.\n"
            "  - Score >= 0.95: HIGH confidence candidate — proceed to Step 3 for verification.\n"
            "  - Score 0.85–0.94: LOW confidence candidate — proceed to Step 3 for web verification.\n"
            "  - Score < 0.85: No useful match — proceed to Step 3 with open web search.\n"
            "  NOTE: A high fuzzy score alone is NOT sufficient — always verify via Step 3.\n\n"
            "STEP 3 — Web Investigation (runs for all unresolved sponsors):\n"
            "  - DuckDuckGoSearchTool: '{sponsor} acquired by pharmaceutical company SEC 8-K'\n"
            "  - DuckDuckGoSearchTool: '{sponsor} parent company public stock ticker'\n"
            "  - Check acquisition date vs trial start_date in studies table.\n"
            "    If trial started BEFORE acquisition: map to PARENT company ticker.\n"
            "    If trial started AFTER acquisition: standard mapping.\n\n"
            "STEP 4 — Alias Registration (DatabaseWriteTool on entity_aliases):\n"
            "  - Write each resolved mapping with: alias (raw sponsor name), ticker,\n"
            "    canonical_name, relationship_type, acquisition_date (null if not acquired),\n"
            "    confidence, resolution_method, source_url.\n"
            "  - Confidence rubric (spec Section 6 — use exact values):\n"
            "    1.00: Exact match in entity_aliases table\n"
            "    0.98: Acquisition confirmed in SEC 8-K filing\n"
            "    0.95: Acquisition confirmed in official press release\n"
            "    0.92: Strong fuzzy match (≥0.90) + subsidiary confirmed by web search\n"
            "    0.88: Moderate fuzzy match (0.85–0.90) + web search confirms\n"
            "    0.72: Fuzzy match only, no web confirmation → mark UNRESOLVED (below threshold)\n"
            "    0.00: No match found → UNRESOLVED\n"
            "  - resolution_method enum: ALIAS_TABLE_EXACT | EXACT_MATCH | FUZZY_MATCH | "
            "    WEB_SEARCH_CONFIRMED | WEB_SEARCH_PRIVATE_CONFIRMED.\n\n"
            "STEP 5 — Update trial_pipeline (for all resolved INDUSTRY tickers):\n"
            "  - For each sponsor resolved to a real ticker (not GOVERNMENT/ACADEMIC/PRIVATE):\n"
            "    DatabaseWriteTool: table=trial_pipeline, data={\n"
            "      ticker: <resolved_ticker>,\n"
            "      nct_id: <from studies WHERE lead_sponsor = <alias>>,\n"
            "      source_type: 'ENTITY_RESOLVED',\n"
            "      confidence_score: <confidence from Step 4>\n"
            "    }.\n"
            "  - The studies table has lead_sponsor column — use it to get NCT IDs.\n"
            "  - On conflict (ticker, nct_id already exists): do not overwrite higher confidence.\n\n"
            "CRITICAL SCOPE GUARD: Do NOT process sponsors whose company has "
            "onboarding_status = 'COMPLETE' — trial linkage for those is owned by "
            "onboard_company.py.\n\n"
            "Return JSON: [{\"sponsor\": str, \"ticker\": str|null, "
            "\"canonical_name\": str|null, \"relationship_type\": str, "
            "\"acquisition_date\": str|null, \"confidence\": float, "
            "\"resolution_method\": str, \"source_url\": str|null}]"
        ),
        expected_output=(
            "JSON array: each element has sponsor, ticker (str or null), "
            "canonical_name (str or null), relationship_type "
            "(SUBSIDIARY|EXACT_MATCH|ACQUISITION|PRIVATE|GOVERNMENT|ACADEMIC|UNRESOLVED), "
            "acquisition_date (str or null), confidence (float 0.0–1.0), "
            "resolution_method (ALIAS_TABLE_EXACT|EXACT_MATCH|FUZZY_MATCH|"
            "WEB_SEARCH_CONFIRMED|WEB_SEARCH_PRIVATE_CONFIRMED), source_url (str or null)."
        ),
        agent=agent,
    )
