"""
Crew 2 — Analysis Crew

Agents: Profiler (003), Peer Reviewer (004), Insider (005),
        Partnership (010), Smart Money (011)
Process: All 5 run in parallel (each has independent DB outputs;
         Strategist in Crew 3 reads all of their outputs)

Entry point: run_analysis_crew(ticker)
"""

import logging

from crewai import Crew, Process

from src.agents.agent_003_profiler import make_profiler_agent, make_profiler_task
from src.agents.agent_004_peer_reviewer import (
    make_peer_reviewer_agent,
    make_peer_reviewer_task,
)
from src.agents.agent_005_insider import make_insider_agent, make_insider_task
from src.agents.agent_010_partnership import make_partnership_agent, make_partnership_task
from src.agents.agent_011_smart_money import make_smart_money_agent, make_smart_money_task
from src.db.data_manager import get_session, write_agent_json_output

log = logging.getLogger(__name__)


def _get_primary_nct_id(ticker: str) -> str | None:
    """Fetch the highest-priority active Phase 3 NCT ID for this ticker.

    Uses trial_pipeline as the ticker→NCT linkage table (studies has no ticker column).
    """
    try:
        from src.db.models import Study, TrialPipeline
        from sqlmodel import select
        with get_session() as session:
            row = session.exec(
                select(Study.nct_id)
                .join(TrialPipeline, TrialPipeline.nct_id == Study.nct_id)
                .where(
                    TrialPipeline.ticker == ticker,
                    Study.phase == "PHASE3",
                    Study.status.in_(["ACTIVE_NOT_RECRUITING", "RECRUITING", "COMPLETED"]),
                ).order_by(Study.primary_completion_date.desc())
            ).first()
            return row
    except Exception as exc:
        log.warning("Could not fetch NCT ID for %s: %s", ticker, exc)
        return None


def run_analysis_crew(ticker: str, nct_id: str | None = None) -> dict:
    """
    Run the Analysis Crew for a single ticker.

    Args:
        ticker: Stock ticker symbol
        nct_id: NCT ID for Peer Reviewer audit. If None, uses primary Phase 3 trial.

    Returns:
        dict with analysis results from all 5 agents
    """
    log.info("Crew 2 starting for %s", ticker)

    if nct_id is None:
        nct_id = _get_primary_nct_id(ticker)
        if nct_id is None:
            log.warning("No Phase 3 NCT ID found for %s — Peer Reviewer will use web search", ticker)
            nct_id = f"UNKNOWN_{ticker}"

    # Build agents
    profiler = make_profiler_agent()
    peer_reviewer = make_peer_reviewer_agent()
    insider = make_insider_agent()
    partnership = make_partnership_agent()
    smart_money = make_smart_money_agent()

    # Build tasks
    tasks = [
        make_profiler_task(profiler, ticker),
        make_peer_reviewer_task(peer_reviewer, ticker, nct_id),
        make_insider_task(insider, ticker),
        make_partnership_task(partnership, ticker),
        make_smart_money_task(smart_money, ticker),
    ]

    crew = Crew(
        agents=[profiler, peer_reviewer, insider, partnership, smart_money],
        tasks=tasks,
        process=Process.parallel,
        verbose=False,
    )

    result_raw = crew.kickoff()
    log.info("Crew 2 complete for %s", ticker)

    result = {
        "ticker": ticker,
        "nct_id": nct_id,
        "output": str(result_raw),
    }

    # Dual-write output (REQ-072)
    write_agent_json_output("analysis_crew", ticker, result)

    return result
