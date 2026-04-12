"""
Agent 011 — Smart Money: Institutional 13G/13D Tracker

Spec: docs/AGENT_011_SMART_MONEY.md v2.3
Crew: Analysis (Crew 2)
Model: llama3.1:8b (Ollama GPU0)

Tracks institutional conviction via 13G/13D/13D/A filings.
Distinguishes biotech-specialist accumulation (HIGH signal) from
passive index fund accumulation (LOW signal) and activist campaigns.

Fund Classification:
  Biotech Specialists (+3 modifier): Baker Bros, RA Capital, Foresite, Perceptive,
    Orbis, Venrock, RTW, EcoR1, Boxer, Deep Track
  Specialist-Adjacent (+2): OrbiMed
  Generalists (-2): BlackRock, Vanguard, State Street
  Mixed (0): Fidelity, T. Rowe Price
  Thematic (-1): ARK
"""

import os

from crewai import Agent, LLM, Task

from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.local_rag_tool import LocalRAGTool
from src.tools.sec_edgar_tool import SECEdgarFetcherTool

_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")

_BIOTECH_SPECIALISTS = (
    "Baker Bros (+3), RA Capital (+3), Foresite (+3), Perceptive Advisors (+3), "
    "Orbis Investment (+3), Venrock (+3), RTW Investments (+3), EcoR1 (+3), "
    "Boxer Capital (+3), Deep Track Capital (+3), Deerfield Management (+3), "
    "Sofinnova Partners (+3), OrbiMed (+2)"
)
_GENERALIST_FUNDS = (
    "BlackRock (-2), Vanguard (-2), State Street (-2), "
    "Fidelity (0), T. Rowe Price (0), ARK Invest (-1)"
)


def make_smart_money_agent() -> Agent:
    llm = LLM(model="ollama/llama3.1:8b", base_url=_GPU0)
    return Agent(
        role="Institutional Conviction Analyst",
        goal=(
            "Track 13G and 13D institutional filings for all watchlist companies. "
            "Identify when biotech-specialist funds — Baker Bros, RA Capital, Foresite, "
            "Perceptive — take new or increasing positions. Distinguish these high-signal "
            "moves from passive index fund accumulation. Detect activist campaigns via "
            "13D filings and flag analyst conflicts."
        ),
        backstory=(
            "You are a hedge fund analyst specializing in 13F, 13G, and 13D filings. "
            "You know that when Baker Brothers — who backed Regeneron before most people "
            "had heard of it — files a 13G on a small-cap biotech, it deserves serious "
            "attention. You also know that a new BlackRock 13G just means index inclusion "
            "and carries no alpha signal. You read the footnotes for activist language "
            "in 13D filings, and you check whether price has already moved 20%+ since "
            "the filing date."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            LocalRAGTool(),
            SECEdgarFetcherTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=15,
    )


def make_smart_money_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"SMART MONEY ANALYSIS — Analyze 13G/13D institutional filings for {ticker} "
            "(180-day lookback).\n\n"
            f"Biotech Specialist Funds (high signal): {_BIOTECH_SPECIALISTS}\n"
            f"Generalist/Index Funds (low signal): {_GENERALIST_FUNDS}\n\n"
            "STEP 1 — Fetch 13G/13D Filings:\n"
            "  - SECEdgarFetcherTool: fetch SC 13G, SC 13G/A, SC 13D, SC 13D/A "
            "    for {ticker}, last 180 days.\n"
            "  - DatabaseQueryTool: SELECT * FROM sec_filings WHERE ticker = '{ticker}' "
            "    AND filing_type IN ('SC 13G', 'SC 13G/A', 'SC 13D', 'SC 13D/A').\n\n"
            "STEP 2 — Fund Classification:\n"
            "  For each filing, classify the institution:\n"
            "  - BIOTECH_SPECIALIST: Baker Bros, RA Capital, Foresite, Perceptive, "
            "    Orbis, Venrock, RTW, EcoR1, Boxer, Deep Track → +3 modifier\n"
            "  - SPECIALIST_ADJACENT: OrbiMed → +2 modifier\n"
            "  - GENERALIST: BlackRock, Vanguard, State Street → -2 modifier\n"
            "  - MIXED: Fidelity, T. Rowe Price → 0 modifier\n"
            "  - THEMATIC: ARK → -1 modifier\n"
            "  - UNKNOWN: research via LocalRAGTool (agent_memos collection): '{fund_name} biotech strategy'\n\n"
            "STEP 3 — Position Change Detection:\n"
            "  Compare current filing to previous (if any) via DatabaseQueryTool:\n"
            "    SELECT shares, pct_of_class, filing_date FROM smart_money_positions "
            "    WHERE ticker = '{ticker}' AND institution_name = '{institution}' "
            "    ORDER BY filing_date DESC LIMIT 2.\n"
            "  Classify change:\n"
            "    NEW (no prior position): +3\n"
            "    ACCUMULATION (increased ≥ 20%): +2\n"
            "    MAINTENANCE (changed < 20%): 0\n"
            "    REDUCTION (decreased ≥ 20%): -2\n"
            "    EXIT (sold full position): -4\n\n"
            "STEP 4 — Activist Detection (13D only):\n"
            "  - 13D = activist intent (vs. 13G = passive).\n"
            "  - Read filing text for: 'board representation', 'strategic alternatives', "
            "    'shareholder value', 'merger', 'sale of company'.\n"
            "  - Activist 13D with BIOTECH_SPECIALIST = highest conviction signal.\n\n"
            "STEP 5 — Conviction Scoring (1–10):\n"
            "  Base score by form type (per spec Section 7 Step 4):\n"
            "    SC 13D (activist): 8\n"
            "    SC 13G by BIOTECH_SPECIALIST fund: 7\n"
            "    SC 13G by generalist fund: 5\n"
            "    SC 13G/A (amendment, no material change): 4\n"
            "  Add fund_classifier_modifier (Step 2: -2 to +3).\n"
            "  Add position_change_modifier (Step 3: -4 to +3).\n"
            "  Add position_size_modifier:\n"
            "    position_as_pct_of_market_cap = (shares × price) / market_cap_usd\n"
            "    >15%: +2 | 10–15%: +1 | 5–10%: 0 | <5%: -1\n"
            "    DatabaseQueryTool: SELECT market_cap_usd FROM companies WHERE ticker = '{ticker}'.\n"
            "  Add 13d_intent_modifier (SC 13D filings only):\n"
            "    'acquisition', 'merger', 'tender offer' in purpose text: +2\n"
            "    'undervalued', 'capital allocation': +1\n"
            "    Standard 'investment purposes' boilerplate: 0\n"
            "  Final conviction_score = base + sum(modifiers), clamped to 1–10.\n\n"
            "STEP 6 — Aggregate Signal:\n"
            "  SPECIALIST_CLUSTER: 2+ BIOTECH_SPECIALIST funds with NEW/ACCUMULATION\n"
            "  ACTIVIST_PLUS_SPECIALIST: 13D + BIOTECH_SPECIALIST fund both present\n"
            "  SPECIALIST_NEW: 1 BIOTECH_SPECIALIST NEW position\n"
            "  INDEX_ONLY: only GENERALIST funds\n"
            "  REDUCTION: dominant position changes are REDUCTION/EXIT\n"
            "  EXIT: major specialist exiting\n\n"
            "STEP 7 — Analyst Conflict Check:\n"
            "  - DatabaseQueryTool: SELECT headline FROM news_articles "
            "    WHERE ticker = '{ticker}' AND category = 'analyst' "
            "    AND headline LIKE '%downgrade%' "
            "    AND published_at > datetime('now', '-7 days').\n"
            "  - If analyst downgrade within 7 days: conflicting_signal = True, "
            "    reduce conviction_score by 1.\n\n"
            "STEP 8 — Already-Priced-In Check (REQ-024):\n"
            "  - DatabaseQueryTool: SELECT price_current, week52_low "
            "    FROM companies WHERE ticker = '{ticker}'.\n"
            "  - Calculate price drift since earliest new specialist filing.\n"
            "  - already_priced_in = True if price drift > 20%.\n\n"
            "STEP 9 — Write granular positions to smart_money_positions via DatabaseWriteTool:\n"
            f"  For each institution found: ticker={ticker}, institution_name, filing_date,\n"
            "  filing_type (SC_13G|SC_13G_A|SC_13D|SC_13D_A), shares, pct_of_class,\n"
            "  is_specialist (True for Baker Bros/RA Capital/Foresite/Perceptive/Orbis/\n"
            "  Venrock/RTW/EcoR1/Boxer/Deep Track/OrbiMed, False otherwise).\n"
            "  PK: (ticker, institution_name, filing_date).\n\n"
            "STEP 10 — Write summary to agent_smart_money_findings via DatabaseWriteTool:\n"
            f"  ticker={ticker}, scan_date=today, signal, conviction_score,\n"
            "  top_institution, top_institution_pct, price_drift_pct,\n"
            "  conflicting_signal (bool), already_priced_in (bool), analysis_summary,\n"
            "  uploaded_to_rag=False (will be set True by sync_local_rag.py on Sunday sync).\n\n"
            "Return JSON: {\"ticker\": str, \"signal\": str, \"conviction_score\": int, "
            "\"top_institution\": str, \"top_institution_pct\": float, "
            "\"price_drift_pct\": float, \"conflicting_signal\": bool, "
            "\"already_priced_in\": bool, \"top_holders\": [{\"fund\": str, "
            "\"type\": str, \"change\": str, \"pct_outstanding\": float}]}"
        ),
        expected_output=(
            "JSON with ticker, signal (SPECIALIST_CLUSTER|ACTIVIST_PLUS_SPECIALIST|"
            "SPECIALIST_NEW|INDEX_ONLY|REDUCTION|EXIT), conviction_score (1–10), "
            "top_institution, top_institution_pct, price_drift_pct, "
            "conflicting_signal (bool), already_priced_in (bool), and top_holders list."
        ),
        agent=agent,
    )
