"""
Agent 010 — Partnership: BD&L Intelligence

Spec: docs/AGENT_010_PARTNERSHIP.md v2.3
Crew: Analysis (Crew 2)
Model: llama3.2:3b (Ollama GPU1)

Identifies high-value commercial partnerships: CO_DEVELOPMENT, LICENSING,
CO_PROMOTION, OPTION_TO_ACQUIRE, TECHNOLOGY.
Excludes: MANUFACTURING, CRO_SERVICE, ACADEMIC.

Quality Score (REQ-025, strict formula, range 0–5):
  +2 Tier 1 partner (major pharma >$100B), +1 Tier 2/3 ($10–100B or tech company),
  +2 if upfront_usd > 10% × market_cap_at_deal_usd,
  -1 if direction = IN (company is licensee, not licensor).
  PK: (ticker, partner_name, drug_asset) — drug_asset required on every write.
"""

import os

from crewai import Agent, LLM, Task

from src.tools.clinicaltrials_tool import ClinicalTrialsTool
from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.local_rag_tool import LocalRAGTool
from src.tools.search_tool import DuckDuckGoSearchTool

_GPU1 = os.environ.get("OLLAMA_HOST_GPU1", "http://ollama-gpu1:11435")

_TIER1_PARTNERS = (
    "Pfizer, Roche, J&J, AstraZeneca, Bristol-Myers Squibb (BMY), Merck (MRK), "
    "Eli Lilly (LLY), Novo Nordisk, AbbVie, Sanofi, GSK, Novartis, Amgen, "
    "Gilead, Regeneron, Biogen"
)
_TIER2_PARTNERS = (
    "Astellas, Daiichi Sankyo, UCB, Ipsen, Seagen, Alexion, Shire, Bausch, "
    "Jazz, Halozyme, Incyte"
)
_TIER3_PARTNERS = (
    "Google, Alphabet, Microsoft, NVIDIA, Amazon, Meta, IBM, Apple "
    "(technology/AI platform partners)"
)


def make_partnership_agent() -> Agent:
    llm = LLM(model="ollama/llama3.2:3b", base_url=_GPU1)
    return Agent(
        role="Business Development & Licensing Intelligence Analyst",
        goal=(
            "Identify and score all material partnerships for watchlist companies. "
            "Distinguish high-value co-development and licensing deals from low-signal "
            "CRO/CDMO manufacturing contracts. Score each partnership by partner tier "
            "and deal economics. Flag Tier 1 pharma partnerships as validation signals."
        ),
        backstory=(
            "You are a BD&L specialist who spent 10 years at a major pharma licensing "
            "group. You know that a $200M upfront + $1B milestone deal with Roche "
            "validates a platform more than any Phase 2 result. You can extract deal "
            "terms from 10-K/10-Q footnotes, ClinicalTrials.gov collaborators, and "
            "press releases. You always exclude CDMO manufacturing agreements — they "
            "signal revenue needs, not scientific validation."
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
        max_iter=15,
    )


def make_partnership_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"PARTNERSHIP INTELLIGENCE — Find and score all material partnerships for {ticker}.\n\n"
            f"TIER 1 partners (major pharma >$100B, +2 quality score): {_TIER1_PARTNERS}\n"
            f"TIER 2 partners ($10–100B pharma, +1 quality score): {_TIER2_PARTNERS}\n"
            f"TIER 3 partners (technology/AI companies, +1 quality score): {_TIER3_PARTNERS}\n\n"
            "TASK A — ClinicalTrials.gov Structural Query:\n"
            "  - ClinicalTrialsTool: search by sponsor={ticker} to find collaborators.\n"
            "  - For each trial, extract collaborator names from results.\n"
            "  - Classify each collaborator:\n"
            "    CO_DEVELOPMENT: joint clinical development with pharma partner\n"
            "    LICENSING: named licensor/licensee in trial description\n"
            "    CO_PROMOTION: co-commercialization language\n"
            "    OPTION_TO_ACQUIRE: option agreement language\n"
            "    TECHNOLOGY: platform/technology access agreement\n"
            "    MANUFACTURING: exclude (CMO/CDMO relationship)\n"
            "    CRO_SERVICE: exclude (contract research only)\n"
            "    ACADEMIC: keep as LOW quality\n\n"
            "TASK B — SEC Filing Extraction:\n"
            "  - LocalRAGTool: query sec_filings for '{ticker} licensing agreement milestone "
            "    upfront collaboration deal terms'\n"
            "  - Extract: partner_name, deal_type, upfront_payment_usd, "
            "    total_milestone_usd, royalty_rate, deal_date.\n\n"
            "TASK C — News Validation:\n"
            "  - DatabaseQueryTool: SELECT headline, url FROM news_articles "
            "    WHERE ticker = '{ticker}' AND category = 'partnership' "
            "    AND published_at > datetime('now', '-180 days').\n"
            "  - DuckDuckGoSearchTool: '{ticker} partnership deal collaboration licensing 2024 2025'\n"
            "  - Cross-reference with CT.gov and SEC findings.\n\n"
            "TASK D — Quality Scoring (REQ-025 strict formula):\n"
            "  - DatabaseQueryTool: SELECT market_cap_usd FROM companies "
            "    WHERE ticker = '{ticker}' (for upfront ratio calculation).\n"
            "  For each partnership, compute quality_score:\n"
            "  +2: Tier 1 partner (major pharma >$100B market cap)\n"
            "  +1: Tier 2 OR Tier 3 partner (mid-pharma OR technology company)\n"
            "  +2: upfront_usd > 10% × market_cap_at_deal_usd "
            "(snapshot market cap at deal_date, use current market_cap_usd if deal_date is today)\n"
            "  -1: direction = IN (company is licensee, not licensor — less validating)\n"
            "  quality_score range: 0–5 (REQ-025 canonical formula — no other modifiers).\n\n"
            "TASK E — Direction Classification:\n"
            "  OUT: {ticker} licenses IP out to partner (licensor role, more validating)\n"
            "  IN: {ticker} licenses IP from partner (licensee role)\n"
            "  BOTH: mutual cross-licensing\n\n"
            "TASK F — Write to partnerships table via DatabaseWriteTool:\n"
            "  PK is (ticker, partner_name, drug_asset) — drug_asset is REQUIRED.\n"
            "  For each partnership: ticker, partner_name, drug_asset (drug/platform name),\n"
            "  partnership_type, direction, deal_date, upfront_usd, milestone_usd,\n"
            "  market_cap_at_deal_usd, quality_score, scan_date=today,\n"
            "  status (ACTIVE|TERMINATED|PENDING), confidence (HIGH|MEDIUM|LOW),\n"
            "  source_type (SEC_10K|CLINICALTRIALS|SEC_10K_AND_CLINICALTRIALS|RSS_NEWS).\n\n"
            "Return JSON: {\"ticker\": str, \"partnerships_found\": int, "
            "\"partnerships\": [{\"partner_name\": str, \"drug_asset\": str, "
            "\"partnership_type\": str, "
            "\"quality_score\": int, \"direction\": str, \"status\": str}]}"
        ),
        expected_output=(
            "JSON with ticker, count of partnerships found, and list of partnerships "
            "each with partner_name, drug_asset, partnership_type, quality_score (0–5), "
            "direction (IN|OUT|BOTH), and status (ACTIVE|TERMINATED|PENDING)."
        ),
        agent=agent,
    )
