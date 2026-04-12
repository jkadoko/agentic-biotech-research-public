"""
Nightly SEC EDGAR filings ingestion — 10-K, 10-Q, 8-K, Form 4, SC 13G, SC 13D.

Schedule: nightly 03:00 via APScheduler
REQ-071: only processes is_active = 1 tickers

For each active ticker:
  1. Resolve CIK via EDGAR company search (or use stored companies.cik)
  2. Fetch recent filings list from EDGAR submissions API
  3. For 10-K filings: set onboarding_status = STALE so onboard_company.py re-runs
  4. Upsert new filing rows into sec_filings table

EDGAR API:
  https://data.sec.gov/submissions/CIK{cik_padded}.json
  User-Agent header: SEC_USER_AGENT env var (e.g., "MyApp your@email.com")
"""

import logging
import os
import time
from datetime import date, datetime

import requests

from src.db.data_manager import (
    get_active_tickers,
    get_session,
    mark_company_onboarding_status,
    upsert_company,
    upsert_sec_filing,
)
from src.db.models import Company, init_db

log = logging.getLogger(__name__)

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "BiotechAnalyzer research@example.com")
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_COMPANY_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2000-01-01&forms=10-K"
FILING_TYPES_OF_INTEREST = {"10-K", "10-Q", "8-K", "4", "SC 13G", "SC 13G/A", "SC 13D", "SC 13D/A"}

# EDGAR rate limit: 10 req/s; be conservative
_REQUEST_DELAY = 0.15


def _headers() -> dict:
    return {"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}


def _pad_cik(cik: str) -> str:
    return str(cik).zfill(10)


def resolve_cik(ticker: str) -> str | None:
    """Lookup CIK from EDGAR company search. Returns CIK string or None."""
    try:
        url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&forms=10-K"
        resp = requests.get(url, headers=_headers(), timeout=15)
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
        if hits:
            entity = hits[0].get("_source", {})
            return str(entity.get("entity_id", "")).lstrip("0") or None
    except Exception as exc:
        log.debug("CIK lookup failed for %s: %s", ticker, exc)

    # Fallback: EDGAR company tickers JSON
    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_headers(), timeout=15,
        )
        resp.raise_for_status()
        for entry in resp.json().values():
            if entry.get("ticker", "").upper() == ticker.upper():
                return str(entry["cik_str"])
    except Exception as exc:
        log.debug("company_tickers fallback failed for %s: %s", ticker, exc)
    return None


def fetch_submissions(cik: str) -> dict:
    """Fetch EDGAR submissions JSON for a padded CIK."""
    url = EDGAR_SUBMISSIONS_URL.format(cik=_pad_cik(cik))
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_filings(submissions: dict, ticker: str) -> list[dict]:
    """Extract filing rows we care about from the submissions JSON."""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    rows = []
    for form, filing_date_str, accession, doc in zip(forms, dates, accessions, primary_docs):
        # Normalize form type
        form_clean = form.strip()
        if form_clean == "4":
            form_clean = "Form 4"
        if form_clean not in FILING_TYPES_OF_INTEREST and form_clean.replace("/A", "") not in FILING_TYPES_OF_INTEREST:
            continue

        # Build EDGAR URL
        accession_fmt = accession.replace("-", "")
        edgar_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{submissions.get('cik', '')}/{accession_fmt}/{doc}"
        )

        try:
            filing_date = date.fromisoformat(filing_date_str)
        except (ValueError, TypeError):
            continue

        rows.append({
            "ticker": ticker,
            "filing_type": form_clean,
            "filing_date": filing_date,
            "edgar_url": edgar_url,
        })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(tickers: list[str] | None = None) -> None:
    init_db()
    with get_session() as session:
        active = tickers or get_active_tickers(session)
        log.info("fetch_sec_filings: processing %d tickers", len(active))

        for ticker in active:
            co = session.get(Company, ticker)
            cik = co.cik if co else None

            # Resolve CIK if not cached
            if not cik:
                cik = resolve_cik(ticker)
                if cik:
                    upsert_company(session, {"ticker": ticker, "cik": cik})
                    session.flush()
                else:
                    log.warning("Could not resolve CIK for %s — skipping", ticker)
                    continue

            time.sleep(_REQUEST_DELAY)
            try:
                submissions = fetch_submissions(cik)
            except Exception as exc:
                log.warning("EDGAR submissions fetch failed for %s (CIK %s): %s", ticker, cik, exc)
                continue

            filings = parse_filings(submissions, ticker)
            new_10k_found = False
            for filing in filings:
                upsert_sec_filing(session, filing)
                if filing["filing_type"] == "10-K":
                    new_10k_found = True

            # REQ-171: new 10-K → mark STALE to trigger re-onboarding
            if new_10k_found and co and co.last_filing_parsed:
                # Only mark stale if this 10-K is newer than last parse
                most_recent_10k = max(
                    (f["filing_date"] for f in filings if f["filing_type"] == "10-K"),
                    default=None,
                )
                if most_recent_10k and most_recent_10k > co.last_filing_parsed:
                    mark_company_onboarding_status(session, ticker, "STALE")
                    log.info("New 10-K detected for %s — onboarding_status = STALE", ticker)

        session.commit()
        log.info("fetch_sec_filings: complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()
