"""
SECEdgarFetcherTool — fetch Form 4 and 13G/13D filings from EDGAR.

Spec: docs/CREWAI_TOOLS.md v2.0, Section 4
Used by: Agent 005 (Insider — Form 4), Agent 011 (Smart Money — 13G/13D)
"""

import json
import os
import time
from datetime import datetime, timedelta

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "BiotechAnalyzer research@example.com")
_EDGAR_RATE_DELAY = 0.15   # 10 req/s max


class SECEdgarFetchInput(BaseModel):
    cik: str = Field(description="Company CIK number (will be zero-padded to 10 digits)")
    form_types: list[str] = Field(
        description=(
            "List of form types to fetch, e.g. ['4'] for Form 4, "
            "['SC 13G', 'SC 13G/A', 'SC 13D', 'SC 13D/A'] for institutional ownership"
        )
    )
    lookback_days: int = Field(
        default=90,
        description="Number of days to look back for filings",
    )


class SECEdgarFetcherTool(BaseTool):
    name: str = "sec_edgar_fetch"
    description: str = (
        "Fetch SEC filings from EDGAR API for a given company CIK. "
        "Use for Form 4 (insider transactions) and SC 13G/13D (institutional ownership). "
        "Returns list of filing metadata with download URLs."
    )
    args_schema: type[BaseModel] = SECEdgarFetchInput
    user_agent: str = SEC_USER_AGENT

    def _run(self, cik: str, form_types: list[str], lookback_days: int = 90) -> str:
        try:
            headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
            padded_cik = str(cik).zfill(10)
            url = f"https://data.sec.gov/submissions/CIK{padded_cik}.json"
            time.sleep(_EDGAR_RATE_DELAY)

            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()

            submissions = resp.json()
            filings = submissions.get("filings", {}).get("recent", {})
            forms = filings.get("form", [])
            dates = filings.get("filingDate", [])
            accessions = filings.get("accessionNumber", [])
            docs = filings.get("primaryDocument", [])

            cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            results = []

            for i, form in enumerate(forms):
                if form not in form_types:
                    continue
                filing_date = dates[i] if i < len(dates) else ""
                if filing_date < cutoff:
                    continue
                accession = accessions[i] if i < len(accessions) else ""
                doc = docs[i] if i < len(docs) else ""
                accession_fmt = accession.replace("-", "")
                raw_cik = str(submissions.get("cik", cik)).lstrip("0")
                filing_url = (
                    f"https://www.sec.gov/Archives/edgar/data/{raw_cik}/{accession_fmt}/{doc}"
                )
                results.append({
                    "form": form,
                    "date": filing_date,
                    "accession": accession,
                    "url": filing_url,
                })

            return json.dumps(results)
        except requests.exceptions.Timeout:
            return "ERROR: Request timed out. Try again or use a cached result."
        except requests.exceptions.HTTPError as e:
            return f"ERROR: HTTP {e.response.status_code} — {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    def fetch_filing_text(self, url: str) -> str:
        """
        Helper: fetch the raw text of a specific EDGAR filing URL.
        Used by Insider and Smart Money agents to read Form 4 XML or 13G full text.
        """
        try:
            headers = {"User-Agent": self.user_agent}
            time.sleep(_EDGAR_RATE_DELAY)
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
