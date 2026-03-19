"""
Crew 1 — Data Collection Crew

Agents: Detective (001), Scout (002), Oracle (008)
Process: Detective + Scout run in parallel → Oracle runs after (needs resolved tickers)

Entry point: run_data_collection_crew(ticker)
"""

import json
import logging
import os

from crewai import Crew, Process

from src.agents.agent_001_detective import make_detective_agent, make_detective_task
from src.agents.agent_002_scout import (
    make_disease_context_task,
    make_ma_news_signal_task,
    make_new_trial_discovery_task,
    make_scout_agent,
)
from src.agents.agent_008_oracle import (
    make_data_readout_task,
    make_oracle_agent,
    make_pdufa_hunt_task,
    make_rss_catalyst_task,
)
from src.db.data_manager import get_session, write_agent_json_output

log = logging.getLogger(__name__)


def run_data_collection_crew(ticker: str, sponsor_names: list[str] | None = None) -> dict:
    """
    Run the Data Collection Crew for a single ticker.

    Args:
        ticker: Stock ticker symbol (e.g., 'MRNA')
        sponsor_names: ClinicalTrials.gov sponsor names to resolve (optional).
                       If None, fetches INDUSTRY sponsors from the studies table.

    Returns:
        dict with keys: detective, scout_disease, scout_trials, scout_ma, oracle_pdufa,
                        oracle_readouts, oracle_rss
    """
    log.info("Crew 1 starting for %s", ticker)

    # Build agents
    detective = make_detective_agent()
    scout = make_scout_agent()
    oracle = make_oracle_agent()

    # If no sponsor_names provided, fetch from DB
    if sponsor_names is None:
        try:
            from src.db.models import Study
            from sqlmodel import select
            with get_session() as session:
                rows = session.exec(
                    select(Study.lead_sponsor).where(
                        Study.ticker == ticker,
                        Study.lead_sponsor_class == "INDUSTRY",
                    ).distinct()
                ).all()
                sponsor_names = [r for r in rows if r]
        except Exception as exc:
            log.warning("Could not fetch sponsor names: %s", exc)
            sponsor_names = []

    # --- Phase 1: Detective + Scout (run sequentially as CrewAI tasks) ---
    phase1_tasks = []

    if sponsor_names:
        phase1_tasks.append(make_detective_task(detective, sponsor_names))

    phase1_tasks.append(make_disease_context_task(scout, [ticker]))
    phase1_tasks.append(make_new_trial_discovery_task(scout, ticker))
    phase1_tasks.append(make_ma_news_signal_task(scout))

    crew1 = Crew(
        agents=[detective, scout],
        tasks=phase1_tasks,
        process=Process.sequential,
        verbose=False,
    )

    phase1_result = crew1.kickoff()
    log.info("Crew 1 Phase 1 complete for %s", ticker)

    # --- Phase 2: Oracle (depends on resolved tickers from Detective) ---
    oracle_tasks = [
        make_pdufa_hunt_task(oracle, ticker),
        make_data_readout_task(oracle, ticker),
        make_rss_catalyst_task(oracle),
    ]

    crew2 = Crew(
        agents=[oracle],
        tasks=oracle_tasks,
        process=Process.sequential,
        verbose=False,
    )

    phase2_result = crew2.kickoff()
    log.info("Crew 1 Phase 2 (Oracle) complete for %s", ticker)

    # Combine results
    result = {
        "ticker": ticker,
        "phase1": str(phase1_result),
        "phase2": str(phase2_result),
    }

    # Dual-write output (REQ-072)
    write_agent_json_output("data_collection_crew", ticker, result)

    return result
