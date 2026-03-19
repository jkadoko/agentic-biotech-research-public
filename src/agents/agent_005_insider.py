"""
Agent 005 — Insider: Form 4 Activity Tracker

Spec: docs/AGENT_005_INSIDER.md v2.2
Crew: Analysis (Crew 2)
Model: llama3.1:8b (Ollama GPU0)

Detects statistically meaningful insider buying clusters from SEC Form 4 filings.
Code P (open-market purchase) is the ONLY true buy signal.
Excludes: Code F (tax withholding), Code D (derivative), Code A (award/grant).

Conviction Score (1–10):
  Base 5 + modifiers:
    CEO/CFO +2, CMO +2, >$500k purchase +2, >$100k +1,
    cluster (3+ insiders 30 days) +2, near 52wk low +1,
    held >30 days confirmed +1, 10b5-1 plan -3
"""

import os

from crewai import Agent, LLM, Task

from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.search_tool import DuckDuckGoSearchTool
from src.tools.sec_edgar_tool import SECEdgarFetcherTool

_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")


def make_insider_agent() -> Agent:
    llm = LLM(model="ollama/llama3.1:8b", base_url=_GPU0)
    return Agent(
        role="Insider Activity Specialist",
        goal=(
            "Identify statistically meaningful insider buying clusters from SEC Form 4 "
            "filings. Separate true conviction signals (Code P open-market buys by "
            "executives near 52-week lows) from noise (RSU vesting, 10b5-1 plan sales, "
            "tax withholding). Produce a conviction score (1–10) and signal classification."
        ),
        backstory=(
            "You are a forensic analyst who has studied 10 years of insider transaction "
            "data. You know that Code P transactions — especially by CEOs, CMOs, and "
            "directors buying $100k+ out of pocket near 52-week lows — are among the "
            "strongest alpha signals in biotech. You also know that Form F transactions "
            "are RSU tax withholding (bearish noise) and that 10b5-1 plan sales are "
            "pre-scheduled and carry no conviction signal. You check the footnotes."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            DuckDuckGoSearchTool(),
            SECEdgarFetcherTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=15,
    )


def make_insider_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"INSIDER ANALYSIS — Analyze Form 4 transactions for {ticker} (90-day lookback).\n\n"
            "STEP 1 — Fetch Form 4 Filings:\n"
            "  - SECEdgarFetcherTool: fetch Form 4 filings for {ticker}, last 90 days.\n"
            "  - Also query local cache: DatabaseQueryTool on sec_filings table "
            "    WHERE filing_type = 'Form 4' AND ticker = '{ticker}'.\n\n"
            "STEP 2 — Transaction Code Filter (CRITICAL):\n"
            "  INCLUDE (true buy signals):\n"
            "    Code P = open-market purchase (HIGH signal — always include)\n"
            "  EXCLUDE (noise):\n"
            "    Code F = tax withholding on RSU vesting (exclude)\n"
            "    Code D = derivative exercise (exclude unless accompanied by Code P)\n"
            "    Code A = award/grant (exclude)\n"
            "    Code M = option exercise (exclude unless Code S follows within 30 days)\n"
            "  SELL signals (track separately):\n"
            "    Code S = open-market sale\n"
            "    Note: Code S is only bearish if NOT preceded by 10b5-1 plan registration.\n\n"
            "STEP 3 — 10b5-1 Plan Detection:\n"
            "  - For every Code S transaction, check Form 4 footnotes for '10b5-1' language.\n"
            "  - 10b5-1 sales are pre-scheduled and carry NO conviction signal.\n"
            "  - Non-10b5-1 sales are discretionary and bearish.\n\n"
            "STEP 4 — Cluster Detection:\n"
            "  - A CLUSTER = 3 or more unique insiders with Code P within a 30-day window.\n"
            "  - A MINI_CLUSTER = 2 unique insiders with Code P within 30 days.\n"
            "  - Single insider = SINGLE_BUY (or STRONG_BUY if amount > $500k).\n\n"
            "STEP 5 — Conviction Scoring (base 5, range 1–10):\n"
            "  +2: Title is CEO or CFO\n"
            "  +2: Title is CMO or Chief Medical Officer\n"
            "  +2: Purchase amount > $500,000\n"
            "  +1: Purchase amount $100,000–$499,999\n"
            "  +2: CLUSTER detected (3+ insiders in 30 days)\n"
            "  +1: Purchase price within 10% of 52-week low\n"
            "  +1: Confirmed holding period > 30 days (no offsetting sale)\n"
            "  -3: 10b5-1 plan attached to any transaction in window\n"
            "  Final: cap conviction at 1 (min) and 10 (max).\n\n"
            "STEP 6 — Signal Classification:\n"
            "  CLUSTER_BUY: 3+ unique insiders Code P, conviction ≥ 7\n"
            "  STRONG_BUY: single insider Code P, amount > $500k, conviction ≥ 6\n"
            "  SINGLE_BUY: single insider Code P, conviction 4–5\n"
            "  NEUTRAL: no Code P in last 90 days\n"
            "  SINGLE_DISCRETIONARY_SELL: Code S, non-10b5-1, single insider\n"
            "  CLUSTER_DISCRETIONARY_SELL: 3+ Code S, non-10b5-1, within 30 days\n\n"
            "STEP 7 — Write to agent_insider_findings via DatabaseWriteTool:\n"
            f"  ticker={ticker}, signal, conviction_score, cluster_detected (bool),\n"
            "  largest_purchase_usd, insider_count_90d, analysis_date.\n\n"
            "Return JSON: {\"ticker\": str, \"signal\": str, \"conviction_score\": int, "
            "\"cluster_detected\": bool, \"insider_count_90d\": int, "
            "\"largest_purchase_usd\": float, \"largest_buyer_title\": str}"
        ),
        expected_output=(
            "JSON with ticker, signal (CLUSTER_BUY|STRONG_BUY|SINGLE_BUY|NEUTRAL|"
            "SINGLE_DISCRETIONARY_SELL|CLUSTER_DISCRETIONARY_SELL), conviction_score (1–10), "
            "cluster_detected (bool), insider_count_90d, largest_purchase_usd, "
            "and largest_buyer_title."
        ),
        agent=agent,
    )
