"""
Agent 009 — Volatility: Cash-Secured Put Specialist

Spec: docs/AGENT_009_VOLATILITY.md v2.2
Crew: Strategy (Crew 3)
Model: llama3.2:3b (Ollama GPU1)

Executes the CSP strategy with surgical precision:
  - Harvests elevated IV from catalysts OUTSIDE the expiration window
  - Rejects binary event exposure
  - Never sells CSPs within 45 DTE of a PDUFA or Phase 3 data readout

6-Step Protocol:
  1. Floor Price Calculation
  2. Catalyst Safety Check
  3. Pipeline Diversity Check (≥2 active assets)
  4. Valley of Death Shield (runway check)
  5. Liquidity Check (OI≥500, spread≤10%)
  6. Strike & Return Calculation (annualized ≥18% preferred)
"""

import os

from crewai import Agent, LLM, Task

from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.local_rag_tool import LocalRAGTool
from src.tools.options_tool import OptionsChainTool

_GPU1 = os.environ.get("OLLAMA_HOST_GPU1", "http://ollama-gpu1:11435")


def make_volatility_agent() -> Agent:
    llm = LLM(model="ollama/llama3.2:3b", base_url=_GPU1)
    return Agent(
        role="Cash-Secured Put Specialist",
        goal=(
            "Select optimal CSP strikes below the floor price with ≥18% annualized return "
            "and zero binary event exposure. Reject any CSP where a PDUFA date, Phase 3 "
            "data readout, or AdComm falls within the expiration window. "
            "Never sell a CSP on a company with runway < 12 months."
        ),
        backstory=(
            "You are a systematic options trader who has run CSP strategies on biotech "
            "stocks for 8 years. Your one rule: never let a binary event land inside "
            "your expiration window. You calculate the floor price as the MAX of "
            "cash-per-share, book value per share, and 52-week low × 0.90. You check "
            "open interest and bid-ask spreads obsessively — you never trade illiquid "
            "options. You target 18%+ annualized returns and reject anything below 12%."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            LocalRAGTool(),
            OptionsChainTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=10,
    )


def make_volatility_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"CSP ANALYSIS — Evaluate {ticker} for Cash-Secured Put opportunity.\n\n"
            "STEP 1 — Floor Price Calculation:\n"
            "  - DatabaseQueryTool: SELECT cash_per_share, book_value_per_share, "
            "    week52_low, market_cap_usd, runway_months, "
            "    is_active FROM companies WHERE ticker = '{ticker}'.\n"
            "  - calculated_floor = MAX(cash_per_share, book_value_per_share, "
            "    week52_low × 0.90).\n"
            "  - Extended floor (if IV > 80% AND runway > 18 months AND active assets ≥ 3):\n"
            "    extended_floor = calculated_floor × 1.25.\n"
            "  - Use extended_floor if conditions met, otherwise calculated_floor.\n\n"
            "STEP 2 — Catalyst Safety Check (CRITICAL — reject if fails):\n"
            "  - DatabaseQueryTool: SELECT event_type, event_date, market_impact_score "
            "    FROM catalysts WHERE ticker = '{ticker}' "
            "    AND event_type IN ('PDUFA_DATE', 'ADCOMM_DATE', 'DATA_READOUT', "
            "    'INTERIM_ANALYSIS') ORDER BY event_date ASC.\n"
            "  - Find next expiration date: today + 30 to 45 days (target DTE window).\n"
            "  - REJECT if ANY high-impact catalyst (market_impact_score ≥ 6) falls "
            "    between today and expiration date + 3 days buffer.\n"
            "  - CAUTION if market_impact_score = 4–5 falls in window.\n"
            "  - IV Leakage Warning: if IV_30d > 60% but no catalyst identified, "
            "    proceed at 50% position size.\n\n"
            "STEP 3 — Pipeline Diversity Check:\n"
            "  - DatabaseQueryTool: SELECT COUNT(*) FROM trial_pipeline tp "
            "    JOIN studies s ON tp.nct_id = s.nct_id "
            "    WHERE tp.ticker = '{ticker}' AND s.status IN "
            "    ('RECRUITING', 'ACTIVE_NOT_RECRUITING') AND s.phase IN "
            "    ('PHASE1', 'PHASE2', 'PHASE3').\n"
            "  - REJECT if active Phase 2/3 trial count < 2 (single-asset binary risk).\n\n"
            "STEP 4 — Valley of Death Shield:\n"
            "  Using runway_months from Step 1:\n"
            "  - runway > 18 months: PASS\n"
            "  - 12–18 months AND has Tier 1 partner (check partnerships table): PASS\n"
            "  - 12–18 months AND no Tier 1 partner: CAUTION (reduce size 50%)\n"
            "  - < 12 months AND no partner: REJECT\n"
            "  - < 6 months: ALWAYS REJECT (imminent dilution risk)\n"
            "  - LocalRAGTool: query sec_filings for '{ticker} burn rate cash flow 10-Q' "
            "    to detect burn rate acceleration (>20% QoQ increase = DILUTION_ACCELERATION_FLAG).\n\n"
            "STEP 5 — Liquidity Check:\n"
            "  - OptionsChainTool: fetch options chain for {ticker}, max_dte=45.\n"
            "  - Filter puts with strike ≤ floor price.\n"
            "  - REQUIRE: open_interest ≥ 500 AND bid_ask_spread_pct ≤ 0.10.\n"
            "  - REJECT if no liquid puts exist below floor price.\n\n"
            "STEP 6 — Strike & Return Calculation:\n"
            "  - From filtered options, select best put:\n"
            "    Target: strike closest to (floor_price × 0.95) with OI ≥ 500.\n"
            "  - Calculate annualized return:\n"
            "    premium_pct = mid_price / strike\n"
            "    annualized_return = premium_pct × (365 / dte)\n"
            "  - APPROVE if annualized_return ≥ 0.18 (18%)\n"
            "  - ACCEPTABLE if 0.12 ≤ annualized_return < 0.18 (add LOW_YIELD_WARNING)\n"
            "  - REJECT if annualized_return < 0.12\n\n"
            "STEP 7 — Write to agent_volatility_findings via DatabaseWriteTool:\n"
            f"  ticker={ticker}, scan_date=today, status (APPROVED|REJECTED|CAUTION),\n"
            "  calculated_floor, selected_strike, selected_expiration (YYYY-MM-DD), dte,\n"
            "  premium_mid, absolute_return_pct, annualized_return_pct,\n"
            "  iv_source, iv_leakage_warning (bool),\n"
            "  next_catalyst_date, next_catalyst_within_window (bool),\n"
            "  risk_warnings (JSON array), rejection_reason (if any).\n\n"
            "Return JSON: {\"ticker\": str, \"status\": str, \"calculated_floor\": float, "
            "\"selected_strike\": float|null, \"selected_expiration\": str|null, "
            "\"dte\": int|null, \"annualized_return_pct\": float|null, "
            "\"rejection_reason\": str|null, \"warnings\": [str]}"
        ),
        expected_output=(
            "JSON with ticker, status (APPROVED|REJECTED|CAUTION), calculated_floor, "
            "and if APPROVED: selected_strike, selected_expiration (YYYY-MM-DD), dte, "
            "annualized_return_pct. If REJECTED: rejection_reason. "
            "warnings list for CAUTION conditions."
        ),
        agent=agent,
    )
