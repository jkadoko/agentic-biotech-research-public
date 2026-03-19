"""
SQLModel table definitions for biotech_tracker.db (SQLite WAL).

Source of truth: docs/SCHEMA.md v3.5s
All upserts must comply with REQ-004: NULL values MUST NOT overwrite existing non-NULL values.
All agent output tables comply with REQ-072: dual-write to SQLite + output/{agent}/{ticker}_YYYYMMDD.json.

Usage:
    from src.db.models import init_db, engine
    init_db()  # creates all tables and enables WAL mode
"""

import os
from datetime import date, datetime
from typing import Optional

from sqlalchemy import event, text
from sqlmodel import Field, SQLModel, create_engine

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "biotech_tracker.db")
_connect_args = {"check_same_thread": False}
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args=_connect_args, echo=False)


@event.listens_for(engine, "connect")
def _set_wal_mode(dbapi_conn, _):
    """Enable WAL mode and foreign keys on every new connection."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db() -> None:
    """Create all tables if they do not already exist."""
    SQLModel.metadata.create_all(engine)


# ---------------------------------------------------------------------------
# 1. Core Reference Tables
# ---------------------------------------------------------------------------


class Company(SQLModel, table=True):
    """Master company registry. PK: ticker."""

    __tablename__ = "companies"

    ticker: str = Field(primary_key=True)
    company_name: Optional[str] = None
    exchange: Optional[str] = None          # NASDAQ, NYSE
    sector: Optional[str] = None            # e.g., Biotechnology
    cik: Optional[str] = None               # SEC EDGAR Central Index Key
    market_cap_usd: Optional[float] = None
    price_current: Optional[float] = None
    shares_outstanding: Optional[int] = None
    total_cash_usd: Optional[float] = None
    total_debt_usd: Optional[float] = None
    annual_revenue_usd: Optional[float] = None
    cash_per_share: Optional[float] = None  # Derived nightly: total_cash_usd / shares_outstanding
    book_value_per_share: Optional[float] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    # REQ-066: MAX(cash_per_share, book_value_per_share, 52wk_low × 0.90)
    floor_price: Optional[float] = None
    runway_months: Optional[int] = None
    burn_rate_monthly_usd: Optional[float] = None
    listing_date: Optional[date] = None
    added_by: Optional[str] = None          # MANUAL | SCOUT_AUTO
    is_active: Optional[bool] = Field(default=True)   # REQ-071
    last_10k_parsed: Optional[date] = None
    onboarding_status: Optional[str] = None  # PENDING | COMPLETE | FAILED | STALE
    watchlist_flags: Optional[str] = None   # JSON array of {flag, source, headline, source_url, detected_at}
    last_updated: Optional[datetime] = Field(default_factory=datetime.utcnow)


class HistoricalPrice(SQLModel, table=True):
    """Daily OHLCV price history. PK: (ticker, date)."""

    __tablename__ = "historical_prices"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    price_date: date = Field(primary_key=True, sa_column_kwargs={"name": "date"})
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[int] = None


class OptionsChain(SQLModel, table=True):
    """Live options data from E*TRADE. PK: (ticker, expiration, strike, option_type)."""

    __tablename__ = "options_chains"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    expiration: date = Field(primary_key=True)
    strike: float = Field(primary_key=True)
    option_type: str = Field(primary_key=True)   # CALL | PUT
    iv: Optional[float] = None                   # Implied volatility — Volatility agent Step 2
    oi: Optional[int] = None                     # Open interest — Volatility agent Step 5: OI ≥ 500
    bid: Optional[float] = None
    ask: Optional[float] = None
    last_updated: Optional[datetime] = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# 2. Clinical Trials Pipeline (AACT + CT.gov API v2)
# ---------------------------------------------------------------------------


class Study(SQLModel, table=True):
    """Trial lifecycle data sourced from AACT nightly CSVs. PK: nct_id."""

    __tablename__ = "studies"

    nct_id: str = Field(primary_key=True)
    title: Optional[str] = None
    phase: Optional[str] = None              # Phase 1 | Phase 2 | Phase 3 | Phase 4
    status: Optional[str] = None            # RECRUITING | ACTIVE_NOT_RECRUITING | COMPLETED | ...
    # REQ-029: INTERVENTIONAL | OBSERVATIONAL | EXPANDED_ACCESS
    study_type: Optional[str] = None
    start_date: Optional[date] = None
    primary_completion_date: Optional[date] = None
    enrollment: Optional[int] = None
    enrollment_type: Optional[str] = None   # 'Actual' | 'Anticipated' (verbatim from AACT)
    # REQ-074: 1 if enrollment_type = 'Actual'; TAM queries must filter WHERE enrollment_is_actual = 1
    enrollment_is_actual: Optional[bool] = None
    lead_sponsor: Optional[str] = None      # Raw, pre-resolution
    lead_sponsor_class: Optional[str] = None  # INDUSTRY | NIH | FED | NETWORK | OTHER


class TrialPipeline(SQLModel, table=True):
    """Ticker-to-trial linkage. PK: (ticker, nct_id)."""

    __tablename__ = "trial_pipeline"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    nct_id: str = Field(primary_key=True, foreign_key="studies.nct_id")
    # 10K_CITED > DRUG_NAME_MATCH > COMPANY_NAME_MATCH > ENTITY_RESOLVED
    relationship_type: Optional[str] = None


class Condition(SQLModel, table=True):
    """Trial disease targets from AACT conditions.txt. PK: (nct_id, condition_name)."""

    __tablename__ = "conditions"

    nct_id: str = Field(primary_key=True, foreign_key="studies.nct_id")
    condition_name: str = Field(primary_key=True)
    condition_normalized: Optional[str] = None   # Scout abbrev expansion


class Intervention(SQLModel, table=True):
    """Drug/asset registry from AACT + onboarding. No composite PK — surrogate via nct_id + drug_name."""

    __tablename__ = "interventions"

    nct_id: str = Field(primary_key=True, foreign_key="studies.nct_id")
    drug_name: str = Field(primary_key=True)
    ticker: Optional[str] = Field(default=None, foreign_key="companies.ticker")
    indication: Optional[str] = None
    mechanism_of_action: Optional[str] = None
    # DRUG | BIOLOGICAL | DEVICE | PROCEDURE | OTHER
    intervention_type: Optional[str] = None
    # REQ-086: NULL for investigational drugs
    orange_book_appl_no: Optional[str] = None
    purple_book_bla_no: Optional[str] = None
    patent_expiry: Optional[date] = None


class Collaborator(SQLModel, table=True):
    """Industry co-sponsors from AACT collaborators.txt. No separate PK needed — (nct_id, collaborator_name) uniqueness."""

    __tablename__ = "collaborators"

    nct_id: str = Field(primary_key=True, foreign_key="studies.nct_id")
    collaborator_name: str = Field(primary_key=True)
    # INDUSTRY | NIH | NETWORK | OTHER
    collaborator_class: Optional[str] = None


class DesignOutcome(SQLModel, table=True):
    """Trial endpoint registry from AACT design_outcomes.txt. PK: (nct_id, outcome_type, measure)."""

    __tablename__ = "design_outcomes"

    nct_id: str = Field(primary_key=True, foreign_key="studies.nct_id")
    outcome_type: str = Field(primary_key=True)    # 'primary' | 'secondary'
    # REQ-075: normalize via data/endpoint_synonyms.csv before frequency analysis
    measure: str = Field(primary_key=True)


# ---------------------------------------------------------------------------
# 3. Entity Resolution
# ---------------------------------------------------------------------------


class EntityAlias(SQLModel, table=True):
    """Sponsor-to-ticker resolution cache. PK: alias. Pre-seeded with 25 critical mappings."""

    __tablename__ = "entity_aliases"

    alias: str = Field(primary_key=True)    # Raw sponsor name from CT.gov / AACT
    canonical_name: Optional[str] = None    # Resolved company name
    ticker: Optional[str] = None           # Resolved public ticker
    # EXACT_MATCH | SUBSIDIARY | PRIVATE | ACADEMIC | GOVERNMENT | FOREIGN | UNRESOLVED
    relationship_type: Optional[str] = None
    acquisition_date: Optional[date] = None
    confidence: Optional[float] = None     # 0.0–1.0
    # ALIAS_TABLE_EXACT | FUZZY_MATCH | WEB_SEARCH_CONFIRMED | WEB_SEARCH_PRIVATE_CONFIRMED
    resolution_method: Optional[str] = None
    source_url: Optional[str] = None
    created_date: Optional[date] = None
    last_verified_date: Optional[date] = None


# ---------------------------------------------------------------------------
# 4. Disease Epidemiology
# ---------------------------------------------------------------------------


class DiseaseContext(SQLModel, table=True):
    """Patient population data for TAM / market sizing. PK: condition_normalized."""

    __tablename__ = "disease_context"

    condition_normalized: str = Field(primary_key=True)
    condition_raw: Optional[str] = None
    prevalence_us: Optional[int] = None        # REQ-070: is_orphan = true if < 200,000
    prevalence_global: Optional[int] = None
    is_orphan: Optional[bool] = None           # Derived: prevalence_us < 200000
    mortality_rate: Optional[str] = None
    data_tier: Optional[int] = None            # 1=DB cache, 2=GBD 2019 CSV, 3=WHO GHO API, 4=Ollama batch
    source: Optional[str] = None
    last_updated: Optional[date] = None


# ---------------------------------------------------------------------------
# 5. FDA Datasets
# ---------------------------------------------------------------------------


class Orphan(SQLModel, table=True):
    """FDA orphan drug designations. PK: (ticker, drug_name)."""

    __tablename__ = "orphan"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    drug_name: str = Field(primary_key=True)
    indication: Optional[str] = None
    orphan_designation_date: Optional[date] = None
    exclusivity_expiry: Optional[date] = None


# ---------------------------------------------------------------------------
# 6. SEC Filings
# ---------------------------------------------------------------------------


class SecFiling(SQLModel, table=True):
    """Filing registry. PK: (ticker, filing_date, filing_type)."""

    __tablename__ = "sec_filings"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    filing_date: date = Field(primary_key=True)
    filing_type: str = Field(primary_key=True)  # 10-K | 10-Q | 8-K | Form 4 | SC 13G | SC 13D
    edgar_url: Optional[str] = None
    local_rag_source_id: Optional[str] = None   # ChromaDB source ID
    uploaded_to_rag: Optional[bool] = Field(default=False)


# ---------------------------------------------------------------------------
# 7. Ticker-First Onboarding
# ---------------------------------------------------------------------------


class CompanyOnboardingLog(SQLModel, table=True):
    """Onboarding audit trail. PK: (ticker, onboarding_date)."""

    __tablename__ = "company_onboarding_log"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    onboarding_date: datetime = Field(primary_key=True, default_factory=datetime.utcnow)
    # MANUAL_TICKER | SCOUT_IPO | STALE_REFRESH
    trigger_source: Optional[str] = None
    sec_edgar_url: Optional[str] = None
    filing_date: Optional[date] = None
    drugs_extracted: Optional[int] = None        # REQ-084: > 0 or flag LOW confidence
    nct_ids_cited: Optional[int] = None
    trials_linked: Optional[int] = None
    orphan_lookups: Optional[int] = None
    # HIGH (>3 drugs) | MEDIUM (1–3) | LOW (0 drugs)
    extraction_confidence: Optional[str] = None
    # SUCCESS | PARTIAL | FAILED
    status: Optional[str] = None
    error_notes: Optional[str] = None


# ---------------------------------------------------------------------------
# 8. Agent Output Tables
# ---------------------------------------------------------------------------


class AgentProfilerFinding(SQLModel, table=True):
    """Profiler (Agent 003) output. PK: (ticker, profile_date). REQ-072."""

    __tablename__ = "agent_profiler_findings"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    profile_date: date = Field(primary_key=True)
    profiler_score: Optional[int] = None        # 0–100
    management_score: Optional[int] = None
    tam_estimate_usd: Optional[float] = None
    # REQ-023: Σ(market_size_usd × POS) — Phase POS: P1=15%, P2=30%, P3=60%, NDA=90%
    rnpv_usd: Optional[float] = None
    # REQ-015: LOW (0–2) | MODERATE (3–5) | HIGH (≥6)
    competition_score: Optional[str] = None
    # REQ-026: THERAPEUTIC | PLATFORM
    company_type: Optional[str] = None
    # LOW | ELEVATED | CRITICAL
    patent_cliff_risk: Optional[str] = None
    # SUPERIOR | COMPETITIVE | INFERIOR | FIRST_MOVER
    competitive_advantage: Optional[str] = None
    kill_switch: Optional[bool] = Field(default=False)
    # BANKRUPTCY_IMMINENT | MANAGEMENT_FRAUD
    kill_switch_reason: Optional[str] = None
    full_json: Optional[str] = None             # REQ-072: complete profiler output JSON
    uploaded_to_rag: Optional[bool] = Field(default=False)


class AgentScientificAudit(SQLModel, table=True):
    """Peer Reviewer (Agent 004) output. PK: (ticker, nct_id, audit_date). REQ-072."""

    __tablename__ = "agent_scientific_audits"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    nct_id: str = Field(primary_key=True, foreign_key="studies.nct_id")
    audit_date: date = Field(primary_key=True)
    condition: Optional[str] = None
    # PHASE1 | PHASE2 | PHASE3
    phase: Optional[str] = None
    validity_score: Optional[int] = None
    # STRONG_SCIENCE | SOLID | WEAK | VERY_WEAK | FRAUD_RISK
    verdict: Optional[str] = None
    endpoint_switching_detected: Optional[bool] = Field(default=False)
    # SUPERIOR | FIRST_MOVER | COMPETITIVE | INFERIOR
    competitive_advantage: Optional[str] = None
    red_flags: Optional[str] = None             # JSON list of {phrase, penalty}
    full_json: Optional[str] = None
    uploaded_to_rag: Optional[bool] = Field(default=False)


class AgentInsiderFinding(SQLModel, table=True):
    """Insider Activity Tracker (Agent 005) output. PK: (ticker, scan_date). REQ-072."""

    __tablename__ = "agent_insider_findings"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    scan_date: date = Field(primary_key=True)
    # CLUSTER_BUY | STRONG_BUY | SINGLE_BUY | NEUTRAL | SINGLE_DISCRETIONARY_SELL | CLUSTER_DISCRETIONARY_SELL
    signal: Optional[str] = None
    conviction_score: Optional[int] = None      # 1–10
    total_open_market_buys: Optional[int] = None
    total_discretionary_sells: Optional[int] = None
    analysis_summary: Optional[str] = None
    full_json: Optional[str] = None
    uploaded_to_rag: Optional[bool] = Field(default=False)


class Catalyst(SQLModel, table=True):
    """Catalyst calendar (Oracle, Agent 008). PK: (ticker, event_type, event_date)."""

    __tablename__ = "catalysts"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    # PDUFA_DATE | ADCOMM_DATE | DATA_READOUT | CONFERENCE | INTERIM_ANALYSIS
    event_type: str = Field(primary_key=True)
    event_date: date = Field(primary_key=True)
    event_name: Optional[str] = None
    drug_name: Optional[str] = None
    indication: Optional[str] = None
    # HIGH | MEDIUM-HIGH | MEDIUM | LOW
    date_confidence: Optional[str] = None
    date_note: Optional[str] = None
    # PRIORITY (6-month) | STANDARD (10-month) — PDUFA only
    review_type: Optional[str] = None
    # ORAL | LATE_BREAKING | POSTER_DISCUSSION | POSTER — conference only
    presentation_type: Optional[str] = None
    market_impact_score: Optional[int] = None   # 1–10
    source_url: Optional[str] = None
    # PDUFA_CALENDAR | 8K_FILING | CONFERENCE_ABSTRACT | COMPANY_GUIDANCE | RSS_NEWS
    source_type: Optional[str] = None
    scan_date: Optional[date] = None


class AgentVolatilityFinding(SQLModel, table=True):
    """CSP Recommendations (Agent 009). PK: (ticker, scan_date). REQ-072."""

    __tablename__ = "agent_volatility_findings"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    scan_date: date = Field(primary_key=True)
    # APPROVED | REJECTED | CAUTION
    status: Optional[str] = None
    # STEP1_FLOOR … STEP6_RETURN; NULL if APPROVED
    rejection_step: Optional[str] = None
    rejection_reason: Optional[str] = None
    calculated_floor: Optional[float] = None
    extended_floor: Optional[float] = None      # floor × 1.25 if IV>80% + runway>18m + ≥3 assets
    selected_strike: Optional[float] = None
    selected_expiration: Optional[date] = None
    premium_mid: Optional[float] = None
    absolute_return_pct: Optional[float] = None
    annualized_return_pct: Optional[float] = None   # preferred ≥ 18%
    open_interest: Optional[int] = None
    iv_pct: Optional[float] = None
    # PDUFA_DRIVEN | CONFERENCE_DRIVEN | UNKNOWN
    iv_source: Optional[str] = None
    iv_leakage_warning: Optional[bool] = Field(default=False)
    next_catalyst_date: Optional[date] = None
    next_catalyst_within_window: Optional[bool] = Field(default=False)
    risk_warnings: Optional[str] = None         # JSON list of warning strings
    full_json: Optional[str] = None


class Partnership(SQLModel, table=True):
    """Commercial partnerships (Agent 010). PK: (ticker, partner_name, drug_asset)."""

    __tablename__ = "partnerships"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    partner_name: str = Field(primary_key=True)
    drug_asset: str = Field(primary_key=True)
    partner_ticker: Optional[str] = None
    partner_tier: Optional[int] = None          # 1=Major Pharma, 2=Mid-tier, 3=Technology
    # CO_DEVELOPMENT | LICENSING | CO_PROMOTION | OPTION_TO_ACQUIRE | TECHNOLOGY | ACADEMIC
    partnership_type: Optional[str] = None
    # OUT (company is licensor — positive) | IN (company is licensee — FIPCO risk)
    direction: Optional[str] = None
    indication: Optional[str] = None
    deal_date: Optional[date] = None
    upfront_usd: Optional[int] = None
    milestone_usd: Optional[int] = None
    # REQ-025: snapshotted at write time; prevents ratio drift as market cap changes post-deal
    market_cap_at_deal_usd: Optional[int] = None
    quality_score: Optional[int] = None
    # ACTIVE | TERMINATED | PENDING | ACQUISITION_COMPLETED
    status: Optional[str] = None
    # HIGH (≥2 sources) | MEDIUM (one source) | LOW (LLM only)
    confidence: Optional[str] = None
    # SEC_10K | CLINICALTRIALS | SEC_10K_AND_CLINICALTRIALS | RSS_NEWS
    source_type: Optional[str] = None
    source_url: Optional[str] = None
    scan_date: Optional[date] = None


class AgentSmartMoneyFinding(SQLModel, table=True):
    """Institutional signal summary (Agent 011). PK: (ticker, scan_date). REQ-072."""

    __tablename__ = "agent_smart_money_findings"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    scan_date: date = Field(primary_key=True)
    # SPECIALIST_CLUSTER | ACTIVIST_PLUS_SPECIALIST | SPECIALIST_NEW | INDEX_ONLY | REDUCTION | EXIT
    signal: Optional[str] = None
    conviction_score: Optional[int] = None      # 1–10
    top_institution: Optional[str] = None
    top_institution_pct: Optional[float] = None
    price_drift_pct: Optional[float] = None
    already_priced_in: Optional[bool] = Field(default=False)   # drift > 20%
    conflicting_signal: Optional[bool] = Field(default=False)
    analysis_summary: Optional[str] = None
    full_json: Optional[str] = None
    uploaded_to_rag: Optional[bool] = Field(default=False)


class SmartMoneyPosition(SQLModel, table=True):
    """Raw 13G/13D positions (Agent 011). PK: (ticker, institution_name, filing_date)."""

    __tablename__ = "smart_money_positions"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    institution_name: str = Field(primary_key=True)
    filing_date: date = Field(primary_key=True)
    # SC_13G | SC_13G_A | SC_13D | SC_13D_A
    filing_type: Optional[str] = None
    shares: Optional[int] = None
    pct_of_class: Optional[float] = None
    is_specialist: Optional[bool] = Field(default=False)


class AgentInvestmentMemo(SQLModel, table=True):
    """Investment memos (Strategist, Agent 006). PK: (ticker, memo_date). REQ-072."""

    __tablename__ = "agent_investment_memos"

    ticker: str = Field(primary_key=True, foreign_key="companies.ticker")
    memo_date: date = Field(primary_key=True)
    biotech_alpha_score: Optional[int] = None   # 0–100 BAS
    # CSP_CANDIDATE | MOONSHOT | DEEP_VALUE | HOLD | AVOID
    primary_recommendation: Optional[str] = None
    secondary_recommendation: Optional[str] = None
    tags: Optional[str] = None                  # JSON array e.g. ["#CSP_CANDIDATE", "#ORPHAN"]
    strategy_fit: Optional[str] = None          # JSON: per-strategy ELIGIBLE/INELIGIBLE
    risk_factors: Optional[str] = None          # JSON: key risk bullets
    kill_switch: Optional[bool] = Field(default=False)
    # BANKRUPTCY_IMMINENT | MANAGEMENT_FRAUD | FRAUD_RISK
    kill_switch_reason: Optional[str] = None
    full_json: Optional[str] = None
    uploaded_to_rag: Optional[bool] = Field(default=False)


# ---------------------------------------------------------------------------
# 9. News Ingestion
# ---------------------------------------------------------------------------


class NewsArticle(SQLModel, table=True):
    """RSS-sourced headlines (REQ-087–REQ-091). PK: id (auto-increment). URL is UNIQUE."""

    __tablename__ = "news_articles"

    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: Optional[str] = Field(default=None, foreign_key="companies.ticker")
    headline: str
    source: str                             # biopharmadive | fiercepharma | endpoints | statnews
    url: str = Field(sa_column_kwargs={"unique": True})   # REQ-090: dedup key; INSERT OR IGNORE
    published_at: datetime
    # REQ-089: m&a | conference | analyst | partnership | fda | NULL
    category: Optional[str] = None
    fetched_at: Optional[datetime] = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# 10. System Metadata
# ---------------------------------------------------------------------------


class SystemMetadata(SQLModel, table=True):
    """Scheduler state store. PK: key.

    Keys:
      aact_last_sync           — ISO timestamp of last successful AACT upsert
      aact_studies_prev_count  — Row count from previous run (REQ-079: >10% drop = alert)
      last_news_prune          — ISO timestamp of last news_articles 90-day pruning
    """

    __tablename__ = "system_metadata"

    key: str = Field(primary_key=True)
    value: str
    updated_at: Optional[datetime] = Field(default_factory=datetime.utcnow)
