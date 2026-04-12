"""
APScheduler — orchestrates all nightly ingestion + weekly tasks.

Runs as a Docker service (`scheduler` in docker-compose.yml).
On ThinkPad dev: run directly with `python scripts/scheduler.py` to test scheduling logic.

Schedule (PRD Section 9.3):
  Every 4h (06:00, 10:00, 14:00, 18:00) — fetch_news (RSS feeds)
  Nightly 06:00 — fetch_market_data (yfinance fundamentals + prices)
  Nightly 06:30 — fetch_options (E*TRADE; skips gracefully if no session)
  Daily   07:00 — fetch_sec_filings + onboard_company (PENDING/STALE tickers)
  Daily   07:30 — fetch_aact_csvs (AACT bulk CSV download, alongside CT.gov API v2)
  Daily   08:00 — Run Data Collection Crew (Detective, Scout, Oracle)
  Daily   09:00 — Run Analysis Crew (Profiler, Peer Reviewer, Insider, Partnership, Smart Money)
  Daily   10:30 — Run Strategy Crew (Volatility, Strategist)
  Weekly  Sun 01:00 — fetch_fda_data (Orange/Purple Book + orphan DB)
  Weekly  Sun 02:00 — prune old news_articles (REQ-091); sync_local_rag (agent memos → ChromaDB)

REQ-063: scheduler SLA alerts go to output/scheduler_alerts.log
"""

import logging
import os
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.models import init_db

log = logging.getLogger(__name__)

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
_ALERT_LOG = os.path.join(OUTPUT_DIR, "scheduler_alerts.log")


def _setup_alert_log() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fh = logging.FileHandler(_ALERT_LOG)
    fh.setLevel(logging.WARNING)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)


# ---------------------------------------------------------------------------
# Job wrappers — each imports lazily to avoid circular imports
# ---------------------------------------------------------------------------


def job_fetch_news():
    from ingestion.fetch_news import run
    log.info("[scheduler] fetch_news: start")
    try:
        run()
    except Exception as exc:
        log.error("[scheduler] fetch_news FAILED: %s", exc)


def job_fetch_aact():
    from ingestion.fetch_aact_csvs import run
    log.info("[scheduler] fetch_aact_csvs: start")
    try:
        run()
    except Exception as exc:
        log.error("[scheduler] fetch_aact_csvs FAILED: %s", exc)


def job_fetch_sec():
    from ingestion.fetch_sec_filings import run
    log.info("[scheduler] fetch_sec_filings: start")
    try:
        run()
    except Exception as exc:
        log.error("[scheduler] fetch_sec_filings FAILED: %s", exc)


def job_fetch_options():
    from ingestion.fetch_options import run
    log.info("[scheduler] fetch_options: start")
    try:
        run()
    except Exception as exc:
        log.error("[scheduler] fetch_options FAILED: %s", exc)


def job_fetch_market_data():
    from ingestion.fetch_market_data import run
    log.info("[scheduler] fetch_market_data: start")
    try:
        run()
    except Exception as exc:
        log.error("[scheduler] fetch_market_data FAILED: %s", exc)


def job_onboard_pending():
    from scripts.onboard_company import run_pending
    log.info("[scheduler] onboard_company (PENDING/STALE): start")
    try:
        run_pending(workers=3)
    except Exception as exc:
        log.error("[scheduler] onboard_company FAILED: %s", exc)


def job_fetch_fda():
    from ingestion.fetch_fda_data import run
    log.info("[scheduler] fetch_fda_data: start")
    try:
        run()
    except Exception as exc:
        log.error("[scheduler] fetch_fda_data FAILED: %s", exc)


def job_prune_news():
    from ingestion.fetch_news import prune_old_articles
    log.info("[scheduler] prune_old_articles (REQ-091): start")
    try:
        prune_old_articles()
    except Exception as exc:
        log.error("[scheduler] prune_old_articles FAILED: %s", exc)


# ---------------------------------------------------------------------------
# Crew execution jobs (REQ-063: 08:00 → 09:00 → 10:30, done by 11:00 UTC)
# ---------------------------------------------------------------------------

def _sla_guard(job_name: str, max_seconds: int = 1800) -> bool:
    """
    REQ-063: Return True if it is safe to start job_name.
    Writes a start-marker to scheduler_alerts.log and checks for stale markers
    from the prior job that would indicate the pipeline is still running.
    Returns False (skip) if a stale in-progress marker is found.
    """
    import time as _time
    marker_key = f"_active_{job_name}"
    now = _time.time()

    # Check for stale marker from a prior invocation
    existing = getattr(_sla_guard, marker_key, None)
    if existing is not None:
        elapsed = now - existing
        if elapsed < max_seconds:
            log.warning(
                "[scheduler] SLA guard: %s still running (%.0fs) — skipping this run",
                job_name, elapsed,
            )
            return False
        else:
            log.warning(
                "[scheduler] SLA guard: %s took %.0fs (> %ds SLA limit)",
                job_name, elapsed, max_seconds,
            )

    setattr(_sla_guard, marker_key, now)
    return True


def _sla_clear(job_name: str) -> None:
    marker_key = f"_active_{job_name}"
    setattr(_sla_guard, marker_key, None)


def job_run_data_collection_crew():
    """Run Crew 1 (Detective, Scout, Oracle) for all active tickers. 08:00 UTC.

    Global tasks (IPO watch + RSS catalyst scan) run ONCE before the per-ticker loop.
    Per-ticker tasks (PDUFA hunt, data readouts, entity resolution) run for each ticker.
    """
    if not _sla_guard("crew1"):
        return
    log.info("[scheduler] Crew 1 (Data Collection): start")
    try:
        from src.db.data_manager import get_session
        from src.db.models import Company
        from src.crews.data_collection_crew import run_data_collection_crew, run_global_data_collection
        from sqlmodel import select

        # Global tasks first — IPO watch + RSS catalyst scan (once per day, not per ticker)
        try:
            run_global_data_collection()
        except Exception as exc:
            log.error("[scheduler] Crew 1 global tasks FAILED: %s", exc)

        with get_session() as session:
            tickers = session.exec(
                select(Company.ticker).where(Company.is_active == True)
            ).all()

        for ticker in tickers:
            try:
                run_data_collection_crew(ticker)
            except Exception as exc:
                log.error("[scheduler] Crew 1 FAILED for %s: %s", ticker, exc)
    except Exception as exc:
        log.error("[scheduler] Crew 1 job FAILED: %s", exc)
    finally:
        _sla_clear("crew1")
    log.info("[scheduler] Crew 1 complete")


def job_run_analysis_crew():
    """Run Crew 2 (Profiler, Peer Reviewer, Insider, Partnership, Smart Money). 09:00 UTC."""
    if not _sla_guard("crew2"):
        return
    log.info("[scheduler] Crew 2 (Analysis): start")
    try:
        from src.db.data_manager import get_session
        from src.db.models import Company
        from src.crews.analysis_crew import run_analysis_crew
        from sqlmodel import select

        with get_session() as session:
            tickers = session.exec(
                select(Company.ticker).where(Company.is_active == True)
            ).all()

        for ticker in tickers:
            try:
                run_analysis_crew(ticker)
            except Exception as exc:
                log.error("[scheduler] Crew 2 FAILED for %s: %s", ticker, exc)
    except Exception as exc:
        log.error("[scheduler] Crew 2 job FAILED: %s", exc)
    finally:
        _sla_clear("crew2")
    log.info("[scheduler] Crew 2 complete")


def job_run_strategy_crew():
    """Run Crew 3 (Volatility, Strategist). 10:30 UTC."""
    if not _sla_guard("crew3"):
        return
    log.info("[scheduler] Crew 3 (Strategy): start")
    try:
        from src.db.data_manager import get_session
        from src.db.models import Company
        from src.crews.strategy_crew import run_strategy_crew
        from sqlmodel import select

        with get_session() as session:
            tickers = session.exec(
                select(Company.ticker).where(Company.is_active == True)
            ).all()

        for ticker in tickers:
            try:
                run_strategy_crew(ticker)
            except Exception as exc:
                log.error("[scheduler] Crew 3 FAILED for %s: %s", ticker, exc)
    except Exception as exc:
        log.error("[scheduler] Crew 3 job FAILED: %s", exc)
    finally:
        _sla_clear("crew3")
    log.info("[scheduler] Crew 3 complete")


def job_sync_local_rag():
    """
    Weekly Sunday 02:00 UTC — embed new agent memos into ChromaDB agent_memos collection.
    Sets uploaded_to_rag = 1 on synced records (REQ-072 memory loop).
    """
    log.info("[scheduler] sync_local_rag: start")
    try:
        from scripts.sync_local_rag import run
        run()
    except Exception as exc:
        log.error("[scheduler] sync_local_rag FAILED: %s", exc)
    log.info("[scheduler] sync_local_rag complete")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    _setup_alert_log()

    log.info("Initialising database...")
    init_db()

    scheduler = BlockingScheduler(timezone="UTC")

    # Every 4 hours — news (REQ-087)
    scheduler.add_job(job_fetch_news, CronTrigger(hour="*/4"), id="fetch_news")

    # Nightly batch (PRD Section 9.3 schedule — must complete before Crew 1 at 08:00)
    scheduler.add_job(job_fetch_market_data, CronTrigger(hour=6, minute=0), id="fetch_market")
    scheduler.add_job(job_fetch_options, CronTrigger(hour=6, minute=30), id="fetch_options")
    scheduler.add_job(job_fetch_sec, CronTrigger(hour=7, minute=0), id="fetch_sec")
    scheduler.add_job(job_onboard_pending, CronTrigger(hour=7, minute=0), id="onboard_pending")
    scheduler.add_job(job_fetch_aact, CronTrigger(hour=7, minute=30), id="fetch_aact")

    # Weekly (Sunday)
    scheduler.add_job(job_fetch_fda, CronTrigger(day_of_week="sun", hour=1, minute=0), id="fetch_fda")
    scheduler.add_job(job_prune_news, CronTrigger(day_of_week="sun", hour=2, minute=0), id="prune_news")

    # Crew execution (08:00 → 09:00 → 10:30 UTC — must complete by 11:00)
    scheduler.add_job(job_run_data_collection_crew, CronTrigger(hour=8, minute=0), id="crew1")
    scheduler.add_job(job_run_analysis_crew, CronTrigger(hour=9, minute=0), id="crew2")
    scheduler.add_job(job_run_strategy_crew, CronTrigger(hour=10, minute=30), id="crew3")

    # Weekly: sync agent memos to ChromaDB (Sunday 02:00 UTC — same window as prune_news)
    scheduler.add_job(
        job_sync_local_rag,
        CronTrigger(day_of_week="sun", hour=2, minute=0),
        id="sync_rag",
    )

    log.info("Scheduler started. Jobs: %s", [j.id for j in scheduler.get_jobs()])
    log.info("Press Ctrl+C to exit.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
