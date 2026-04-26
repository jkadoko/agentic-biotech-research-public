"""
Agent 006 — Strategist: Chief Investment Officer

Spec: docs/AGENT_006_STRATEGIST.md v3.3
Crew: Strategy (Crew 3)
Model: deepseek-r1:7b (Ollama GPU1) — reasoning model required

Synthesizes all 8 upstream agent outputs into an investment memo with
Biotech Alpha Score (BAS, 0–100) and Kelly Criterion position sizing.

BAS Formula:
  BAS = (Profiler×0.20 + Science×0.25 + Insider_Norm×0.15
       + SmartMoney_Norm×0.15 + Partnership_Bonus
       + FinHealth×0.15 + Catalyst_Timing×0.10)

5 Strategy Checklist (independent):
  A. CSP (REQ-011) — Agent 009 APPROVED, no catalyst in window, ≥2 assets, science≥30
  B. Deep Value (REQ-012) — cash>market_cap OR runway>18m, science≥40
  C. Moonshot (REQ-014) — THERAPEUTIC only, science≥80, orphan/TAM>$1B, cap<$500M
  D. Smart Money Follow (REQ-024) — conviction≥7, insider confirmed, drift≤20%
  E. Commercial Viability (REQ-027) — no patent cliff, major partner, TAM>$500M
"""

import os

from crewai import Agent, LLM, Task

from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.local_rag_tool import LocalRAGTool
from src.tools.ollama_tool import OllamaLLMTool

_GPU1 = os.environ.get("OLLAMA_HOST_GPU1", "http://ollama-gpu1:11435")


def make_strategist_agent() -> Agent:
    llm = LLM(model="ollama/deepseek-r1:7b", base_url=_GPU1)
    return Agent(
        role="Chief Investment Officer",
        goal=(
            "Synthesize inputs from all 8 upstream agents into a rigorous investment memo. "
            "Calculate the Biotech Alpha Score (BAS, 0–100), determine which investment "
            "strategies are eligible, select the primary recommendation, and size positions "
            "using Kelly Criterion. Trigger HARD_SELL immediately on kill switches — "
            "no exceptions."
        ),
        backstory=(
            "You are a seasoned biotech CIO managing three portfolio mandates: Income Fund "
            "(CSP premium income), Growth Fund (Moonshot asymmetric upside), and Value Fund "
            "(Deep Value). You have seen every playbook — orphan drug designation doesn't "
            "matter if management is burning cash and selling stock. You think in "
            "risk-adjusted expected value, not hope. You use Kelly Criterion for position "
            "sizing but never allocate more than 5% of portfolio to a single name. "
            "Your memo must be actionable, not academic."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            LocalRAGTool(),
            OllamaLLMTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=25,
    )


def make_strategist_task(agent: Agent, ticker: str) -> Task:
    return Task(
        description=(
            f"INVESTMENT MEMO — Generate full investment recommendation for {ticker}.\n\n"
            "PRE-CHECK — Kill Switches (run first, override everything):\n"
            "  - DatabaseQueryTool: SELECT kill_switch, kill_switch_reason FROM agent_profiler_findings "
            "    WHERE ticker = '{ticker}' ORDER BY profile_date DESC LIMIT 1.\n"
            "  - DatabaseQueryTool: SELECT verdict FROM agent_scientific_audits "
            "    WHERE ticker = '{ticker}' ORDER BY audit_date DESC LIMIT 1.\n"
            "  - DatabaseQueryTool: SELECT signal FROM agent_smart_money_findings "
            "    WHERE ticker = '{ticker}' ORDER BY scan_date DESC LIMIT 1.\n"
            "  - DatabaseQueryTool: SELECT signal, conviction_score FROM agent_insider_findings "
            "    WHERE ticker = '{ticker}' ORDER BY scan_date DESC LIMIT 1.\n"
            "  HARD_SELL triggers (stop here, write memo with HARD_SELL):\n"
            "    kill_switch = 1 AND kill_switch_reason IN ('BANKRUPTCY_IMMINENT', 'MANAGEMENT_FRAUD')\n"
            "    OR verdict = 'FRAUD_RISK'\n"
            "  STRONG_SELL trigger:\n"
            "    smart_money_conviction < 3 AND insider_signal = 'CLUSTER_DISCRETIONARY_SELL'\n\n"
            "STEP 1 — Collect All Agent Outputs:\n"
            "  - Profiler: SELECT profiler_score, tam_estimate_usd, rnpv_usd, competition_score, "
            "    competitive_advantage, company_type, patent_cliff_risk FROM agent_profiler_findings "
            "    WHERE ticker = '{ticker}' ORDER BY profile_date DESC LIMIT 1.\n"
            "  - Peer Reviewer: SELECT validity_score, verdict, competitive_advantage, "
            "    endpoint_switching_detected FROM agent_scientific_audits "
            "    WHERE ticker = '{ticker}' ORDER BY audit_date DESC LIMIT 1.\n"
            "  - Insider: SELECT signal, conviction_score FROM "
            "    agent_insider_findings WHERE ticker = '{ticker}' ORDER BY scan_date DESC LIMIT 1.\n"
            "  - Oracle: SELECT event_type, event_date, market_impact_score, date_confidence "
            "    FROM catalysts WHERE ticker = '{ticker}' "
            "    AND event_date > date('now') ORDER BY event_date ASC LIMIT 3.\n"
            "  - Volatility: SELECT status, calculated_floor, selected_strike, "
            "    annualized_return_pct FROM agent_volatility_findings "
            "    WHERE ticker = '{ticker}' ORDER BY scan_date DESC LIMIT 1.\n"
            "  - Partnership: SELECT partner_name, partnership_type, direction, "
            "    quality_score FROM partnerships WHERE ticker = '{ticker}' "
            "    AND status = 'ACTIVE' ORDER BY quality_score DESC LIMIT 3.\n"
            "  - Smart Money: SELECT signal, conviction_score, conflicting_signal, "
            "    already_priced_in FROM agent_smart_money_findings "
            "    WHERE ticker = '{ticker}' ORDER BY scan_date DESC LIMIT 1.\n"
            "  - Financial: SELECT price_current, market_cap_usd, total_cash_usd, "
            "    cash_per_share, runway_months FROM companies WHERE ticker = '{ticker}'.\n"
            "  - Active trial count (CSP eligibility): SELECT COUNT(*) AS active_trial_count "
            "    FROM trial_pipeline tp JOIN studies s ON tp.nct_id = s.nct_id "
            "    WHERE tp.ticker = '{ticker}' AND s.phase IN ('PHASE1', 'PHASE2', 'PHASE3') "
            "    AND s.status IN ('RECRUITING', 'ACTIVE_NOT_RECRUITING').\n"
            "  - Historical memos: LocalRAGTool query 'investment memo {ticker}' "
            "    in agent_memos collection.\n\n"
            "STEP 2 — Biotech Alpha Score (BAS, 0–100):\n"
            "  Component formulas:\n"
            "  A. Profiler component (0–20): profiler_score × 0.20\n"
            "  B. Science component (0–25): validity_score × 0.25\n"
            "  C. Insider Normalized (0–15 base):\n"
            "     insider_norm = conviction_score × 10\n"
            "     +10 bonus if signal = CLUSTER_BUY\n"
            "     -20 penalty if signal = CLUSTER_DISCRETIONARY_SELL\n"
            "     insider_component = insider_norm × 0.15\n"
            "  D. Smart Money Normalized (0–15):\n"
            "     sm_norm = conviction_score × 10\n"
            "     sm_component = sm_norm × 0.15\n"
            "  E. Partnership Bonus (0–10):\n"
            "     +10: CO_DEVELOPMENT or LICENSING with Tier 1 pharma (quality≥4)\n"
            "     +5: TECHNOLOGY partner or Tier 2 pharma\n"
            "     +0: no material partnership\n"
            "  F. Financial Health (0–15):\n"
            "     100 × 0.15 = 15 pts: runway > 24 months\n"
            "     80 × 0.15 = 12 pts: runway 18–24 months\n"
            "     60 × 0.15 = 9 pts: runway 12–18 months\n"
            "     30 × 0.15 = 4.5 pts: runway 6–12 months\n"
            "     0: runway < 6 months\n"
            "  G. Catalyst Timing (0–10):\n"
            "     9 pts: PDUFA_DATE within 30–90 days\n"
            "     8 pts: DATA_READOUT within 30–90 days (Phase 3)\n"
            "     7 pts: ADCOMM_DATE within 30–90 days\n"
            "     5 pts: DATA_READOUT (Phase 2) or CONFERENCE within 14–30 days\n"
            "     4 pts: no near-term catalyst\n"
            "     3 pts: catalyst < 14 days (too close, binary risk)\n"
            "     Multiply by 0.10 for final component.\n"
            "  BAS = A + B + C + D + E + F + G (cap 0–100).\n\n"
            "STEP 3 — Strategy Eligibility (evaluate each independently):\n\n"
            "  A. CASH_SECURED_PUT (REQ-011):\n"
            "     REQUIRED: volatility_status = 'APPROVED'\n"
            "     REQUIRED: no PDUFA/DATA_READOUT within expiry window\n"
            "     REQUIRED: active Phase 2/3 trial count ≥ 2\n"
            "     REQUIRED: validity_score ≥ 30\n"
            "     PREFERRED: annualized_return_pct ≥ 0.18\n"
            "     Position size (Kelly): p=0.85, q=0.15, b=premium_mid/selected_strike\n"
            "       (b = fractional reward on capital at risk for the specific put contract)\n"
            "       f* = (p×b - q) / b, capped at 5% portfolio.\n\n"
            "  B. DEEP_VALUE (REQ-012):\n"
            "     Tang's Rule: cash_per_share > price_current "
            "       OR total_cash_usd > market_cap_usd × 0.90\n"
            "     Abate's Rule: runway_months > 18\n"
            "     REQUIRED: burn decreasing (check qoq from financials if available)\n"
            "     REQUIRED: validity_score ≥ 40\n"
            "     REQUIRED: kill_switch is null\n"
            "     Position size: 2–5% (halved to 1–2.5% if validity_score < 50).\n\n"
            "  C. MOONSHOT (REQ-014):\n"
            "     REQUIRED: company_type = 'THERAPEUTIC' (PLATFORM excluded entirely)\n"
            "     REQUIRED: validity_score ≥ 80 (NO exceptions)\n"
            "     REQUIRED: market_cap_usd < 500_000_000 ($500M)\n"
            "     REQUIRED: patent_cliff_risk != 'CRITICAL'\n"
            "     REQUIRED: competition_score IN ('LOW', 'MODERATE') (HIGH disqualifies)\n"
            "     REQUIRED: orphan designation OR tam_estimate_usd > 1_000_000_000\n"
            "     REQUIRED: PDUFA or Phase 3 readout within 24 months\n"
            "     Position size: 1–3% (up to 5% if BAS > 85).\n\n"
            "  D. SMART_MONEY_FOLLOW (REQ-024):\n"
            "     REQUIRED: conviction_score ≥ 7\n"
            "     REQUIRED: insider_signal IN ('CLUSTER_BUY', 'STRONG_BUY', 'SINGLE_BUY')\n"
            "     REQUIRED: already_priced_in = False (price drift ≤ 20%)\n"
            "     REQUIRED: validity_score ≥ 50\n"
            "     Position size: 2–4%.\n\n"
            "  E. COMMERCIAL_VIABILITY (REQ-027):\n"
            "     REQUIRED: patent_cliff_risk = 'LOW'\n"
            "     REQUIRED: competitive_advantage IN ('SUPERIOR', 'FIRST_MOVER')\n"
            "     REQUIRED: has confirmed Tier 1 partner (partnership quality_score ≥ 3)\n"
            "     REQUIRED: tam_estimate_usd > 500_000_000\n"
            "     Position size: 3–5%.\n\n"
            "STEP 4 — Decision Synthesis:\n"
            "  - Collect all ELIGIBLE strategies.\n"
            "  - Primary recommendation = strategy with highest expected value "
            "    (PDUFA catalyst + CSP = income; high BAS + Moonshot = growth).\n"
            "  - Secondary recommendation = next-best eligible strategy.\n"
            "  - Risk factors: list all concerns (conflicting_signal, endpoint_switching_detected, "
            "    low runway, patent cliff, IV leakage warning, already_priced_in).\n"
            "  - Add 'CONFLICTING_SIGNAL' to risk_factors if conflicting_signal = True.\n"
            "  - rNPV narrative: if rnpv_usd > market_cap_usd × 2, note undervaluation.\n"
            "  - Hashtags: #CSP_CANDIDATE (if CSP), #MOONSHOT (if Moonshot), "
            "    #DEEP_VALUE (if Deep Value), #SMART_MONEY (if Smart Money Follow), "
            "    #COMMERCIAL (if Commercial Viability).\n\n"
            "STEP 5 — Write to agent_investment_memos via DatabaseWriteTool:\n"
            f"  ticker={ticker}, memo_date=today, biotech_alpha_score=BAS,\n"
            "  primary_recommendation, secondary_recommendation,\n"
            "  risk_factors (JSON array), hashtags (JSON array), full_json (complete memo).\n\n"
            "Return JSON: {\"ticker\": str, \"bas\": float, "
            "\"primary_recommendation\": str, \"secondary_recommendation\": str|null, "
            "\"eligible_strategies\": [str], \"risk_factors\": [str], "
            "\"hashtags\": [str], "
            "\"position_size_pct\": float, \"memo_summary\": str}"
        ),
        expected_output=(
            "JSON with ticker, BAS (0–100), primary_recommendation "
            "(HARD_SELL|STRONG_SELL|CASH_SECURED_PUT|DEEP_VALUE|MOONSHOT|"
            "SMART_MONEY_FOLLOW|COMMERCIAL_VIABILITY|WATCH|NO_ACTION), "
            "secondary_recommendation (or null), eligible_strategies list, "
            "risk_factors list, hashtags list, position_size_pct, and memo_summary."
        ),
        agent=agent,
    )
