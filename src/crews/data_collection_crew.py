"""
Crew 1 — Data Collection Crew

Agents: Detective (001), Scout (002), Oracle (008)
Process: Detective + Scout run in parallel → Oracle runs after (needs resolved tickers)

Entry points:
  run_data_collection_crew(ticker) — per-ticker: entity resolution + disease context + PDUFA
  run_global_data_collection()    — once per day: IPO watch + RSS catalyst scan (not per-ticker)
"""

import logging

from crewai import Crew, Process

from src.agents.agent_001_detective import make_detective_agent, make_detective_task
from src.agents.agent_002_scout import (
    make_disease_context_task,
    make_ipo_watch_task,
    make_ma_news_signal_task,
    make_new_trial_discovery_task,
    make_scout_agent,
    make_ticker_onboarding_handoff_task,
)
from src.agents.agent_008_oracle import (
    make_data_readout_task,
    make_oracle_agent,
    make_pdufa_hunt_task,
    make_rss_catalyst_task,
)
from src.db.data_manager import get_session, write_agent_json_output

log = logging.getLogger(__name__)


def run_global_data_collection() -> dict:
    """
    Global daily tasks that must run ONCE (not per-ticker):
      - Scout IPO Watch (Task A): scans SEC EDGAR for new S-1 filings — last 7 days
      - Oracle RSS Catalyst Scan: extracts PDUFA/conference catalysts from news articles
      - Scout Ticker Onboarding Handoff (Task D): marks newly-PENDING tickers as IN_PROGRESS
        and writes company_onboarding_log rows — runs sequentially AFTER IPO Watch

    Called by scheduler BEFORE the per-ticker loop (08:00 UTC, ahead of per-ticker runs).
    Running these per-ticker would cause 1,000+ redundant global news scans daily.
    """
    log.info("Crew 1 global tasks: IPO watch + RSS catalyst scan")

    scout = make_scout_agent()
    oracle = make_oracle_agent()

    global_tasks = [
        make_ipo_watch_task(scout),
        make_rss_catalyst_task(oracle),
    ]

    crew = Crew(
        agents=[scout, oracle],
        tasks=global_tasks,
        process=Process.parallel,
        verbose=False,
    )

    result_raw = crew.kickoff()

    # Task D — Ticker Onboarding Handoff: must run AFTER IPO Watch (sequential)
    # Marks newly-detected PENDING tickers as IN_PROGRESS and writes to company_onboarding_log
    # so onboard_company.py picks them up at its 07:00 run the following day.
    handoff_task = make_ticker_onboarding_handoff_task(scout)
    handoff_crew = Crew(
        agents=[scout],
        tasks=[handoff_task],
        process=Process.sequential,
        verbose=False,
    )
    handoff_raw = handoff_crew.kickoff()

    result = {"type": "global", "output": str(result_raw), "handoff": str(handoff_raw)}
    write_agent_json_output("data_collection_crew_global", "GLOBAL", result)
    log.info("Crew 1 global tasks complete")
    return result


def run_data_collection_crew(ticker: str, sponsor_names: list[str] | None = None) -> dict:
    """
    Run the Data Collection Crew for a single ticker.

    Args:
        ticker: Stock ticker symbol (e.g., 'MRNA')
        sponsor_names: ClinicalTrials.gov sponsor names to resolve (optional).
                       If None, fetches INDUSTRY sponsors from the studies table.

    Returns:
        dict with ticker, phase1, phase2 output strings
    """
    log.info("Crew 1 starting for %s", ticker)

    # Build agents
    detective = make_detective_agent()
    scout = make_scout_agent()
    oracle = make_oracle_agent()

    # If no sponsor_names provided, fetch from DB
    if sponsor_names is None:
        try:
            from src.db.models import Study, TrialPipeline
            from sqlmodel import select
            with get_session() as session:
                rows = session.exec(
                    select(Study.lead_sponsor)
                    .join(TrialPipeline, TrialPipeline.nct_id == Study.nct_id)
                    .where(
                        TrialPipeline.ticker == ticker,
                        Study.lead_sponsor_class == "INDUSTRY",
                    ).distinct()
                ).all()
                sponsor_names = [r for r in rows if r]
        except Exception as exc:
            log.warning("Could not fetch sponsor names: %s", exc)
            sponsor_names = []

    # --- Phase 1: Detective + Scout run in parallel ---
    phase1_tasks = []

    if sponsor_names:
        phase1_tasks.append(make_detective_task(detective, sponsor_names))

    phase1_tasks.append(make_disease_context_task(scout, [ticker]))
    phase1_tasks.append(make_new_trial_discovery_task(scout, ticker))
    phase1_tasks.append(make_ma_news_signal_task(scout))

    crew1 = Crew(
        agents=[detective, scout],
        tasks=phase1_tasks,
        process=Process.parallel,
        verbose=False,
    )

    phase1_result = crew1.kickoff()
    log.info("Crew 1 Phase 1 complete for %s", ticker)

    # --- Phase 2: Oracle ticker-specific tasks (PDUFA + data readouts) ---
    # NOTE: RSS catalyst scan is NOT here — it runs once globally via run_global_data_collection()
    oracle_tasks = [
        make_pdufa_hunt_task(oracle, ticker),
        make_data_readout_task(oracle, ticker),
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
