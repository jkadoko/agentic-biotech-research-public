"""
Agent 003 — Profiler: Company Intelligence

Spec: docs/AGENT_003_PROFILER.md v2.2
Crew: Analysis (Crew 2)
Model: llama3.1:8b (Ollama GPU0)

Builds comprehensive qualitative company profile: management quality, TAM,
patent cliff, competitive positioning. Outputs Profiler Score (0–100).

Scoring weights:
  30% management quality + 25% TAM attractiveness + 20% patent cliff + 25% competitive advantage
"""

import os

from crewai import Agent, LLM, Task

from src.tools.clinicaltrials_tool import ClinicalTrialsTool
from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.local_rag_tool import LocalRAGTool
from src.tools.search_tool import DuckDuckGoSearchTool

_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")


def make_profiler_agent() -> Agent:
    llm = LLM(model="ollama/llama3.1:8b", base_url=_GPU0)
    return Agent(
        role="Company Intelligence Analyst",
        goal=(
            "Produce a comprehensive company profile with a Profiler Score (0–100) "
            "covering management quality, TAM size, patent cliff risk, and competitive "
            "positioning. Trigger BANKRUPTCY_IMMINENT or MANAGEMENT_FRAUD kill switches "
            "immediately when warranted — these override all other scores."
        ),
        backstory=(
            "You are a buy-side analyst who has covered biotech for 15 years. You know "
            "that management execution matters as much as the science — a CEO who has "
            "delivered two successful drug approvals is worth 20 points on your scorecard. "
            "You use bottom-up TAM calculations (prevalence × price × penetration), not "
            "top-down market research reports. You use ClinicalTrialsTool for competitor "
            "landscape — never raw SQL — so you get live CT.gov data for rivals."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            DuckDuckGoSearchTool(),
            ClinicalTrialsTool(),
            LocalRAGTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=20,
    )


def make_profiler_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"COMPANY PROFILE — Build a full intelligence profile for {ticker}.\n\n"
            "PRE-CHECK — Kill Switches (run first, return immediately if triggered):\n"
            "  - DatabaseQueryTool: SELECT cash_usd, burn_rate_monthly_usd, runway_months, "
            "    market_cap_usd FROM companies WHERE ticker = '{ticker}'.\n"
            "  - BANKRUPTCY_IMMINENT: runway_months < 3 OR "
            "(runway_months < 6 AND cash_usd < burn_rate_monthly_usd × 3).\n"
            "  - DuckDuckGoSearchTool: '{ticker} SEC investigation fraud accounting restatement'\n"
            "  - MANAGEMENT_FRAUD: SEC investigation confirmed OR accounting restatement.\n"
            "  - If kill switch triggered: write kill_switch to agent_profiler_findings "
            "    and return {\"kill_switch\": \"BANKRUPTCY_IMMINENT\"|\"MANAGEMENT_FRAUD\"}.\n\n"
            "TASK A — Management Quality Score (30% weight, 0–30 pts):\n"
            "  - LocalRAGTool: query sec_filings for 'CEO experience drug approval track record'\n"
            "  - DuckDuckGoSearchTool: '{ticker} CEO CMO board biotech experience approvals'\n"
            "  - Score criteria:\n"
            "    +8: CEO has ≥2 prior drug approvals at previous companies\n"
            "    +4: CEO has 1 prior drug approval\n"
            "    +3: CEO has pharma/biotech leadership but no prior approval\n"
            "    +6: Insider ownership > 5% of shares outstanding\n"
            "    +4: Cash dilution < 15% annually (check historical raises)\n"
            "    +5: Clinical execution rate ≥ 80% (trials completed on time)\n"
            "    -5: CEO tenure < 1 year (transition risk)\n"
            "    -8: History of excessive dilution (>30% annually)\n"
            "  - management_score = sum (cap at 30).\n\n"
            "TASK B — TAM Analysis (25% weight, 0–25 pts):\n"
            "  - DatabaseQueryTool: SELECT indication_name, prevalence_per_100k, tam_usd "
            "    FROM disease_context WHERE ticker = '{ticker}'.\n"
            "  - For each indication without disease_context data:\n"
            "    DuckDuckGoSearchTool: '{indication} US prevalence patients annual treatment cost'\n"
            "  - Bottom-up TAM: us_patients × annual_price_usd × 0.30 (penetration rate).\n"
            "  - Primary indication TAM scoring:\n"
            "    25 pts: TAM > $5B | 20 pts: $2–5B | 15 pts: $1–2B | 10 pts: $500M–1B | 5 pts: <$500M\n"
            "  - Extract rNPV: DatabaseQueryTool from company financials if available.\n"
            "  - company_type: THERAPEUTIC (has drug candidates) vs PLATFORM (AI/tool/CRO).\n\n"
            "TASK C — Patent Cliff Detection (20% weight, 0–20 pts):\n"
            "  - DuckDuckGoSearchTool: '{ticker} patent expiry exclusivity cliff biosimilar'\n"
            "  - Classify: LOW (no cliff within 5 years, 20 pts), "
            "    MODERATE (cliff 3–5 years, 10 pts), CRITICAL (cliff <3 years, 0 pts).\n\n"
            "TASK D — Competitive Landscape (25% weight, 0–25 pts):\n"
            "  - For each primary indication, use ClinicalTrialsTool to find top 3 competitors:\n"
            "    query: '{indication} Phase 3', status: ACTIVE_RECRUITING|ACTIVE_NOT_RECRUITING.\n"
            "  - Classify competition:\n"
            "    LOW (0–1 Phase 3 competitors, 25 pts)\n"
            "    MODERATE (2–3 competitors, 15 pts)\n"
            "    HIGH (4–6 competitors, 8 pts)\n"
            "    SATURATED (>6 competitors, 2 pts)\n"
            "  - Competitive advantage: FIRST_MOVER, SUPERIOR, COMPARABLE, INFERIOR.\n\n"
            "SCORING: profiler_score = management_score + tam_score + patent_score + competition_score.\n\n"
            "Write to agent_profiler_findings via DatabaseWriteTool:\n"
            "  ticker, profiler_score, tam_usd, rnpv_usd, competition_score,\n"
            "  competitive_advantage, company_type, patent_cliff, kill_switch (if any).\n\n"
            "Return JSON: {\"ticker\": str, \"profiler_score\": int, \"tam_usd\": float, "
            "\"competition_score\": int, \"competitive_advantage\": str, "
            "\"company_type\": str, \"patent_cliff\": str, \"kill_switch\": str|null}"
        ),
        expected_output=(
            "JSON with ticker, profiler_score (0–100), tam_usd, competition_score, "
            "competitive_advantage (FIRST_MOVER|SUPERIOR|COMPARABLE|INFERIOR), "
            "company_type (THERAPEUTIC|PLATFORM), patent_cliff (LOW|MODERATE|CRITICAL), "
            "and kill_switch (null or BANKRUPTCY_IMMINENT|MANAGEMENT_FRAUD)."
        ),
        agent=agent,
    )
