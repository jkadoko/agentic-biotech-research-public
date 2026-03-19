"""
REQ-004-compliant upsert helpers for biotech_tracker.db.

REQ-004: NULL incoming values MUST NOT overwrite existing non-NULL values.
All public functions in this module enforce this constraint via _merge_into().

Usage:
    from src.db.data_manager import upsert_company, upsert_study, get_session
    with get_session() as session:
        upsert_company(session, {"ticker": "MRNA", "company_name": "Moderna"})
        session.commit()
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Generator

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from src.db.models import (
    AgentInsiderFinding,
    AgentInvestmentMemo,
    AgentProfilerFinding,
    AgentScientificAudit,
    AgentSmartMoneyFinding,
    AgentVolatilityFinding,
    Catalyst,
    Collaborator,
    Company,
    CompanyOnboardingLog,
    Condition,
    DesignOutcome,
    DiseaseContext,
    EntityAlias,
    HistoricalPrice,
    Intervention,
    NewsArticle,
    OptionsChain,
    Orphan,
    Partnership,
    SecFiling,
    SmartMoneyPosition,
    Study,
    SystemMetadata,
    TrialPipeline,
    engine,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager yielding a SQLModel Session. Caller must commit."""
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Core upsert primitive (REQ-004)
# ---------------------------------------------------------------------------


def _merge_into(existing: Any, incoming: dict[str, Any]) -> bool:
    """
    Apply fields from `incoming` onto `existing` SQLModel instance,
    skipping any key whose incoming value is None (REQ-004).

    Returns True if any field was changed.
    """
    changed = False
    for key, value in incoming.items():
        if value is None:
            continue  # REQ-004: never overwrite with NULL
        if getattr(existing, key, None) != value:
            setattr(existing, key, value)
            changed = True
    return changed


def _set_last_updated(obj: Any) -> None:
    if hasattr(obj, "last_updated"):
        obj.last_updated = datetime.utcnow()


# ---------------------------------------------------------------------------
# Company
# ---------------------------------------------------------------------------


def upsert_company(session: Session, data: dict[str, Any]) -> Company:
    """
    Insert or update a row in `companies`.
    REQ-004: NULL values never overwrite existing non-NULL values.
    """
    ticker = data.get("ticker")
    if not ticker:
        raise ValueError("upsert_company: 'ticker' is required")

    existing = session.get(Company, ticker)
    if existing is None:
        obj = Company(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj

    _merge_into(existing, data)
    _set_last_updated(existing)
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# Historical Prices
# ---------------------------------------------------------------------------


def upsert_historical_price(session: Session, data: dict[str, Any]) -> HistoricalPrice:
    ticker = data["ticker"]
    price_date = data.get("price_date") or data.get("date")
    # Normalise key: model field is price_date, ingestion dicts may use "date"
    normalised = {("price_date" if k == "date" else k): v for k, v in data.items() if v is not None}
    existing = session.get(HistoricalPrice, (ticker, price_date))
    if existing is None:
        obj = HistoricalPrice(**normalised)
        session.add(obj)
        return obj
    _merge_into(existing, normalised)
    session.add(existing)
    return existing


def bulk_upsert_prices(session: Session, rows: list[dict[str, Any]]) -> int:
    """Upsert a batch of historical price rows. Returns count upserted."""
    count = 0
    for row in rows:
        upsert_historical_price(session, row)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Options Chains
# ---------------------------------------------------------------------------


def upsert_options_chain(session: Session, data: dict[str, Any]) -> OptionsChain:
    pk = (data["ticker"], data["expiration"], data["strike"], data["option_type"])
    existing = session.get(OptionsChain, pk)
    if existing is None:
        obj = OptionsChain(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    existing.last_updated = datetime.utcnow()
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# Studies (AACT)
# ---------------------------------------------------------------------------


def upsert_study(session: Session, data: dict[str, Any]) -> Study:
    nct_id = data.get("nct_id")
    if not nct_id:
        raise ValueError("upsert_study: 'nct_id' is required")

    # Derive enrollment_is_actual from enrollment_type (REQ-074)
    if "enrollment_type" in data and data.get("enrollment_type") is not None:
        data["enrollment_is_actual"] = data["enrollment_type"] == "Actual"

    existing = session.get(Study, nct_id)
    if existing is None:
        obj = Study(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# Trial Pipeline linkage
# ---------------------------------------------------------------------------


def upsert_trial_pipeline(session: Session, ticker: str, nct_id: str,
                           relationship_type: str) -> TrialPipeline:
    """
    Insert or update a ticker↔trial linkage.
    Priority: 10K_CITED > DRUG_NAME_MATCH > COMPANY_NAME_MATCH > ENTITY_RESOLVED.
    A higher-priority relationship_type always wins over an existing lower-priority one.
    """
    _PRIORITY = {
        "10K_CITED": 4,
        "DRUG_NAME_MATCH": 3,
        "COMPANY_NAME_MATCH": 2,
        "ENTITY_RESOLVED": 1,
    }

    existing = session.get(TrialPipeline, (ticker, nct_id))
    if existing is None:
        obj = TrialPipeline(ticker=ticker, nct_id=nct_id, relationship_type=relationship_type)
        session.add(obj)
        return obj

    existing_priority = _PRIORITY.get(existing.relationship_type or "", 0)
    incoming_priority = _PRIORITY.get(relationship_type, 0)
    if incoming_priority > existing_priority:
        existing.relationship_type = relationship_type
        session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# Conditions, Interventions, Collaborators, DesignOutcomes
# ---------------------------------------------------------------------------


def upsert_condition(session: Session, data: dict[str, Any]) -> Condition:
    pk = (data["nct_id"], data["condition_name"])
    existing = session.get(Condition, pk)
    if existing is None:
        obj = Condition(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_intervention(session: Session, data: dict[str, Any]) -> Intervention:
    pk = (data["nct_id"], data["drug_name"])
    existing = session.get(Intervention, pk)
    if existing is None:
        obj = Intervention(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_collaborator(session: Session, data: dict[str, Any]) -> Collaborator:
    pk = (data["nct_id"], data["collaborator_name"])
    existing = session.get(Collaborator, pk)
    if existing is None:
        obj = Collaborator(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_design_outcome(session: Session, data: dict[str, Any]) -> DesignOutcome:
    pk = (data["nct_id"], data["outcome_type"], data["measure"])
    existing = session.get(DesignOutcome, pk)
    if existing is None:
        obj = DesignOutcome(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# Entity Aliases
# ---------------------------------------------------------------------------


def upsert_entity_alias(session: Session, data: dict[str, Any]) -> EntityAlias:
    alias = data.get("alias")
    if not alias:
        raise ValueError("upsert_entity_alias: 'alias' is required")
    existing = session.get(EntityAlias, alias)
    if existing is None:
        obj = EntityAlias(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    existing.last_verified_date = date.today()
    session.add(existing)
    return existing


def resolve_alias(session: Session, sponsor_name: str) -> EntityAlias | None:
    """Return cached resolution for a sponsor name, or None if not found."""
    return session.get(EntityAlias, sponsor_name)


# ---------------------------------------------------------------------------
# Disease Context
# ---------------------------------------------------------------------------


def upsert_disease_context(session: Session, data: dict[str, Any]) -> DiseaseContext:
    key = data.get("condition_normalized")
    if not key:
        raise ValueError("upsert_disease_context: 'condition_normalized' is required")

    # Derive is_orphan from prevalence_us (REQ-070)
    if "prevalence_us" in data and data["prevalence_us"] is not None:
        data["is_orphan"] = data["prevalence_us"] < 200_000

    existing = session.get(DiseaseContext, key)
    if existing is None:
        obj = DiseaseContext(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    existing.last_updated = date.today()
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# FDA Orphan
# ---------------------------------------------------------------------------


def upsert_orphan(session: Session, data: dict[str, Any]) -> Orphan:
    pk = (data["ticker"], data["drug_name"])
    existing = session.get(Orphan, pk)
    if existing is None:
        obj = Orphan(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# SEC Filings
# ---------------------------------------------------------------------------


def upsert_sec_filing(session: Session, data: dict[str, Any]) -> SecFiling:
    pk = (data["ticker"], data["filing_date"], data["filing_type"])
    existing = session.get(SecFiling, pk)
    if existing is None:
        obj = SecFiling(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# Onboarding Log
# ---------------------------------------------------------------------------


def write_onboarding_log(session: Session, data: dict[str, Any]) -> CompanyOnboardingLog:
    """Always inserts a new audit record (no update — audit trail must be append-only)."""
    obj = CompanyOnboardingLog(**{k: v for k, v in data.items() if v is not None})
    session.add(obj)
    return obj


# ---------------------------------------------------------------------------
# Agent Output Tables (REQ-072)
# ---------------------------------------------------------------------------


def upsert_profiler_finding(session: Session, data: dict[str, Any]) -> AgentProfilerFinding:
    pk = (data["ticker"], data["profile_date"])
    existing = session.get(AgentProfilerFinding, pk)
    if existing is None:
        obj = AgentProfilerFinding(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_scientific_audit(session: Session, data: dict[str, Any]) -> AgentScientificAudit:
    pk = (data["ticker"], data["nct_id"], data["audit_date"])
    existing = session.get(AgentScientificAudit, pk)
    if existing is None:
        obj = AgentScientificAudit(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_insider_finding(session: Session, data: dict[str, Any]) -> AgentInsiderFinding:
    pk = (data["ticker"], data["scan_date"])
    existing = session.get(AgentInsiderFinding, pk)
    if existing is None:
        obj = AgentInsiderFinding(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_catalyst(session: Session, data: dict[str, Any]) -> Catalyst:
    pk = (data["ticker"], data["event_type"], data["event_date"])
    existing = session.get(Catalyst, pk)
    if existing is None:
        obj = Catalyst(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_volatility_finding(session: Session, data: dict[str, Any]) -> AgentVolatilityFinding:
    pk = (data["ticker"], data["scan_date"])
    existing = session.get(AgentVolatilityFinding, pk)
    if existing is None:
        obj = AgentVolatilityFinding(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_partnership(session: Session, data: dict[str, Any]) -> Partnership:
    pk = (data["ticker"], data["partner_name"], data["drug_asset"])
    existing = session.get(Partnership, pk)
    if existing is None:
        obj = Partnership(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_smart_money_finding(session: Session, data: dict[str, Any]) -> AgentSmartMoneyFinding:
    pk = (data["ticker"], data["scan_date"])
    existing = session.get(AgentSmartMoneyFinding, pk)
    if existing is None:
        obj = AgentSmartMoneyFinding(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_smart_money_position(session: Session, data: dict[str, Any]) -> SmartMoneyPosition:
    pk = (data["ticker"], data["institution_name"], data["filing_date"])
    existing = session.get(SmartMoneyPosition, pk)
    if existing is None:
        obj = SmartMoneyPosition(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


def upsert_investment_memo(session: Session, data: dict[str, Any]) -> AgentInvestmentMemo:
    pk = (data["ticker"], data["memo_date"])
    existing = session.get(AgentInvestmentMemo, pk)
    if existing is None:
        obj = AgentInvestmentMemo(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        return obj
    _merge_into(existing, data)
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# News Articles (REQ-087–REQ-091)
# ---------------------------------------------------------------------------

_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    (r"acqui|merger|buy.*out|takeover", "m&a"),
    (r"ASCO|ASH|JPM|AACR|ESMO|EHA", "conference"),
    (r"upgrade|downgrade|price target|initiates", "analyst"),
    (r"partnership|collaboration|licens|royalt", "partnership"),
    (r"FDA|PDUFA|NDA|BLA|accelerated approval", "fda"),
]


def _categorize_headline(headline: str) -> str | None:
    """REQ-089: assign category from headline via regex."""
    import re
    for pattern, category in _CATEGORY_PATTERNS:
        if re.search(pattern, headline, re.IGNORECASE):
            return category
    return None


def insert_news_article(session: Session, data: dict[str, Any]) -> NewsArticle | None:
    """
    REQ-090: INSERT OR IGNORE on url uniqueness.
    Returns the inserted NewsArticle, or None if it already exists.
    Applies REQ-089 category assignment if not already set.
    """
    # REQ-089: auto-categorize
    if not data.get("category"):
        data["category"] = _categorize_headline(data.get("headline", ""))

    try:
        obj = NewsArticle(**{k: v for k, v in data.items() if v is not None})
        session.add(obj)
        session.flush()   # surface IntegrityError from UNIQUE constraint immediately
        return obj
    except IntegrityError:
        session.rollback()
        return None  # duplicate URL — REQ-090 ignore


def associate_news_tickers(session: Session) -> int:
    """
    REQ-088: Post-insert ticker association pass.
    Matches company_name (≥5 chars) against headline; writes ticker FK.
    Returns count of newly associated rows.
    """
    from sqlmodel import select

    # Fetch all active companies with long-enough names
    companies = session.exec(
        select(Company).where(
            Company.is_active == True,
        )
    ).all()

    untagged = session.exec(
        select(NewsArticle).where(NewsArticle.ticker == None)
    ).all()

    count = 0
    for article in untagged:
        hl = article.headline.lower()
        for co in companies:
            name = co.company_name or ""
            if len(name) < 5:
                continue
            if name.lower() in hl or co.ticker.lower() in hl:
                article.ticker = co.ticker
                session.add(article)
                count += 1
                break  # first match wins

    return count


# ---------------------------------------------------------------------------
# System Metadata
# ---------------------------------------------------------------------------


def set_metadata(session: Session, key: str, value: str) -> SystemMetadata:
    existing = session.get(SystemMetadata, key)
    if existing is None:
        obj = SystemMetadata(key=key, value=value)
        session.add(obj)
        return obj
    existing.value = value
    existing.updated_at = datetime.utcnow()
    session.add(existing)
    return existing


def get_metadata(session: Session, key: str) -> str | None:
    obj = session.get(SystemMetadata, key)
    return obj.value if obj else None


# ---------------------------------------------------------------------------
# Convenience helpers used by multiple ingestion scripts
# ---------------------------------------------------------------------------


def get_active_tickers(session: Session) -> list[str]:
    """Return all active tickers. REQ-071: ingestion scripts filter is_active = 1."""
    rows = session.exec(
        select(Company).where(Company.is_active == True)
    ).all()
    return [r.ticker for r in rows]


def mark_company_onboarding_status(session: Session, ticker: str, status: str) -> None:
    """Update onboarding_status for a ticker. status ∈ {PENDING, COMPLETE, FAILED, STALE}."""
    co = session.get(Company, ticker)
    if co:
        co.onboarding_status = status
        _set_last_updated(co)
        session.add(co)


def write_agent_json_output(ticker: str, agent_name: str, payload: dict) -> str:
    """
    REQ-072: write full agent output JSON to output/{agent_name}/{ticker}_YYYYMMDD.json.
    Returns the path written.
    """
    output_dir = os.environ.get("OUTPUT_DIR", "output")
    agent_dir = os.path.join(output_dir, agent_name)
    os.makedirs(agent_dir, exist_ok=True)
    filename = f"{ticker}_{date.today().strftime('%Y%m%d')}.json"
    path = os.path.join(agent_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    log.debug("REQ-072: wrote agent output to %s", path)
    return path
