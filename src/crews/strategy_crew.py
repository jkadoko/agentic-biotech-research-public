"""
Crew 3 — Strategy Crew

Agents: Volatility (009), Strategist (006)
Process: Volatility runs first (CSP evaluation) → Strategist synthesizes all inputs

Entry point: run_strategy_crew(ticker)
Full pipeline: run_full_analysis(ticker) — runs all 3 crews in sequence
"""

import logging

from crewai import Crew, Process

from src.agents.agent_006_strategist import make_strategist_agent, make_strategist_task
from src.agents.agent_009_volatility import make_volatility_agent, make_volatility_task
from src.db.data_manager import write_agent_json_output

log = logging.getLogger(__name__)


def run_strategy_crew(ticker: str) -> dict:
    """
    Run the Strategy Crew for a single ticker.

    Volatility (009) runs first to evaluate CSP opportunity.
    Strategist (006) then synthesizes all 8 agent outputs into an investment memo.

    Args:
        ticker: Stock ticker symbol

    Returns:
        dict with volatility and strategist outputs
    """
    log.info("Crew 3 starting for %s", ticker)

    # Build agents
    volatility = make_volatility_agent()
    strategist = make_strategist_agent()

    # Build tasks — Volatility first, then Strategist
    tasks = [
        make_volatility_task(volatility, ticker),
        make_strategist_task(strategist, ticker),
    ]

    crew = Crew(
        agents=[volatility, strategist],
        tasks=tasks,
        process=Process.sequential,
        verbose=False,
    )

    result_raw = crew.kickoff()
    log.info("Crew 3 complete for %s", ticker)

    result = {
        "ticker": ticker,
        "output": str(result_raw),
    }

    # Dual-write output (REQ-072)
    write_agent_json_output("strategy_crew", ticker, result)

    return result


def run_full_analysis(ticker: str) -> dict:
    """
    Run all 3 crews in sequence for a single ticker.

    Schedule (from PROJECT_PLAN.md):
      08:00 Crew 1 (Data Collection)
      09:00 Crew 2 (Analysis)
      10:30 Crew 3 (Strategy)

    Args:
        ticker: Stock ticker symbol

    Returns:
        dict with combined results from all 3 crews
    """
    from src.crews.data_collection_crew import run_data_collection_crew
    from src.crews.analysis_crew import run_analysis_crew

    log.info("Full analysis pipeline starting for %s", ticker)

    crew1_result = run_data_collection_crew(ticker)
    log.info("Crew 1 complete for %s", ticker)

    crew2_result = run_analysis_crew(ticker)
    log.info("Crew 2 complete for %s", ticker)

    crew3_result = run_strategy_crew(ticker)
    log.info("Crew 3 complete for %s", ticker)

    combined = {
        "ticker": ticker,
        "data_collection": crew1_result,
        "analysis": crew2_result,
        "strategy": crew3_result,
    }

    write_agent_json_output("full_analysis", ticker, combined)
    log.info("Full analysis complete for %s", ticker)

    return combined
