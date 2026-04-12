"""
commit_onboarding.py: Step 2 of the JSON-First Onboarding Architecture.

This script runs on the Server (or locally if syncing DB) and reads all JSON files
in `output/onboarding/` that haven't been committed yet. It safely performs
database upserts (REQ-004 compliant) into all relevant tables including:
- Companies
- Interventions (Drugs)
- Partnerships
- Catalysts

After upserting the parsed JSON data, it runs the standard trial linkage (Step 6)
and FDA orphan lookups (Step 7) to fully integrate the new company data.
Successfully processed JSON files are moved to `output/onboarding/processed/`.

Usage:
  python scripts/commit_onboarding.py
"""

import glob
import json
import logging
import os
import shutil
import sys
import traceback
from datetime import datetime

# Ensure project root on path when run as script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.models import init_db
from src.db.data_manager import (
    ensure_onboarding_study_sentinel,
    get_session,
    mark_company_onboarding_status,
    upsert_catalyst,
    upsert_company,
    upsert_intervention,
    upsert_partnership,
    upsert_sec_filing,
    write_onboarding_log,
)
from scripts.onboard_company import step6_link_trials, step7_fda_lookups

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "onboarding")
PROCESSED_DIR = os.path.join(OUTPUT_DIR, "processed")

def process_single_json(filepath: str) -> None:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    ticker = data.get("ticker")
    if not ticker:
        log.error("JSON missing 'ticker': %s", filepath)
        return

    log.info("=" * 60)
    log.info("Committing Onboarding Data for: %s", ticker)

    ext = data.get("extraction", {})
    comp_meta = ext.get("company_metadata", {})
    drugs = ext.get("drugs", [])
    clinical_trials = ext.get("clinical_trials", [])
    partnerships = ext.get("partnerships", [])
    catalysts = ext.get("future_catalysts", [])

    # Initialize Audit Log
    audit = {
        "ticker": ticker,
        "trigger_source": "JSON_SYNC",
        "drugs_extracted": len(drugs),
        "nct_ids_cited": len([t for t in clinical_trials if t.get("nct_id")]),
        "trials_linked": 0,
        "orphan_lookups": 0,
        "status": "FAILED",
        "error_notes": None,
        "sec_edgar_url": data.get("filing_url"),
        "filing_date": data.get("filing_date"),
    }

    try:
        with get_session() as session:
            # 1. Update Company
            company_update = {
                "ticker": ticker,
                "company_name": comp_meta.get("name"),
                "exchange": comp_meta.get("exchange"),
                "sector": comp_meta.get("sector"),
                "cik": data.get("cik"),
                "last_filing_parsed": datetime.today().date(),
            }
            upsert_company(session, company_update)

            # 2. Update SEC Filing
            filing_date_str = data.get("filing_date")
            filing_date = datetime.strptime(filing_date_str, "%Y-%m-%d").date() if filing_date_str else datetime.today().date()
            
            upsert_sec_filing(session, {
                "ticker": ticker,
                "filing_date": filing_date,
                "filing_type": data.get("filing_type", "10-K"),
                "edgar_url": data.get("filing_url"),
                "local_rag_source_id": None, # Kept local on dev PC
                "uploaded_to_rag": False,
            })
            session.flush()

            # Guarantee ONBOARDING FK for interventions
            ensure_onboarding_study_sentinel(session)

            # 3. Upsert Drugs / Interventions
            drug_names = []
            for d in drugs:
                d_name = d.get("name")
                if not d_name:
                    continue
                drug_names.append(d_name)
                upsert_intervention(session, {
                    "nct_id": "ONBOARDING",
                    "drug_name": d_name,
                    "ticker": ticker,
                    "indication": d.get("indication"),
                })
            session.flush()

            # 4. Upsert Partnerships
            for p in partnerships:
                if not p.get("partner_name"):
                    continue
                upsert_partnership(session, {
                    "ticker": ticker,
                    "partner_name": p.get("partner_name"),
                    "drug_asset": "Multiple/Platform" if not drugs else drugs[0].get("name", "Unknown"), # Default if no asset linked
                    "partnership_type": p.get("type"),
                    "upfront_usd": p.get("upfront_usd"),
                    "milestone_usd": p.get("milestone_usd"),
                    "source_type": "SEC_10K"
                })
            session.flush()

            # 5. Upsert Catalysts
            for c in catalysts:
                if not c.get("event_type") or not c.get("event_date"):
                    continue
                
                raw_date = c.get("event_date")
                parsed_date = datetime.today().date()
                if isinstance(raw_date, str) and "-" in raw_date:
                    try:
                        parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                    except ValueError:
                        pass

                upsert_catalyst(session, {
                    "ticker": ticker,
                    "event_type": str(c["event_type"])[:100],
                    "event_date": parsed_date,
                    "event_name": str(c.get("event_name", ""))[:200],
                    "drug_name": str(c.get("drug_name", ""))[:100],
                    "indication": str(c.get("indication", ""))[:200],
                    "source_type": "COMPANY_GUIDANCE"
                })
            session.flush()
            
            # Commit initial data
            session.commit()

            # 6. Link clinical trials using extracted drug names & company name
            # We reconstruct the expected 'extracted' payload for the legacy step6 function.
            legacy_extracted = {
                "drug_names": drug_names,
                "nct_ids": [t.get("nct_id") for t in clinical_trials if t.get("nct_id")]
            }
            linked = step6_link_trials(session, ticker, legacy_extracted, comp_meta.get("name"))
            audit["trials_linked"] = linked
            session.commit()

            # 7. FDA lookups
            fda_count = step7_fda_lookups(session, ticker, drug_names)
            audit["orphan_lookups"] = fda_count
            session.commit()

            # Determine Success
            audit["extraction_confidence"] = "HIGH" if len(drug_names) > 3 else "MEDIUM" if len(drug_names) > 0 else "LOW"
            audit["status"] = "COMPLETE" if audit["extraction_confidence"] != "LOW" or linked > 0 else "PARTIAL"

            # Finalize Audit
            write_onboarding_log(session, {**audit, "onboarding_date": datetime.utcnow()})
            mark_company_onboarding_status(session, ticker, audit["status"])
            session.commit()
            
            log.info("Onboarding Commit %s: %s", ticker, audit["status"])

            # Move to processed
            os.makedirs(PROCESSED_DIR, exist_ok=True)
            shutil.move(filepath, os.path.join(PROCESSED_DIR, os.path.basename(filepath)))
            log.info("Moved %s to processed/", os.path.basename(filepath))

    except Exception as e:
        log.error("Commit failed for %s: %s", ticker, e)
        traceback.print_exc()
        # Attempt minimal failure log
        try:
            with get_session() as session:
                audit["error_notes"] = f"Commit Error: {str(e)}"
                write_onboarding_log(session, {**audit, "onboarding_date": datetime.utcnow()})
                mark_company_onboarding_status(session, ticker, "FAILED")
                session.commit()
        except:
            pass


def main():
    if not os.path.exists(OUTPUT_DIR):
        log.info("No output directory found at %s. Nothing to commit.", OUTPUT_DIR)
        return

    json_files = glob.glob(os.path.join(OUTPUT_DIR, "*.json"))
    if not json_files:
        log.info("No new JSON files found to commit.")
        return

    init_db()
    log.info("Found %d JSON files to process.", len(json_files))

    for filepath in json_files:
        process_single_json(filepath)


if __name__ == "__main__":
    main()
