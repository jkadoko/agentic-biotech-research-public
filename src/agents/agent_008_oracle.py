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
from src.tools.local_rag_tool import LocalRAGTool
from src.tools.search_tool import DuckDuckGoSearchTool
from src.tools.sec_edgar_tool import SECEdgarFetcherTool

_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")

# Market impact scores per event type (spec Section 4 — use canonical SCHEMA.md enum values)
_IMPACT_SCORES = {
    "PDUFA_DATE": 9,
    "ADCOMM_DATE": 7,
    "DATA_READOUT": 8,   # Phase 3; Phase 2 readouts assigned 5 in task logic
    "CONFERENCE": 6,
    "INTERIM_ANALYSIS": 6,
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
            LocalRAGTool(),
            DuckDuckGoSearchTool(),
            ClinicalTrialsTool(),
            SECEdgarFetcherTool(),
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
            "  - DatabaseQueryTool: SELECT s.nct_id, s.title, s.phase, s.status, "
            "    s.primary_completion_date FROM studies s "
            "    JOIN trial_pipeline tp ON s.nct_id = tp.nct_id "
            "    WHERE tp.ticker = '{ticker}' "
            "    AND s.status = 'ACTIVE_NOT_RECRUITING' AND s.phase = 'PHASE3'.\n"
            "  - ACTIVE_NOT_RECRUITING Phase 3 trials = primary NDA candidates. "
            "    Do NOT include COMPLETED trials (REQ-006: active-only evaluation).\n"
            "  - LocalRAGTool: query sec_filings for '{ticker} NDA BLA submission PDUFA 8-K'\n\n"
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
            f"    ticker={ticker}, event_type=PDUFA_DATE|ADCOMM_DATE, event_date (YYYY-MM-DD), "
            "    date_confidence=HIGH (if from SEC 8-K) | MEDIUM-HIGH (if from company IR) | MEDIUM (if from news), "
            f"    market_impact_score=9 for PDUFA / 7 for AdComm, scan_date=today.\n"
            "    date_confidence must be one of: HIGH | MEDIUM-HIGH | MEDIUM | LOW (string only, no floats).\n\n"
            "Return JSON: {\"ticker\": str, \"catalysts\": ["
            "{\"event_type\": str, \"event_date\": str, \"date_confidence\": str, "
            "\"market_impact_score\": int}]}"
        ),
        expected_output=(
            "JSON with ticker and list of PDUFA/AdComm catalysts found, each with "
            "event_type, event_date (YYYY-MM-DD), date_confidence (HIGH|MEDIUM-HIGH|MEDIUM|LOW), "
            "and market_impact_score."
        ),
        agent=agent,
    )


def make_data_readout_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"DATA READOUT SCANNER — Find Phase 2/3 topline readout dates for {ticker}.\n\n"
            "STEP 1 — Query pipeline:\n"
            "  - DatabaseQueryTool: SELECT s.nct_id, s.title, s.phase, "
            "    s.primary_completion_date FROM studies s "
            "    JOIN trial_pipeline tp ON s.nct_id = tp.nct_id "
            "    WHERE tp.ticker = '{ticker}' "
            "    AND s.status IN ('ACTIVE_NOT_RECRUITING', 'COMPLETED') "
            "    AND s.phase IN ('PHASE2', 'PHASE3').\n\n"
            "STEP 2 — Search for company guidance:\n"
            "  - DuckDuckGoSearchTool: '{ticker} Phase 3 topline data readout 2025 2026'\n"
            "  - DuckDuckGoSearchTool: '{ticker} interim analysis results expected'\n"
            "  - Normalize vague dates to YYYY-MM-DD: "
            "'H1 2026' → '2026-06-30', 'H2 2026' → '2026-12-31', "
            "'Q1 2026' → '2026-03-31', 'mid-2026' → '2026-06-30'.\n\n"
            "STEP 3 — Score readout impact:\n"
            "  - Phase 3 data readout: market_impact_score = 8.\n"
            "  - Phase 2 data readout: market_impact_score = 5.\n"
            "  - Interim analysis: market_impact_score = 6.\n\n"
            "STEP 4 — Write to catalysts table via DatabaseWriteTool "
            "(event_type=DATA_READOUT|CONFERENCE|INTERIM_ANALYSIS, "
            "date_confidence=HIGH|MEDIUM-HIGH|MEDIUM|LOW (string enum only, no floats), "
            "event_date in YYYY-MM-DD).\n\n"
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
            "STEP 1: Query recent news articles (last 180 days, categories fda + conference):\n"
            "  - DatabaseQueryTool: SELECT headline, ticker, url, category FROM news_articles "
            "    WHERE category IN ('fda', 'conference') "
            "    AND published_at > datetime('now', '-180 days').\n\n"
            "STEP 2: For each article, extract catalyst signals:\n"
            "  - FDA articles: look for PDUFA date, approval, complete response letter (CRL), "
            "    AdComm announcement, FDA acceptance.\n"
            "  - Conference articles: look for abstract acceptance, oral presentation, "
            "    poster presentation, late-breaking trial.\n\n"
            "STEP 3: Verify ticker is in watchlist:\n"
            "  - DatabaseQueryTool: SELECT ticker FROM companies WHERE ticker = '{ticker}'.\n"
            "  - Skip if ticker not in watchlist.\n\n"
            "STEP 4: Write new catalysts to catalysts table via DatabaseWriteTool.\n"
            "  - date_confidence = 'MEDIUM' for news-derived catalysts (no official source).\n"
            "  - event_type must be one of: PDUFA_DATE | ADCOMM_DATE | DATA_READOUT | "
            "    CONFERENCE | INTERIM_ANALYSIS.\n\n"
            "Return JSON: {\"catalysts_extracted\": int, "
            "\"tickers_updated\": [str]}"
        ),
        expected_output=(
            "JSON with count of catalysts extracted from RSS and list of tickers updated."
        ),
        agent=agent,
    )
