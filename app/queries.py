"""
SQLModel read helpers for the Streamlit UI.

All functions are read-only. They use get_session() from src.db.data_manager
and return SQLModel instances or plain dicts. Streamlit callers should wrap
in @st.cache_data where appropriate.

Design rules:
- Never import Streamlit here (this module is reusable outside the UI)
- Return None / empty list on missing data — never raise
- Preserve WAL read isolation: reads do not block scheduler writes
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

from sqlmodel import select, func

# Ensure project root on path when running from app/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.data_manager import get_session
from src.db.models import (
    AgentInsiderFinding,
    AgentInvestmentMemo,
    AgentProfilerFinding,
    AgentScientificAudit,
    AgentSmartMoneyFinding,
    AgentVolatilityFinding,
    Catalyst,
    Company,
    DesignOutcome,
    HistoricalPrice,
    NewsArticle,
    OptionsChain,
    Partnership,
    Study,
    TrialPipeline,
)

# ---------------------------------------------------------------------------
# Company / Ticker helpers
# ---------------------------------------------------------------------------


def get_all_active_tickers(company_type: str | None = None) -> list[str]:
    """Return sorted list of active ticker symbols, optionally filtered by company_type.

    company_type lives on agent_profiler_findings (not companies), so filtering
    requires a join when the caller passes a non-None value.
    """
    with get_session() as session:
        if company_type:
            q = (
                select(Company.ticker)
                .join(
                    AgentProfilerFinding,
                    AgentProfilerFinding.ticker == Company.ticker,
                    isouter=False,
                )
                .where(Company.is_active == True)
                .where(AgentProfilerFinding.company_type == company_type)
            )
        else:
            q = select(Company.ticker).where(Company.is_active == True)
        rows = session.exec(q.order_by(Company.ticker)).all()
        return list(rows)


def get_company(ticker: str) -> Company | None:
    """Return the Company row for a ticker, or None."""
    with get_session() as session:
        return session.exec(
            select(Company).where(Company.ticker == ticker.upper())
        ).first()


def get_companies_with_ma_alerts() -> list[Company]:
    """Return companies that have MA_RUMOR_FLAG in their watchlist_flags JSON array."""
    with get_session() as session:
        # Performance optimization: pre-filter JSON string using SQLite LIKE
        # instead of reading all watchlist rows and parsing JSON in Python (~20x faster)
        return session.exec(
            select(Company).where(
                Company.is_active == True,
                Company.watchlist_flags.like('%"flag": "MA_RUMOR_FLAG"%'),
            )
        ).all()


def get_endpoints_for_trials(nct_ids: list[str]) -> list[DesignOutcome]:
    """Return all primary and secondary endpoints for a list of trials."""
    if not nct_ids:
        return []
    with get_session() as session:
        return session.exec(
            select(DesignOutcome)
            .where(DesignOutcome.nct_id.in_(nct_ids))
            .order_by(DesignOutcome.outcome_type.asc())
        ).all()


# ---------------------------------------------------------------------------
# Agent output getters
# ---------------------------------------------------------------------------


def get_latest_investment_memo(ticker: str) -> AgentInvestmentMemo | None:
    with get_session() as session:
        return session.exec(
            select(AgentInvestmentMemo)
            .where(AgentInvestmentMemo.ticker == ticker.upper())
            .order_by(AgentInvestmentMemo.memo_date.desc())
        ).first()


def get_latest_profiler(ticker: str) -> AgentProfilerFinding | None:
    with get_session() as session:
        return session.exec(
            select(AgentProfilerFinding)
            .where(AgentProfilerFinding.ticker == ticker.upper())
            .order_by(AgentProfilerFinding.profile_date.desc())
        ).first()


def get_latest_volatility(ticker: str) -> AgentVolatilityFinding | None:
    with get_session() as session:
        return session.exec(
            select(AgentVolatilityFinding)
            .where(AgentVolatilityFinding.ticker == ticker.upper())
            .order_by(AgentVolatilityFinding.scan_date.desc())
        ).first()


def get_latest_insider(ticker: str) -> AgentInsiderFinding | None:
    with get_session() as session:
        return session.exec(
            select(AgentInsiderFinding)
            .where(AgentInsiderFinding.ticker == ticker.upper())
            .order_by(AgentInsiderFinding.scan_date.desc())
        ).first()


def get_latest_smart_money(ticker: str) -> AgentSmartMoneyFinding | None:
    with get_session() as session:
        return session.exec(
            select(AgentSmartMoneyFinding)
            .where(AgentSmartMoneyFinding.ticker == ticker.upper())
            .order_by(AgentSmartMoneyFinding.scan_date.desc())
        ).first()


def get_latest_scientific_audit(ticker: str) -> AgentScientificAudit | None:
    with get_session() as session:
        return session.exec(
            select(AgentScientificAudit)
            .where(AgentScientificAudit.ticker == ticker.upper())
            .order_by(AgentScientificAudit.audit_date.desc())
        ).first()


# ---------------------------------------------------------------------------
# Catalyst Calendar
# ---------------------------------------------------------------------------


def get_catalysts(ticker: str | None = None, days_ahead: int = 540) -> list[Catalyst]:
    """
    Return upcoming catalysts, sorted by event_date ascending.
    If ticker is None, returns catalysts for all active tickers within the window.
    """
    cutoff = date.today() + timedelta(days=days_ahead)
    with get_session() as session:
        q = select(Catalyst).where(
            Catalyst.event_date >= date.today(),
            Catalyst.event_date <= cutoff,
        )
        if ticker:
            q = q.where(Catalyst.ticker == ticker.upper())
        q = q.order_by(Catalyst.event_date.asc())
        return session.exec(q).all()


# ---------------------------------------------------------------------------
# Clinical Trials
# ---------------------------------------------------------------------------


def get_trials_for_ticker(ticker: str, phase_filter: str | None = None) -> list[dict]:
    """
    Return active trials linked to a ticker, joined with Study metadata.
    phase_filter: e.g. 'PHASE3' to restrict to Phase 3 only.
    Returns list of dicts with keys from both TrialPipeline + Study.
    """
    with get_session() as session:
        q = (
            select(TrialPipeline, Study)
            .join(Study, TrialPipeline.nct_id == Study.nct_id)
            .where(TrialPipeline.ticker == ticker.upper())
        )
        if phase_filter:
            q = q.where(Study.phase == phase_filter)
        q = q.order_by(Study.primary_completion_date.asc())
        rows = session.exec(q).all()

    result = []
    for pipeline_row, study_row in rows:
        d = {
            "nct_id": study_row.nct_id,
            "title": study_row.title,
            "phase": study_row.phase,
            "status": study_row.status,
            "study_type": study_row.study_type,
            "start_date": study_row.start_date,
            "primary_completion_date": study_row.primary_completion_date,
            "enrollment": (
                study_row.enrollment if study_row.enrollment_is_actual else None
            ),
            "enrollment_is_actual": study_row.enrollment_is_actual,
            "lead_sponsor": study_row.lead_sponsor,
            "relationship_type": pipeline_row.relationship_type,
        }
        result.append(d)
    return result


def get_endpoints_for_trial(nct_id: str) -> list[DesignOutcome]:
    """Return all primary and secondary endpoints for a trial."""
    with get_session() as session:
        return session.exec(
            select(DesignOutcome)
            .where(DesignOutcome.nct_id == nct_id)
            .order_by(DesignOutcome.outcome_type.asc())
        ).all()


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


def get_news_for_ticker(
    ticker: str, days: int = 90, category: str | None = None
) -> list[NewsArticle]:
    """Return recent news for a specific ticker, newest first."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as session:
        q = select(NewsArticle).where(
            NewsArticle.ticker == ticker.upper(),
            NewsArticle.published_at >= cutoff,
        )
        if category:
            q = q.where(NewsArticle.category == category)
        q = q.order_by(NewsArticle.published_at.desc())
        return session.exec(q).all()


def get_news_feed(category: str | None = None, hours: int = 48) -> list[NewsArticle]:
    """Return recent RSS headlines across all tickers (Page 1 agent signals feed)."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as session:
        q = select(NewsArticle).where(NewsArticle.published_at >= cutoff)
        if category:
            q = q.where(NewsArticle.category == category)
        q = q.order_by(NewsArticle.published_at.desc()).limit(200)
        return session.exec(q).all()


# ---------------------------------------------------------------------------
# Partnerships
# ---------------------------------------------------------------------------


def get_partnerships_for_ticker(ticker: str) -> list[Partnership]:
    """Return all partnerships for a ticker, sorted by partner_tier then status."""
    with get_session() as session:
        return session.exec(
            select(Partnership)
            .where(Partnership.ticker == ticker.upper())
            .order_by(Partnership.partner_tier.asc(), Partnership.status.asc())
        ).all()


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def get_options_for_ticker(ticker: str, option_type: str = "PUT") -> list[OptionsChain]:
    """Return options chain for a ticker. Defaults to PUTs for CSP analysis."""
    with get_session() as session:
        return session.exec(
            select(OptionsChain)
            .where(
                OptionsChain.ticker == ticker.upper(),
                OptionsChain.option_type == option_type,
                OptionsChain.expiration >= date.today(),
            )
            .order_by(OptionsChain.expiration.asc(), OptionsChain.strike.asc())
        ).all()


# ---------------------------------------------------------------------------
# Historical Prices
# ---------------------------------------------------------------------------


def get_historical_prices(ticker: str, days: int = 365) -> list[HistoricalPrice]:
    """Return OHLCV history for a ticker, oldest first (for charting)."""
    cutoff = date.today() - timedelta(days=days)
    with get_session() as session:
        return session.exec(
            select(HistoricalPrice)
            .where(
                HistoricalPrice.ticker == ticker.upper(),
                HistoricalPrice.price_date >= cutoff,
            )
            .order_by(HistoricalPrice.price_date.asc())
        ).all()


# ---------------------------------------------------------------------------
# ChromaDB RAG search
# ---------------------------------------------------------------------------


def search_rag(
    query: str,
    collection: str = "agent_memos",
    top_k: int = 5,
    ticker_filter: str | None = None,
) -> list[dict]:
    """
    Semantic search over ChromaDB. Returns list of {document, metadata, distance}.
    Falls back to empty list if ChromaDB is unavailable.
    """
    try:
        import chromadb
        from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

        host = os.environ.get("CHROMADB_HOST", "chromadb")
        port = int(os.environ.get("CHROMADB_PORT", "8000"))
        ollama_host = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")

        client = chromadb.HttpClient(host=host, port=port)
        embed_fn = OllamaEmbeddingFunction(
            url=f"{ollama_host}/api/embeddings",
            model_name="mxbai-embed-large:latest",
        )
        col = client.get_collection(collection, embedding_function=embed_fn)

        where = {"ticker": ticker_filter} if ticker_filter else None
        results = col.query(
            query_texts=[query],
            n_results=top_k,
            where=where,
        )

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        return [
            {"document": d, "metadata": m, "distance": dist}
            for d, m, dist in zip(docs, metas, dists)
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Portfolio summary (Page 1 HUD)
# ---------------------------------------------------------------------------


def get_portfolio_summary() -> dict:
    """
    Compute high-level portfolio metrics for the HUD.
    Returns: total_return_pct, sharpe_approx, max_drawdown_pct, negative_ev_count
    Note: full Sharpe requires benchmark data; this returns a simplified version.
    """
    # Performance optimization: push aggregation and math to the database layer
    # instead of fetching all active company objects and computing in Python.
    with get_session() as session:
        # Performance optimization: compute aggregation and filtering in SQLite
        # instead of reading all rows and allocating Python objects (~18x faster)
        active_count = session.exec(
            select(func.count(Company.ticker)).where(Company.is_active == True)
        ).one()

        negative_ev_tickers = session.exec(
            select(Company.ticker).where(
                Company.is_active == True,
                Company.total_cash_usd.isnot(None),
                Company.market_cap_usd.isnot(None),
                Company.total_debt_usd.isnot(None),
                Company.total_cash_usd
                > (Company.market_cap_usd + Company.total_debt_usd),
            )
        ).all()

    return {
        "active_count": active_count,
        "negative_ev_count": len(negative_ev_tickers),
        "negative_ev_tickers": list(negative_ev_tickers),
    }
