"""
Agent 008 — Oracle: Catalyst Hunter

Spec: docs/AGENT_008_ORACLE.md v2.3
Crew: Data Collection (Crew 1)
Model: llama3.1:8b (Ollama GPU0)

Builds a prioritized catalyst calendar: PDUFA dates, conference presentations,
Phase 3 data readouts. Runs after Detective (needs resolved tickers).
"""

import os

from crewai import Agent, LLM, Task

from src.tools.clinicaltrials_tool import ClinicalTrialsTool
from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.search_tool import DuckDuckGoSearchTool

_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")

# Market impact scores per event type (spec Section 4)
_IMPACT_SCORES = {
    "PDUFA_DATE": 9,
    "ADCOMM": 7,
    "DATA_READOUT_PHASE3": 8,
    "DATA_READOUT_PHASE2": 5,
    "CONFERENCE_ORAL_PHASE3": 6,
    "INTERIM_ANALYSIS": 6,
    "CONFERENCE_POSTER": 3,
}

# 15-conference calendar (spec Section 3)
_CONFERENCE_CALENDAR = (
    "JPM Healthcare (Jan), AACR (Apr), AAN (Apr), DDW (May), "
    "ASCO (May/Jun), EHA (Jun), ADA (Jun), ESMO (Sep), "
    "AASLD (Nov), AHA (Nov), ASH (Dec), "
    "BIO International (Jun), Cowen Healthcare (Mar), "
    "Needham (Jan), SVB Leerink (Feb)"
)


def make_oracle_agent() -> Agent:
    llm = LLM(model="ollama/llama3.1:8b", base_url=_GPU0)
    return Agent(
        role="Catalyst Hunter",
        goal=(
            "Build and maintain a prioritized catalyst calendar for every watchlist "
            "company: PDUFA dates (impact=9), Phase 3 data readouts (impact=8), "
            "AdComm meetings (impact=7), and conference presentations. "
            "Never confuse a conference date with a PDUFA date."
        ),
        backstory=(
            "You are an FDA regulatory affairs expert who has tracked hundreds of NDA "
            "and BLA submissions. You know that PDUFA clocks start 60 days after filing "
            "acceptance — not at submission — and that Standard Reviews take 10–12 months "
            "while Priority Reviews take 6 months. You cross-reference company IR pages, "
            "CT.gov status, and SEC filings to triangulate catalyst dates, and you assign "
            "market impact scores so the Strategist knows what matters most."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            DuckDuckGoSearchTool(),
            ClinicalTrialsTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=20,
    )


def make_pdufa_hunt_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"PDUFA HUNT — Find FDA regulatory catalysts for {ticker}.\n\n"
            "STEP 1 — Identify NDA/BLA Candidates:\n"
            "  - DatabaseQueryTool: SELECT nct_id, brief_title, phase, status, "
            "    primary_completion_date FROM studies WHERE ticker = '{ticker}' "
            "    AND status IN ('ACTIVE_NOT_RECRUITING', 'COMPLETED') AND phase = 'PHASE3'.\n"
            "  - ACTIVE_NOT_RECRUITING Phase 3 trials = primary NDA candidates.\n\n"
            "STEP 2 — Search for PDUFA Date:\n"
            "  - DuckDuckGoSearchTool: '{ticker} NDA BLA FDA submission PDUFA date'\n"
            "  - DuckDuckGoSearchTool: '{ticker} FDA priority review standard review'\n"
            "  - Extract: submission_date, filing_acceptance_date (Day 60), "
            "    review_type (STANDARD=10mo, PRIORITY=6mo), pdufa_date.\n\n"
            "STEP 3 — AdComm Detection:\n"
            "  - DuckDuckGoSearchTool: '{ticker} FDA advisory committee AdComm meeting date'\n"
            "  - AdComm typically 2–3 months before PDUFA. Assign impact=7.\n\n"
            "STEP 4 — Write to catalysts table via DatabaseWriteTool:\n"
            "  - For each catalyst found:\n"
            f"    ticker={ticker}, event_type=PDUFA_DATE|ADCOMM, event_date, "
            "    confidence (HIGH=0.9 if from SEC filing, MEDIUM=0.7 if from news), "
            f"    market_impact_score=9 for PDUFA / 7 for AdComm.\n\n"
            "Return JSON: {\"ticker\": str, \"catalysts\": ["
            "{\"event_type\": str, \"event_date\": str, \"confidence\": str, "
            "\"market_impact_score\": int}]}"
        ),
        expected_output=(
            "JSON with ticker and list of PDUFA/AdComm catalysts found, each with "
            "event_type, event_date (YYYY-MM-DD or YYYY-QX), confidence, and "
            "market_impact_score."
        ),
        agent=agent,
    )


def make_data_readout_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"DATA READOUT SCANNER — Find Phase 2/3 topline readout dates for {ticker}.\n\n"
            "STEP 1 — Query pipeline:\n"
            "  - DatabaseQueryTool: SELECT nct_id, brief_title, phase, "
            "    primary_completion_date FROM studies WHERE ticker = '{ticker}' "
            "    AND status = 'ACTIVE_RECRUITING' AND phase IN ('PHASE2', 'PHASE3').\n\n"
            "STEP 2 — Search for company guidance:\n"
            "  - DuckDuckGoSearchTool: '{ticker} Phase 3 topline data readout 2025 2026'\n"
            "  - DuckDuckGoSearchTool: '{ticker} interim analysis results expected'\n"
            "  - Normalize vague dates: 'H1 2025' → '2025-Q2', 'mid-2025' → '2025-Q2'.\n\n"
            "STEP 3 — Score readout impact:\n"
            "  - Phase 3 data readout: market_impact_score = 8.\n"
            "  - Phase 2 data readout: market_impact_score = 5.\n"
            "  - Interim analysis: market_impact_score = 6.\n\n"
            "STEP 4 — Write to catalysts table via DatabaseWriteTool.\n\n"
            "STEP 5 — Check for conference presentations:\n"
            f"  Known conferences: {_CONFERENCE_CALENDAR}\n"
            "  - DuckDuckGoSearchTool: '{ticker} ASCO ASH ESMO abstract 2025'\n"
            "  - Conference oral Phase 3: impact=6. Poster: impact=3.\n"
            "  - Write conference catalysts to catalysts table.\n\n"
            "STEP 6 — Deduplication:\n"
            "  - If same nct_id + event_type already exists in catalysts table, "
            "    update only if new confidence is higher.\n\n"
            "Return JSON: {\"ticker\": str, \"readouts\": [{"
            "\"event_type\": str, \"event_date\": str, "
            "\"nct_id\": str, \"market_impact_score\": int}]}"
        ),
        expected_output=(
            "JSON with ticker and list of data readouts and conference presentations, "
            "each with event_type, event_date, nct_id, and market_impact_score."
        ),
        agent=agent,
    )


def make_rss_catalyst_task(agent: Agent) -> Task:
    return Task(
        description=(
            "RSS CATALYST SCAN — Extract catalysts from recent FDA and conference news.\n\n"
            "STEP 1: Query recent news articles (last 48 hours, categories fda + conference):\n"
            "  - DatabaseQueryTool: SELECT headline, ticker, url, category FROM news_articles "
            "    WHERE category IN ('fda', 'conference') "
            "    AND published_at > datetime('now', '-48 hours').\n\n"
            "STEP 2: For each article, extract catalyst signals:\n"
            "  - FDA articles: look for PDUFA date, approval, complete response letter (CRL), "
            "    AdComm announcement, FDA acceptance.\n"
            "  - Conference articles: look for abstract acceptance, oral presentation, "
            "    poster presentation, late-breaking trial.\n\n"
            "STEP 3: Verify ticker is in watchlist:\n"
            "  - DatabaseQueryTool: SELECT ticker FROM companies WHERE ticker = '{ticker}'.\n"
            "  - Skip if ticker not in watchlist.\n\n"
            "STEP 4: Write new catalysts to catalysts table via DatabaseWriteTool.\n"
            "  - confidence = MEDIUM (0.6) for news-derived catalysts (no official source).\n\n"
            "Return JSON: {\"catalysts_extracted\": int, "
            "\"tickers_updated\": [str]}"
        ),
        expected_output=(
            "JSON with count of catalysts extracted from RSS and list of tickers updated."
        ),
        agent=agent,
    )
