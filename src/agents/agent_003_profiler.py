"""
Agent 003 — Profiler: Company Intelligence

Spec: docs/AGENT_003_PROFILER.md v2.2
Crew: Analysis (Crew 2)
Model: llama3.1:8b (Ollama GPU0)

Builds comprehensive qualitative company profile: management quality, TAM,
patent cliff, competitive positioning. Outputs profiler_score (0–100).

Scoring weights:
  30% management quality + 25% TAM attractiveness + 20% patent_cliff_risk + 25% competitive advantage
  competition_score: TEXT label (LOW|MODERATE|HIGH) per REQ-015
  patent_cliff_risk: LOW|ELEVATED|CRITICAL (not MODERATE) per SCHEMA.md
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
            "  - DatabaseQueryTool: SELECT total_cash_usd, burn_rate_monthly_usd, runway_months, "
            "    market_cap_usd FROM companies WHERE ticker = '{ticker}'.\n"
            "  - BANKRUPTCY_IMMINENT check (must pass ALL of these to avoid trigger):\n"
            "    - If runway_months < 3: ALWAYS trigger (no exceptions).\n"
            "    - If runway_months < 6 AND total_cash_usd < burn_rate_monthly_usd × 3: trigger\n"
            "      UNLESS a Tier 1 partner deal (check partnerships table: quality_score ≥ 3\n"
            "      AND status='ACTIVE') OR an ATM facility is confirmed (LocalRAGTool: query\n"
            "      sec_filings for '{ticker} ATM at-the-market equity facility shelf registration').\n"
            "    - If runway_months < 12 with no partner AND no ATM: CAUTION (reduce all scores 50%).\n"
            "  - DuckDuckGoSearchTool: '{ticker} SEC investigation fraud accounting restatement'\n"
            "  - MANAGEMENT_FRAUD: SEC investigation confirmed OR accounting restatement.\n"
            "  - If kill switch triggered: write kill_switch=True, kill_switch_reason to "
            "    agent_profiler_findings and return {\"kill_switch\": true, "
            "    \"kill_switch_reason\": \"BANKRUPTCY_IMMINENT\"|\"MANAGEMENT_FRAUD\"}.\n\n"
            "TASK A — Management Quality Score (30% weight):\n"
            "  - LocalRAGTool: query sec_filings for 'CEO experience drug approval track record'\n"
            "  - DuckDuckGoSearchTool: '{ticker} CEO CMO board biotech experience approvals'\n"
            "  - Raw score (0–65, then normalize to 0–100, then × 0.30 = management component):\n"
            "    +25: CEO has ≥1 prior successful FDA approval at a previous company\n"
            "    +15: CEO has prior Phase 3 success (even without approval) at a previous company\n"
            "    -15: CEO's prior company went bankrupt or was reverse-merged\n"
            "    +10: Management owns >5% of shares outstanding (from proxy via LocalRAGTool)\n"
            "    -10: Management sold >50% of holdings in last 12 months\n"
            "    +20: Clinical execution rate >60% (trials hitting primary endpoints / total completed)\n"
            "    +0: Execution rate 40–60%\n"
            "    -20: Execution rate <40%\n"
            "    +10: Company has NOT issued equity in 2+ years (no dilution)\n"
            "    -10: ATM offering below book value (per occurrence)\n"
            "  - management_raw = sum above. management_component = (management_raw / 65) × 30.\n\n"
            "TASK B — TAM Analysis (25% weight, 0–25 pts):\n"
            "  - DatabaseQueryTool: SELECT dc.condition_normalized, dc.prevalence_us "
            "    FROM disease_context dc "
            "    WHERE dc.condition_normalized IN ("
            "      SELECT c.condition_normalized FROM conditions c "
            "      JOIN trial_pipeline tp ON c.nct_id = tp.nct_id "
            "      WHERE tp.ticker = '{ticker}' AND c.condition_normalized IS NOT NULL).\n"
            "  - For each indication without disease_context data:\n"
            "    DuckDuckGoSearchTool: '{indication} US prevalence patients annual treatment cost'\n"
            "  - Bottom-up TAM: prevalence_us × annual_price_usd × penetration_rate (10–30%).\n"
            "    Orphan (<200k US patients): $150k–$500k/yr pricing tier.\n"
            "    Competitive market: discount 30–70% from list price.\n"
            "    Generic market (>5 approved): $10k–$30k/yr ceiling.\n"
            "  - Primary indication TAM scoring (0–25 pts):\n"
            "    25 pts: TAM > $1B | 18 pts: $500M–$1B | 12 pts: $100M–$500M | 5 pts: <$100M\n"
            "  - rNPV Calculation (REQ-023): For each active trial in trial_pipeline:\n"
            "    Phase POS: Phase 1=15%, Phase 2=30%, Phase 3=60%, NDA/BLA=90%.\n"
            "    rNPV_per_trial = TAM_estimate_usd × POS.\n"
            "    rnpv_usd = Σ(rNPV_per_trial) across all active trials (no double-counting per indication).\n"
            "  - company_type: THERAPEUTIC (has drug candidates) vs PLATFORM (AI/tool/CRO).\n\n"
            "TASK C — Patent Cliff Detection (20% weight, 0–20 pts):\n"
            "  - LocalRAGTool: query sec_filings for 'patent expiry exclusivity key product'\n"
            "  - DuckDuckGoSearchTool: '{ticker} patent expiry exclusivity cliff biosimilar'\n"
            "  - Classify using patent_cliff_risk (authoritative SCHEMA.md enum):\n"
            "    LOW (>48 months or pre-revenue): 20 pts.\n"
            "    ELEVATED (24–48 months to expiry): 10 pts.\n"
            "    CRITICAL (<24 months to expiry): 0 pts.\n"
            "  - For biologics: flag if biosimilars received FDA tentative approval.\n\n"
            "TASK D — Competitive Landscape (25% weight, 0–25 pts):\n"
            "  - For each primary indication, use ClinicalTrialsTool to count Phase 3 competitors:\n"
            "    query: '{indication} Phase 3', status: RECRUITING|ACTIVE_NOT_RECRUITING.\n"
            "  - Classify competition_score (TEXT label, per REQ-015):\n"
            "    LOW (0–2 Phase 3 competitors): 25 pts\n"
            "    MODERATE (3–5 competitors): 15 pts\n"
            "    HIGH (≥6 competitors): 5 pts\n"
            "  - Competitive advantage: FIRST_MOVER (no comparable approved), SUPERIOR "
            "(better efficacy/safety vs SoC), COMPETITIVE (comparable), INFERIOR (worse data).\n\n"
            "SCORING: profiler_score = management_component + tam_score + patent_score + "
            "competition_pts (cap 0–100).\n\n"
            "Write to agent_profiler_findings via DatabaseWriteTool:\n"
            "  ticker, profile_date=today, profiler_score, management_score=management_raw,\n"
            "  tam_estimate_usd, rnpv_usd, competition_score (TEXT: LOW|MODERATE|HIGH),\n"
            "  competitive_advantage, company_type, patent_cliff_risk, kill_switch (bool),\n"
            "  kill_switch_reason (BANKRUPTCY_IMMINENT|MANAGEMENT_FRAUD|null).\n\n"
            "Return JSON: {\"ticker\": str, \"profiler_score\": int, \"tam_estimate_usd\": float, "
            "\"rnpv_usd\": float, \"competition_score\": str, \"competitive_advantage\": str, "
            "\"company_type\": str, \"patent_cliff_risk\": str, \"kill_switch\": bool, "
            "\"kill_switch_reason\": str|null}"
        ),
        expected_output=(
            "JSON with ticker, profiler_score (0–100), tam_estimate_usd, rnpv_usd, "
            "competition_score (LOW|MODERATE|HIGH), "
            "competitive_advantage (FIRST_MOVER|SUPERIOR|COMPETITIVE|INFERIOR), "
            "company_type (THERAPEUTIC|PLATFORM), patent_cliff_risk (LOW|ELEVATED|CRITICAL), "
            "kill_switch (bool), and kill_switch_reason (null|BANKRUPTCY_IMMINENT|MANAGEMENT_FRAUD)."
        ),
        agent=agent,
    )
