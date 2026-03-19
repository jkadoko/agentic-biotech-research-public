# Biotech Investment Analyzer — Database Schema & Variable Reference

**Version:** 3.5s
**Last Updated:** 2026-03-15
**Aligned with:** PRD v3.5y, CREWAI_TOOLS.md v2.0
**Supersedes:** SCHEMA.md v16.5 (legacy 23-agent schema)

All tables reside in a single SQLite WAL-mode database: `biotech_tracker.db`.
This document is the authoritative variable reference for all agents, ingestion scripts, and the Streamlit UI.
The canonical SQL definitions live in `src/db/models.py` (SQLModel).

---

## Core Architecture Principles

- **Single database:** All agents read from and write to `biotech_tracker.db`. No split-brain between PostgreSQL and SQLite.
- **WAL mode:** Eliminates read-write lock contention between the Streamlit UI (reader) and APScheduler jobs (writers).
- **REQ-004:** NULL incoming values MUST NOT overwrite existing non-NULL values during any upsert operation.
- **REQ-072:** All agent outputs are dual-written: (1) structured rows to the appropriate table below, (2) full JSON to `output/{agent_name}/{ticker}_{YYYYMMDD}.json`.

---

## 1. Core Reference Tables

### `companies` — Master Company Registry

| Column | Type | Source | Usage |
|--------|------|--------|-------|
| `ticker` | TEXT PK | User / Scout | Primary equity identifier |
| `company_name` | TEXT | yfinance / 10-K | Human-readable name |
| `exchange` | TEXT | yfinance | NASDAQ, NYSE |
| `sector` | TEXT | yfinance | e.g., Biotechnology |
| `cik` | TEXT | SEC EDGAR | Central Index Key — required for 10-K/8-K/Form 4 fetching via EDGAR |
| `market_cap_usd` | REAL | yfinance / E*TRADE | Current market capitalization |
| `price_current` | REAL | E*TRADE Quote API | Latest trade price |
| `shares_outstanding` | INTEGER | yfinance Fundamentals | Used to compute `cash_per_share = total_cash_usd / shares_outstanding` |
| `total_cash_usd` | REAL | yfinance Fundamentals | Gross cash + equivalents (pre-debt); feeds runway and floor price calculations |
| `total_debt_usd` | REAL | yfinance Fundamentals | Total debt; used for EV and solvency checks |
| `annual_revenue_usd` | REAL | yfinance / SEC 10-K | Trailing 12m revenue; REQ-026 THERAPEUTIC vs PLATFORM classification depends on `> 0` |
| `cash_per_share` | REAL | Derived nightly | `total_cash_usd / shares_outstanding`; input to REQ-066 floor price |
| `book_value_per_share` | REAL | yfinance Fundamentals | Input to REQ-066 floor price |
| `52wk_high` | REAL | E*TRADE / yfinance | 52-week high price |
| `52wk_low` | REAL | E*TRADE / yfinance | 52-week low price; input to REQ-066 floor price (`52wk_low × 0.90`) |
| `floor_price` | REAL | Derived nightly | **REQ-066:** `MAX(cash_per_share, book_value_per_share, 52wk_low × 0.90)` — canonical floor for Volatility agent |
| `runway_months` | INTEGER | Derived | `total_cash_usd / burn_rate_monthly_usd`; updated nightly |
| `burn_rate_monthly_usd` | REAL | SEC 10-K / Profiler | Monthly cash burn extracted during onboarding Step 4 |
| `listing_date` | DATE | yfinance | IPO or listing date |
| `added_by` | TEXT | System | `MANUAL` (user entry) or `SCOUT_AUTO` (Scout Task A discovery) |
| `is_active` | BOOLEAN DEFAULT 1 | Admin | **REQ-071:** 0 for delisted/inactive tickers; all ingestion scripts filter `WHERE is_active = 1` |
| `last_10k_parsed` | DATE | Onboarding pipeline | Date of most recent 10-K extraction run |
| `onboarding_status` | TEXT | Onboarding pipeline | `PENDING` / `COMPLETE` / `FAILED` / `STALE` (triggered when new 10-K detected via RSS) |
| `watchlist_flags` | JSON | Scout Task E / System | Array of active alert objects (e.g., `MA_RUMOR_FLAG`). Each entry: `{flag, source, headline, source_url, detected_at}`. Scout Task E writes here; entries auto-expire after 30 days if no confirming 8-K found. Displayed in Streamlit as amber alert badges. |
| `last_updated` | TIMESTAMP | System | Timestamp of most recent row update |

**Derived / not stored** (computed at query time): `negative_ev = (total_cash_usd > market_cap_usd + total_debt_usd)`

---

### `historical_prices` — Daily Price History

| Column | Type | Source |
|--------|------|--------|
| `ticker` | TEXT | FK → companies |
| `date` | DATE | yfinance |
| `open`, `high`, `low`, `close` | REAL | yfinance |
| `volume` | INTEGER | yfinance |

**PK:** `(ticker, date)`

---

### `options_chains` — Live Options Data

| Column | Type | Source | Usage |
|--------|------|--------|-------|
| `ticker` | TEXT | E*TRADE Options API | FK → companies |
| `expiration` | DATE | E*TRADE | Contract expiration |
| `strike` | REAL | E*TRADE | Strike price |
| `option_type` | TEXT | E*TRADE | `CALL` or `PUT` |
| `iv` | REAL | E*TRADE | Implied volatility — input to Volatility agent Step 2 |
| `oi` | INTEGER | E*TRADE | Open interest — Volatility agent Step 5 requires `OI ≥ 500` |
| `bid`, `ask` | REAL | E*TRADE | Bid/ask for spread check (Step 5: spread ≤ 10%) |
| `last_updated` | TIMESTAMP | System | Timestamp of last options refresh |

**PK:** `(ticker, expiration, strike, option_type)`

---

## 2. Clinical Trials Pipeline (AACT + CT.gov API v2)

### `studies` — Trial Lifecycle Data (from AACT)

| Column | Type | Source | Usage |
|--------|------|--------|-------|
| `nct_id` | TEXT PK | AACT / CT.gov API v2 | ClinicalTrials.gov identifier |
| `title` | TEXT | AACT | Study official title |
| `phase` | TEXT | AACT | `Phase 1`, `Phase 2`, `Phase 3`, `Phase 4`, etc. |
| `status` | TEXT | AACT | `RECRUITING`, `ACTIVE_NOT_RECRUITING`, `COMPLETED`, etc. |
| `study_type` | TEXT | AACT | **REQ-029:** `INTERVENTIONAL` \| `OBSERVATIONAL` \| `EXPANDED_ACCESS` — determines pipeline routing |
| `start_date` | DATE | AACT | Trial start date |
| `primary_completion_date` | DATE | AACT | Primary endpoint completion date (catalyst timeline input) |
| `enrollment` | INTEGER | AACT | Raw enrollment value (Actual or Anticipated — interpret via `enrollment_is_actual`) |
| `enrollment_type` | TEXT | AACT | `'Actual'` or `'Anticipated'` (verbatim from AACT `studies.enrollment_type`) |
| `enrollment_is_actual` | BOOLEAN | Derived at upsert | **REQ-074:** `1` if `enrollment_type = 'Actual'`. All TAM/market-sizing queries MUST filter `WHERE enrollment_is_actual = 1` |
| `lead_sponsor` | TEXT | AACT sponsors table | Lead sponsor name (raw, pre-resolution) |
| `lead_sponsor_class` | TEXT | AACT sponsors table | `INDUSTRY` \| `NIH` \| `FED` \| `NETWORK` \| `OTHER` |

---

### `trial_pipeline` — Ticker-to-Trial Linkage

| Column | Type | Source | Usage |
|--------|------|--------|-------|
| `ticker` | TEXT | Onboarding / Detective | FK → companies |
| `nct_id` | TEXT | Onboarding / AACT | FK → studies |
| `relationship_type` | TEXT | Onboarding pipeline | `10K_CITED` > `DRUG_NAME_MATCH` > `COMPANY_NAME_MATCH` > `ENTITY_RESOLVED` (priority order) |

**PK:** `(ticker, nct_id)`

---

### `conditions` — Trial Disease Targets (from AACT)

| Column | Type | Source |
|--------|------|--------|
| `nct_id` | TEXT | AACT |
| `condition_name` | TEXT | AACT (verbatim) |
| `condition_normalized` | TEXT | Scout condition normalization (abbrev expansion) |

**PK:** `(nct_id, condition_name)`

---

### `interventions` — Drug/Asset Registry (from AACT + Onboarding)

| Column | Type | Source | Usage |
|--------|------|--------|-------|
| `nct_id` | TEXT | AACT | FK → studies |
| `ticker` | TEXT | Onboarding Step 5 | FK → companies; links drug to its public company |
| `drug_name` | TEXT | AACT / 10-K extraction | Drug or compound name |
| `indication` | TEXT | 10-K extraction (Step 4) | Disease target; links drug to `disease_context` |
| `mechanism_of_action` | TEXT | 10-K extraction (Step 4) | e.g., `PD-1 inhibitor`, `CAR-T`, `mRNA vaccine` |
| `intervention_type` | TEXT | AACT | `DRUG`, `BIOLOGICAL`, `DEVICE`, `PROCEDURE`, `OTHER` |
| `orange_book_appl_no` | TEXT | FDA Orange Book (Step 7) | NDA application number for approved small molecules; NULL for investigational (REQ-086) |
| `purple_book_bla_no` | TEXT | FDA Purple Book (Step 7) | BLA number for approved biologics; NULL for investigational (REQ-086) |
| `patent_expiry` | DATE | Orange/Purple Book | Patent/exclusivity expiry; NULL for investigational drugs |

---

### `sponsors` — AACT Sponsors (Denormalized into `studies`)

**Note:** The AACT `sponsors.txt` file (one of the 6 ingested tables) is NOT stored as a separate SQLite table. Its data is denormalized at ingest time:
- Lead sponsor rows (`lead_or_collaborator = 'lead'`) update `studies.lead_sponsor` and `studies.lead_sponsor_class` — this is where REQ-034 (`agency_class = 'INDUSTRY'`) filter is applied.
- Industry collaborator rows (`lead_or_collaborator = 'collaborator'` AND `agency_class = 'INDUSTRY'`) are routed to the `collaborators` table below.
- Non-industry sponsors (NIH, FED, NETWORK, OTHER) are discarded at ingest per REQ-034.

**Why not a separate table:** Maintaining a separate `sponsors` table would require a JOIN to determine whether a trial has an industry lead — the `studies.lead_sponsor_class` column provides this in O(1) at query time.

---

### `collaborators` — Industry Co-Sponsors (from AACT)

| Column | Type | Usage |
|--------|------|-------|
| `nct_id` | TEXT | FK → studies |
| `collaborator_name` | TEXT | Used by Partnership agent: filter `WHERE collaborator_class = 'INDUSTRY'` |
| `collaborator_class` | TEXT | `INDUSTRY` \| `NIH` \| `NETWORK` \| `OTHER` |

---

### `design_outcomes` — Trial Endpoint Registry (from AACT)

| Column | Type | Usage |
|--------|------|-------|
| `nct_id` | TEXT | FK → studies |
| `outcome_type` | TEXT | `'primary'` or `'secondary'` |
| `measure` | TEXT | Free text — **REQ-075:** normalize via `data/endpoint_synonyms.csv` before any frequency analysis |

**PK:** `(nct_id, outcome_type, measure)`

---

## 3. Entity Resolution

### `entity_aliases` — Sponsor-to-Ticker Resolution Cache (Detective 001)

| Column | Type | Usage |
|--------|------|-------|
| `alias` | TEXT PK | Raw sponsor name from ClinicalTrials.gov / AACT |
| `canonical_name` | TEXT | Resolved company name (e.g., "Gilead Sciences") |
| `ticker` | TEXT | Resolved public ticker (e.g., "GILD") |
| `relationship_type` | TEXT | `EXACT_MATCH` \| `SUBSIDIARY` \| `PRIVATE` \| `ACADEMIC` \| `GOVERNMENT` \| `FOREIGN` \| `UNRESOLVED` |
| `acquisition_date` | DATE | Date of M&A event (if applicable) |
| `confidence` | REAL | 0.0–1.0 resolution confidence score |
| `resolution_method` | TEXT | `ALIAS_TABLE_EXACT` \| `FUZZY_MATCH` \| `WEB_SEARCH_CONFIRMED` \| `WEB_SEARCH_PRIVATE_CONFIRMED` |
| `source_url` | TEXT | SEC 8-K or press release URL confirming resolution |
| `created_date` | DATE | Date alias was first resolved |
| `last_verified_date` | DATE | Date alias was last re-confirmed |

Pre-seeded with 25 critical mappings (GILD/Kite, RHHBY/Genentech, BMY/Celgene, PFE/Seagen, AZN/Alexion, etc.). Grows with each new Detective resolution.

---

## 4. Disease Epidemiology

### `disease_context` — Patient Population Data (Scout 002)

| Column | Type | Source | Usage |
|--------|------|--------|-------|
| `condition_normalized` | TEXT PK | Scout normalization | Canonical disease name (abbrev expanded) |
| `condition_raw` | TEXT | AACT / CT.gov | Original string before normalization |
| `prevalence_us` | INTEGER | 4-tier waterfall | US patient population — `is_orphan = true` if < 200,000 (REQ-070) |
| `prevalence_global` | INTEGER | 4-tier waterfall | Global patient population |
| `is_orphan` | BOOLEAN | Derived | `true` if `prevalence_us < 200000` |
| `mortality_rate` | TEXT | GBD / WHO | Annual mortality rate string |
| `data_tier` | INTEGER | System | `1`=DB cache, `2`=GBD 2019 CSV, `3`=WHO GHO API, `4`=Ollama batch |
| `source` | TEXT | System | Source identifier |
| `last_updated` | DATE | System | Date last refreshed |

Read by Profiler for TAM calculation and Peer Reviewer for market size classification.

---

## 5. FDA Datasets

### `orphan` — FDA Orphan Drug Designations

| Column | Type | Source |
|--------|------|--------|
| `ticker` | TEXT | Linked during onboarding Step 7 |
| `drug_name` | TEXT | FDA Orphan Drug DB |
| `indication` | TEXT | FDA Orphan Drug DB |
| `orphan_designation_date` | DATE | FDA Orphan Drug DB |
| `exclusivity_expiry` | DATE | FDA Orphan Drug DB |

**PK:** `(ticker, drug_name)`

---

## 6. SEC Filings

### `sec_filings` — Filing Registry

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `filing_date` | DATE | Date filed with SEC |
| `filing_type` | TEXT | `10-K`, `10-Q`, `8-K`, `Form 4`, `SC 13G`, `SC 13D` |
| `edgar_url` | TEXT | EDGAR filing URL |
| `local_rag_source_id` | TEXT | ChromaDB source ID (written after embedding) |
| `uploaded_to_rag` | BOOLEAN | `true` after successful ChromaDB ingestion |

**PK:** `(ticker, filing_date, filing_type)`

---

## 7. Ticker-First Onboarding

### `company_onboarding_log` — Onboarding Audit Trail

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `onboarding_date` | TIMESTAMP | When the run executed |
| `trigger_source` | TEXT | `MANUAL_TICKER` \| `SCOUT_IPO` \| `STALE_REFRESH` |
| `sec_edgar_url` | TEXT | 10-K URL used for this run |
| `filing_date` | DATE | 10-K filing date |
| `drugs_extracted` | INTEGER | Count of drug names found — REQ-084: must be > 0 or flag LOW confidence |
| `nct_ids_cited` | INTEGER | Count of NCT IDs explicitly in 10-K text (highest confidence links) |
| `trials_linked` | INTEGER | Count of `trial_pipeline` rows created/updated across all three passes |
| `orphan_lookups` | INTEGER | Count of FDA orphan DB queries attempted |
| `extraction_confidence` | TEXT | `HIGH` (>3 drugs) \| `MEDIUM` (1–3) \| `LOW` (0 drugs) |
| `status` | TEXT | `SUCCESS` \| `PARTIAL` \| `FAILED` |
| `error_notes` | TEXT | Failure detail; surfaced in Streamlit manual review queue (REQ-084) |

**PK:** `(ticker, onboarding_date)`

---

## 8. Agent Output Tables

### `agent_profiler_findings` — Profiler (Agent 003)

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `profile_date` | DATE | Run date |
| `profiler_score` | INTEGER | 0–100 overall score |
| `management_score` | INTEGER | CEO track record, dilution history, insider ownership |
| `tam_estimate_usd` | REAL | Bottom-up TAM: `prevalence_us × annual_price × penetration` |
| `rnpv_usd` | REAL | **REQ-023:** `Σ(market_size_usd × POS)` across active trials. Phase POS: P1=15%, P2=30%, P3=60%, NDA=90% |
| `competition_score` | TEXT | **REQ-015:** `LOW` (0–2 competitors) \| `MODERATE` (3–5) \| `HIGH` (≥6) per indication in AACT `conditions` |
| `company_type` | TEXT | **REQ-026:** `THERAPEUTIC` or `PLATFORM` (PLATFORM excluded from Moonshot REQ-014) |
| `patent_cliff_risk` | TEXT | `LOW` \| `ELEVATED` \| `CRITICAL` based on exclusivity timeline |
| `competitive_advantage` | TEXT | `SUPERIOR` \| `COMPETITIVE` \| `INFERIOR` \| `FIRST_MOVER` |
| `kill_switch` | BOOLEAN | `true` if BANKRUPTCY_IMMINENT or MANAGEMENT_FRAUD |
| `kill_switch_reason` | TEXT | `BANKRUPTCY_IMMINENT` \| `MANAGEMENT_FRAUD` |
| `full_json` | TEXT | Complete profiler output JSON blob |
| `uploaded_to_rag` | BOOLEAN | `true` after ChromaDB sync |

**PK:** `(ticker, profile_date)`

---

### `agent_scientific_audits` — Peer Reviewer (Agent 004)

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `nct_id` | TEXT | FK → studies; one audit per trial |
| `audit_date` | DATE | Run date |
| `condition` | TEXT | Trial condition/indication (denormalized from `trial_pipeline` for query convenience) |
| `phase` | TEXT | Trial phase: `PHASE1` \| `PHASE2` \| `PHASE3` |
| `validity_score` | INTEGER | 0–100: 80–100=STRONG_SCIENCE, 60–79=SOLID, 40–59=WEAK, 20–39=VERY_WEAK, 0–19=FRAUD_RISK |
| `verdict` | TEXT | `STRONG_SCIENCE` \| `SOLID` \| `WEAK` \| `VERY_WEAK` \| `FRAUD_RISK` |
| `endpoint_switching_detected` | BOOLEAN | `true` if primary endpoint differs between registration and press release |
| `competitive_advantage` | TEXT | `SUPERIOR` \| `FIRST_MOVER` \| `COMPETITIVE` \| `INFERIOR` vs SoC |
| `red_flags` | JSON | List of spin phrases detected with penalty scores |
| `full_json` | TEXT | Complete audit output |
| `uploaded_to_rag` | BOOLEAN | After ChromaDB sync |

**PK:** `(ticker, nct_id, audit_date)`

---

### `agent_insider_findings` — Insider Activity Tracker (Agent 005)

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `scan_date` | DATE | Run date |
| `signal` | TEXT | `CLUSTER_BUY` \| `STRONG_BUY` \| `SINGLE_BUY` \| `NEUTRAL` \| `SINGLE_DISCRETIONARY_SELL` \| `CLUSTER_DISCRETIONARY_SELL` |
| `conviction_score` | INTEGER | 1–10 |
| `total_open_market_buys` | INTEGER | Count of Code P transactions in look-back window |
| `total_discretionary_sells` | INTEGER | Count of non-10b5-1 Code S transactions |
| `analysis_summary` | TEXT | LLM-generated narrative |
| `full_json` | TEXT | Complete output |
| `uploaded_to_rag` | BOOLEAN | After ChromaDB sync |

**PK:** `(ticker, scan_date)`

Only Code P (open-market purchase) transactions carry signal. Code A/D/F (RSU award/vesting/tax withholding) are excluded.

---

### `catalysts` — Catalyst Calendar (Oracle, Agent 008)

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `event_type` | TEXT | `PDUFA_DATE` \| `ADCOMM_DATE` \| `DATA_READOUT` \| `CONFERENCE` \| `INTERIM_ANALYSIS` |
| `event_name` | TEXT | Human-readable event description |
| `drug_name` | TEXT | Associated drug/asset |
| `indication` | TEXT | Target indication |
| `event_date` | DATE | Scheduled or estimated event date |
| `date_confidence` | TEXT | `HIGH` \| `MEDIUM-HIGH` \| `MEDIUM` \| `LOW` |
| `date_note` | TEXT | Guidance normalization note (e.g., "guided Q1 2026 → 2026-03-31") |
| `review_type` | TEXT | `PRIORITY` (6-month window) or `STANDARD` (10-month window) — PDUFA events only |
| `presentation_type` | TEXT | `ORAL` \| `LATE_BREAKING` \| `POSTER_DISCUSSION` \| `POSTER` — conference events only |
| `market_impact_score` | INTEGER | 1–10 (PDUFA=9, Phase 3 Readout=8, AdComm=7, Phase 2 Readout=5) |
| `source_url` | TEXT | SEC 8-K, FDA calendar, or conference abstract URL |
| `source_type` | TEXT | `PDUFA_CALENDAR` \| `8K_FILING` \| `CONFERENCE_ABSTRACT` \| `COMPANY_GUIDANCE` \| `RSS_NEWS` (from `news_articles` — pre-8K early signal) |
| `scan_date` | DATE | Date catalyst was detected |

**PK:** `(ticker, event_type, event_date)`

Consumed by: Volatility agent (catalyst within CSP window check), Strategist BAS catalyst timing component, Streamlit UI catalyst timeline.

---

### `agent_volatility_findings` — CSP Recommendations (Agent 009)

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `scan_date` | DATE | Run date |
| `status` | TEXT | `APPROVED` \| `REJECTED` \| `CAUTION` |
| `rejection_step` | TEXT | `STEP1_FLOOR` through `STEP6_RETURN`; NULL if APPROVED |
| `rejection_reason` | TEXT | Human-readable rejection explanation |
| `calculated_floor` | REAL | Floor price computed per REQ-066 |
| `extended_floor` | REAL | Floor × 1.25 (applies if IV > 80% AND runway > 18m AND ≥3 active assets) |
| `selected_strike` | REAL | Recommended put strike ≤ floor |
| `selected_expiration` | DATE | Recommended expiration (< 45 DTE) |
| `premium_mid` | REAL | Mid-market premium at selected strike |
| `absolute_return_pct` | REAL | `premium / strike` |
| `annualized_return_pct` | REAL | `(premium / strike) × (365 / DTE)` — preferred ≥ 18% |
| `open_interest` | INTEGER | OI at selected strike |
| `iv_pct` | REAL | Implied volatility at selected strike |
| `iv_source` | TEXT | `PDUFA_DRIVEN` \| `CONFERENCE_DRIVEN` \| `UNKNOWN` |
| `iv_leakage_warning` | BOOLEAN | `true` if high IV with no identified catalyst |
| `next_catalyst_date` | DATE | Next identified catalyst from `catalysts` table |
| `next_catalyst_within_window` | BOOLEAN | `true` if catalyst falls before expiration — triggers REJECT |
| `risk_warnings` | JSON | List of warning strings from all 6 safety steps |
| `full_json` | TEXT | Complete trade analysis |

**PK:** `(ticker, scan_date)`

---

### `partnerships` — Commercial Partnerships (Agent 010)

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `partner_name` | TEXT | Partner company name |
| `partner_ticker` | TEXT | Partner public ticker (if applicable) |
| `partner_tier` | INTEGER | `1`=Major Pharma (16 firms), `2`=Mid-tier, `3`=Technology |
| `partnership_type` | TEXT | `CO_DEVELOPMENT` \| `LICENSING` \| `CO_PROMOTION` \| `OPTION_TO_ACQUIRE` \| `TECHNOLOGY` \| `ACADEMIC` |
| `direction` | TEXT | `OUT` (company is licensor — positive) \| `IN` (company is licensee — FIPCO risk) |
| `drug_asset` | TEXT | Drug or platform being partnered |
| `indication` | TEXT | Target indication |
| `deal_date` | DATE | Agreement execution date |
| `upfront_usd` | BIGINT | Upfront payment in USD |
| `milestone_usd` | BIGINT | Total potential milestone payments |
| `market_cap_at_deal_usd` | BIGINT | Company market cap on `deal_date` — snapshotted at write time; prevents ratio calculation from drifting as market cap changes post-deal |
| `quality_score` | INTEGER | **REQ-025:** `+2` Tier 1, `+1` Tier 2/3, `+2` if `upfront_usd > 0.10 × market_cap_at_deal_usd` (uses historical market cap, not current), `−1` if `direction = IN` |
| `status` | TEXT | `ACTIVE` \| `TERMINATED` \| `PENDING` \| `ACQUISITION_COMPLETED` |
| `confidence` | TEXT | `HIGH` (≥2 sources confirmed) \| `MEDIUM` (one source) \| `LOW` (LLM only) |
| `source_type` | TEXT | `SEC_10K` \| `CLINICALTRIALS` \| `SEC_10K_AND_CLINICALTRIALS` \| `RSS_NEWS` (news-detected, pending 8-K confirmation) |
| `source_url` | TEXT | Source document URL |
| `scan_date` | DATE | Date partnership was detected |

**PK:** `(ticker, partner_name, drug_asset)`

`quality_score` feeds `bas_partnership_bonus` via Strategist SQL view.

---

### `agent_smart_money_findings` — Institutional Signal Summary (Agent 011)

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `scan_date` | DATE | Run date |
| `signal` | TEXT | `SPECIALIST_CLUSTER` \| `ACTIVIST_PLUS_SPECIALIST` \| `SPECIALIST_NEW` \| `INDEX_ONLY` \| `REDUCTION` \| `EXIT` |
| `conviction_score` | INTEGER | 1–10 aggregate across all filings |
| `top_institution` | TEXT | Highest-conviction filer for this ticker |
| `top_institution_pct` | REAL | Top filer's `% of class` |
| `price_drift_pct` | REAL | `(current_price - price_at_filing) / price_at_filing` |
| `already_priced_in` | BOOLEAN | `true` if drift > 20% — Strategist REQ-024 will not recommend entry |
| `conflicting_signal` | BOOLEAN | `true` if analyst downgrade in `news_articles` (category='analyst') within 7 days of a new specialist position — Smart Money Step 6 |
| `analysis_summary` | TEXT | LLM narrative |
| `full_json` | TEXT | Complete output |
| `uploaded_to_rag` | BOOLEAN | After ChromaDB sync |

**PK:** `(ticker, scan_date)`

---

### `smart_money_positions` — Raw 13G/13D Positions (Agent 011)

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `institution_name` | TEXT | Filing institution |
| `filing_type` | TEXT | `SC_13G` \| `SC_13G_A` \| `SC_13D` \| `SC_13D_A` |
| `filing_date` | DATE | SEC filing date |
| `shares` | INTEGER | Shares held as reported |
| `pct_of_class` | REAL | % of outstanding shares (from filing) |
| `is_specialist` | BOOLEAN | `true` if institution in pre-loaded 13-fund specialist list |

**PK:** `(ticker, institution_name, filing_date)`

Used to compute: `position_value_usd = shares × companies.price_current`

---

### `agent_investment_memos` — Strategy Recommendations (Strategist, Agent 006)

| Column | Type | Usage |
|--------|------|-------|
| `ticker` | TEXT | FK → companies |
| `memo_date` | DATE | Run date |
| `biotech_alpha_score` | INTEGER | 0–100 BAS composite score |
| `primary_recommendation` | TEXT | e.g., `CSP_CANDIDATE`, `MOONSHOT`, `DEEP_VALUE`, `HOLD`, `AVOID` |
| `secondary_recommendation` | TEXT | Optional secondary strategy |
| `tags` | JSON | Array of hashtag labels (e.g., `["#CSP_CANDIDATE", "#ORPHAN"]`) |
| `strategy_fit` | JSON | Per-strategy `ELIGIBLE`/`INELIGIBLE` with reasons |
| `risk_factors` | JSON | Key risk bullets from Profiler + Peer Reviewer |
| `kill_switch` | BOOLEAN | `true` if any kill switch triggered |
| `kill_switch_reason` | TEXT | `BANKRUPTCY_IMMINENT` \| `MANAGEMENT_FRAUD` \| `FRAUD_RISK` |
| `full_json` | TEXT | Complete 8-agent briefing packet + memo |
| `uploaded_to_rag` | BOOLEAN | After ChromaDB sync — feeds longitudinal memory loop |

**PK:** `(ticker, memo_date)`

---

## 9. News Ingestion

### `news_articles` — RSS-Sourced Industry Headlines (REQ-087–REQ-091)

| Column | Type | Source | Usage |
|--------|------|--------|-------|
| `id` | INTEGER PK | System | Auto-increment |
| `ticker` | TEXT | Post-fetch association pass | FK → companies; NULL until company name matched against headline (REQ-088). Excluded from association pass if `company_name` < 5 characters. |
| `headline` | TEXT NOT NULL | RSS feed | Article title |
| `source` | TEXT NOT NULL | System | `biopharmadive` \| `fiercepharma` \| `endpoints` \| `statnews` |
| `url` | TEXT UNIQUE | RSS feed | Canonical article URL — deduplication key; `INSERT OR IGNORE` (REQ-090) |
| `published_at` | DATETIME NOT NULL | RSS `<pubDate>` | Article publication timestamp |
| `category` | TEXT | Derived at insert (REQ-089) | `m&a` \| `conference` \| `analyst` \| `partnership` \| `fda` \| NULL (unmatched) |
| `fetched_at` | DATETIME DEFAULT CURRENT_TIMESTAMP | System | When the row was inserted |

**Indexes:** `(ticker, published_at)`, `(category, published_at)`

**Categorization regex** (applied to `headline` at insert time):

| Pattern | Category |
|---------|----------|
| `acqui\|merger\|buy.*out\|takeover` | `m&a` |
| `ASCO\|ASH\|JPM\|AACR\|ESMO\|EHA` | `conference` |
| `upgrade\|downgrade\|price target\|initiates` | `analyst` |
| `partnership\|collaboration\|license\|royalt` | `partnership` |
| `FDA\|PDUFA\|NDA\|BLA\|accelerated approval` | `fda` |

**Pruning:** Weekly (Sunday 02:00) — rows older than 90 days deleted (REQ-091).

**Consumed by:** Oracle (conference + fda categories), Scout (m&a category), Partnership (partnership category), Smart Money (analyst category).

---

## 10. System Metadata

### `system_metadata` — Scheduler State Store

Simple key-value store for scheduler state and sync timestamps. Used by ingestion scripts to detect anomalous row-count drops (REQ-079) and to avoid redundant full re-syncs.

| Column | Type | Notes |
|--------|------|-------|
| `key` | TEXT PK | Identifier (e.g., `aact_last_sync`, `last_news_prune`, `aact_studies_prev_count`) |
| `value` | TEXT NOT NULL | Value as string (ISO timestamps, counts) |
| `updated_at` | TIMESTAMP DEFAULT CURRENT_TIMESTAMP | Last write time |

**SQLModel definition (in `src/db/models.py`):**
```python
class SystemMetadata(SQLModel, table=True):
    __tablename__ = "system_metadata"
    key: str = Field(primary_key=True)
    value: str
    updated_at: Optional[datetime] = Field(default_factory=datetime.utcnow)
```

**Keys used by ingestion scripts:**

| Key | Written by | Purpose |
|-----|-----------|---------|
| `aact_last_sync` | `fetch_aact_csvs.py` | ISO timestamp of last successful AACT upsert |
| `aact_studies_prev_count` | `fetch_aact_csvs.py` | Row count from previous run — REQ-079 >10% drop alert |
| `last_news_prune` | `scheduler.py` | ISO timestamp of last `news_articles` 90-day pruning |

---

## 11. Computed / Derived Values (Not Stored)

These are computed at query time or by the Streamlit UI — never persisted:

| Expression | Used By |
|-----------|---------|
| `days_until_event = catalysts.event_date - CURRENT_DATE` | Streamlit catalyst timeline; re-run daily by scheduler |
| `position_value_usd = smart_money_positions.shares × companies.price_current` | Streamlit Smart Money view |
| `negative_ev = (total_cash_usd > market_cap_usd + total_debt_usd)` | Streamlit Deep Value filter |
| `partnership_bonus` | SQL view queried by Strategist agent (see AGENT_010_PARTNERSHIP.md) |
| `cash_per_share = total_cash_usd / shares_outstanding` | Recomputed nightly; also stored in `companies.cash_per_share` |

---

## 12. REQ Cross-Reference

| REQ | Column(s) Affected | Table |
|-----|--------------------|-------|
| REQ-004 | All upsert operations | All tables |
| REQ-015 | `competition_score` | `agent_profiler_findings` |
| REQ-023 | `rnpv_usd` | `agent_profiler_findings` |
| REQ-025 | `quality_score` | `partnerships` |
| REQ-026 | `company_type` | `agent_profiler_findings` |
| REQ-029 | `study_type` | `studies` |
| REQ-034 | `lead_sponsor_class = 'INDUSTRY'` filter applied at ingest from AACT `sponsors.txt`; non-INDUSTRY rows discarded | `studies.lead_sponsor_class` (output) |
| REQ-040 | `entity_aliases` resolution rate | `entity_aliases` |
| REQ-063 | Scheduler SLA | `output/scheduler_alerts.log` |
| REQ-066 | `floor_price` | `companies` |
| REQ-071 | `is_active` | `companies` |
| REQ-072 | `full_json`, `uploaded_to_rag` | All agent output tables |
| REQ-074 | `enrollment_is_actual` | `studies` |
| REQ-075 | `design_outcomes.measure` | `design_outcomes` |
| REQ-079 | `aact_last_sync`, `aact_studies_prev_count` | `system_metadata` |
| REQ-084 | `drugs_extracted`, `extraction_confidence` | `company_onboarding_log` |
| REQ-087 | RSS feed polling, 4h cadence | `news_articles` (ingestion) |
| REQ-088 | Ticker association pass; `ticker` column | `news_articles` |
| REQ-089 | `category` auto-assign via keyword regex | `news_articles` |
| REQ-090 | `url` UNIQUE constraint, `INSERT OR IGNORE` | `news_articles` |
| REQ-091 | 90-day pruning (Sunday 02:00) | `news_articles` |

---

*Path uniformity: all scripts resolve `biotech_tracker.db` relative to the project root via `os.environ.get("DB_PATH", "biotech_tracker.db")`. Python package imports use the `src/` prefix (`from src.db.models import ...`). Single source of truth for all data variables and states.*
