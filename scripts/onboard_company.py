"""
Ticker-first onboarding pipeline (7 steps).

Runs:
  - On-demand when user adds a ticker via Streamlit UI
  - Automatically for any ticker with onboarding_status = PENDING or STALE (daily 07:00)
  - After Scout Task A detects a new IPO

Steps:
  1. Validate ticker (yfinance)
  2. Fetch latest 10-K from SEC EDGAR
  3. Embed 10-K into ChromaDB (LocalRAGTool write mode — 512-token chunks)
  4. LLM structured extraction (llama3.1:8b via Ollama)
     Extracts: drug names, NCT IDs, pipeline phases, officers, revenue stage,
     patent cliff dates, top risks, cash/burn/runway
  5. Upsert into SQLite (companies + interventions + company_onboarding_log)
  6. Link clinical trials — 4-pass approach:
     Pass 1: NCT IDs cited in 10-K → direct CT.gov lookup (10K_CITED)
     Pass 2: Drug name search → CT.gov (DRUG_NAME_MATCH)
     Pass 3: Company name → CT.gov sponsor search (COMPANY_NAME_MATCH)
     Pass 4: Detective entity_aliases → resolved ticker (ENTITY_RESOLVED)
  7. FDA lookups (Orange/Purple Book + orphan DB) — NOT_FOUND logged, not blocking

Usage:
  python scripts/onboard_company.py MRNA
  python scripts/onboard_company.py  # processes all PENDING/STALE tickers
"""

import argparse
import json
import logging
import multiprocessing
import os
import re
import sys
import time
from datetime import date, datetime
from typing import Any

import warnings

import requests
import yfinance as yf
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dotenv import load_dotenv

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

load_dotenv()

# Ensure project root on path when run as script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.data_manager import (
    ensure_onboarding_study_sentinel,
    get_active_tickers,
    get_session,
    mark_company_onboarding_status,
    upsert_company,
    upsert_entity_alias,
    upsert_intervention,
    upsert_sec_filing,
    upsert_study,
    upsert_trial_pipeline,
    write_agent_json_output,
    write_onboarding_log,
)
from src.db.models import Company, EntityAlias, init_db
from ingestion.fetch_clinical_trials import (
    search_by_drug_name,
    search_by_nct_ids_batch,
    search_by_sponsor,
)
from ingestion.fetch_fda_data import (
    _download_orange_book,
    lookup_orange_book,
    lookup_purple_book,
    run_orphan_lookup,
    upsert_orphan,
)

log = logging.getLogger(__name__)

# GPU host mapping: worker_id → Ollama endpoint
_OLLAMA_HOSTS = {
    0: os.environ.get("OLLAMA_HOST_GPU0", "http://localhost:11434"),
    1: os.environ.get("OLLAMA_HOST_GPU1", "http://localhost:11434"),
}
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "BiotechAnalyzer research@example.com")
CHROMADB_HOST = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMADB_PORT = int(os.environ.get("CHROMADB_PORT", "8000"))

_HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}

# 10-K section boundaries for regex-first extraction
_SECTION_BOUNDARIES_10K = {
    "business": (r"Item\s+1\.?\s+Business", r"Item\s+1A\."),
    "risk_factors": (r"Item\s+1A\.?\s+Risk\s+Factors", r"Item\s+1B\."),
    "mda": (r"Item\s+7\.?\s+Management", r"Item\s+7A\."),
}

# 20-F section boundaries
_SECTION_BOUNDARIES_20F = {
    "business": (r"Item\s+4\.?\s+Information", r"Item\s+4A\."),
    "risk_factors": (r"Item\s+3\.?\s*D\.?\s+Risk\s+Factors", r"Item\s+4\."),
    "mda": (r"Item\s+5\.?\s+Operating\s+and\s+Financial", r"Item\s+6\."),
}

_NCT_PATTERN = re.compile(r"NCT\d{8}", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Step 1: Validate ticker
# ---------------------------------------------------------------------------


def step1_validate_ticker(ticker: str) -> dict | None:
    """
    Validate ticker via yfinance. Returns basic company info dict or None.
    Updates companies row with exchange, sector, company_name if found.
    """
    try:
        info = yf.Ticker(ticker).info
        if not info or info.get("quoteType") == "NONE":
            log.error("Step 1 FAIL: %s not found on yfinance", ticker)
            return None
        return {
            "ticker": ticker,
            "company_name": info.get("longName") or info.get("shortName"),
            "exchange": info.get("exchange"),
            "sector": info.get("sector"),
            "market_cap_usd": info.get("marketCap"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "total_cash_usd": info.get("totalCash"),
        }
    except Exception as exc:
        log.error("Step 1 FAIL: yfinance error for %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Step 2: Fetch latest 10-K from SEC EDGAR
# ---------------------------------------------------------------------------


def _resolve_cik(ticker: str) -> str | None:
    """Resolve ticker → SEC CIK number.

    Strategy:
      1. Lookup in the static company_tickers.json (fast, covers ~13k tickers).
      2. Fallback: query the SEC browse-edgar HTML page and extract the CIK
         from the response (handles recently-listed or renamed companies).
      3. If the ticker contains a suffix (e.g. AIM^F13), retry with the base
         symbol stripped of everything after ^ or =.
    """
    # Normalise: strip whitespace, uppercase
    clean = ticker.strip().upper()

    # Try the original ticker first, then the base symbol if it has a suffix
    candidates = [clean]
    base = re.split(r'[\^=]', clean)[0]
    if base != clean:
        candidates.append(base)

    for candidate in candidates:
        cik = _resolve_cik_single(candidate)
        if cik:
            return cik

    return None


def _resolve_cik_single(ticker: str) -> str | None:
    """Try static JSON first, then browse-edgar fallback."""
    # --- Pass 1: static company_tickers.json ---
    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        for entry in resp.json().values():
            if entry.get("ticker", "").upper() == ticker.upper():
                return str(entry["cik_str"])
    except Exception as exc:
        log.warning("CIK resolution (JSON) failed for %s: %s", ticker, exc)

    # --- Pass 2: browse-edgar HTML fallback ---
    try:
        browse_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?CIK={ticker}&action=getcompany"
        )
        resp = requests.get(browse_url, headers=_HEADERS, timeout=20, allow_redirects=True)
        resp.raise_for_status()
        # The page includes CIK in several places; grab it from the first
        # occurrence of cik=NNNNNN in an href.
        cik_match = re.search(r'CIK=(\d{5,10})', resp.text)
        if cik_match:
            cik = cik_match.group(1)
            log.info("CIK resolved via browse-edgar fallback: %s → %s", ticker, cik)
            return cik
    except Exception as exc:
        log.warning("CIK resolution (browse-edgar) failed for %s: %s", ticker, exc)

    return None


def step2_fetch_10k(ticker: str, cik: str | None = None) -> dict | None:
    """
    Fetch the most recent 10-K text from EDGAR.
    Returns dict with: edgar_url, filing_date, text (raw 10-K text).
    """
    if not cik:
        cik = _resolve_cik(ticker)
    if not cik:
        log.error("Step 2 FAIL: could not resolve CIK for %s", ticker)
        return None

    try:
        submissions_url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"
        resp = requests.get(submissions_url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        submissions = resp.json()
    except Exception as exc:
        log.error("Step 2 FAIL: EDGAR submissions fetch for %s: %s", ticker, exc)
        return None

    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    target_forms = ["10-K", "20-F"]
    fallback_forms = ["S-1", "S-1/A", "F-1", "F-1/A"]

    for accepted_forms in (target_forms, fallback_forms):
        for form, filing_date_str, accession, doc in zip(forms, dates, accessions, docs):
            form_type = form.strip()
            if form_type not in accepted_forms:
                continue
            accession_fmt = accession.replace("-", "")
            edgar_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_fmt}/{doc}"
            )
            try:
                filing_date = date.fromisoformat(filing_date_str)
            except (ValueError, TypeError):
                continue

            # Fetch actual text and strip HTML to plain text.
            # 10-Ks are delivered as iXBRL (inline XBRL) HTML — passing raw HTML to
            # the LLM gives it only XML namespace boilerplate, not drug names.
            time.sleep(0.2)
            try:
                text_resp = requests.get(edgar_url, headers=_HEADERS, timeout=30)
                text_resp.raise_for_status()
                soup = BeautifulSoup(text_resp.text, "lxml")
                text = soup.get_text(separator=" ", strip=True)
            except Exception as exc:
                log.warning("Could not fetch filing text from %s: %s", edgar_url, exc)
                continue  # Try next one if this failed

            log.info("Step 2 OK: fetched %s for %s (filed %s, %d plain-text chars)",
                     form_type, ticker, filing_date_str, len(text))
            return {"edgar_url": edgar_url, "filing_date": filing_date, "text": text, "cik": cik, "filing_type": form_type}

    log.error("Step 2 FAIL: no 10-K, 20-F, or S-1/F-1 found for %s", ticker)
    return None


# ---------------------------------------------------------------------------
# Step 3: Embed 10-K into ChromaDB
# ---------------------------------------------------------------------------


def _ollama_embed_batch(texts: list[str], ollama_host: str,
                        model: str = "mxbai-embed-large:latest",
                        timeout: int = 120) -> list[list[float]] | None:
    """Call Ollama /api/embed directly with a generous timeout.

    The ChromaDB OllamaEmbeddingFunction uses httpx with a very short default
    timeout (~5s), which is insufficient when multiple workers share a GPU.
    By calling the API ourselves we control the timeout (default 120s).
    """
    try:
        resp = requests.post(
            f"{ollama_host}/api/embed",
            json={"model": model, "input": texts},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("embeddings")
    except requests.exceptions.Timeout:
        log.warning("Ollama embed timeout (%ds) for %d texts on %s",
                    timeout, len(texts), ollama_host)
        return None
    except Exception as exc:
        log.warning("Ollama embed error on %s: %s", ollama_host, exc)
        return None


def step3_embed_10k(ticker: str, text: str, edgar_url: str, ollama_host: str | None = None) -> str | None:
    """
    Embed 10-K text into ChromaDB sec_filings collection.

    Pre-computes embeddings via Ollama /api/embed (120s timeout) then upserts
    the vectors directly into ChromaDB.  This bypasses the ChromaDB
    OllamaEmbeddingFunction whose short httpx timeout causes failures when
    multiple workers share a GPU.

    Uses 75-word overlapping chunks with mxbai-embed-large:latest via Ollama.
    Returns ChromaDB source_id or None on failure.
    """
    if ollama_host is None:
        ollama_host = _OLLAMA_HOSTS[0]
    try:
        import chromadb

        client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
        # No embedding_function — we pre-compute embeddings ourselves.
        collection = client.get_or_create_collection(name="sec_filings")

        # 75-word chunks with 15-word overlap, strictly truncated to < 1600 chars (ensures < 512 tokens)
        words = text.split()
        chunk_size = 75
        overlap = 15
        chunks = []
        i = 0
        while i < len(words):
            chunk = " ".join(words[i:i + chunk_size])
            if len(chunk) > 1600:
                chunk = chunk[:1600]
            chunks.append(chunk)
            i += chunk_size - overlap

        if not chunks:
            log.warning("Step 3: no chunks to embed for %s", ticker)
            return None

        source_id = f"{ticker}_{date.today().isoformat()}_10K"
        log.info("Step 3: embedding %d chunks (max chunk length: %d chars)",
                 len(chunks), max(len(c) for c in chunks))
        ids = [f"{source_id}_chunk_{j}" for j in range(len(chunks))]
        metadatas = [
            {"ticker": ticker, "source": edgar_url, "chunk_index": j, "doc_type": "10-K"}
            for j in range(len(chunks))
        ]

        # Pre-compute embeddings in batches of 10, then upsert to ChromaDB.
        # Embedding timeout is 120s per batch (vs OllamaEmbeddingFunction's ~5s).
        embed_batch_size = 10
        max_retries = 4
        all_embeddings: list[list[float]] = []

        for start in range(0, len(chunks), embed_batch_size):
            end = start + embed_batch_size
            batch_texts = chunks[start:end]
            batch_num = start // embed_batch_size + 1
            total_batches = (len(chunks) + embed_batch_size - 1) // embed_batch_size

            embeddings = None
            for attempt in range(max_retries):
                embeddings = _ollama_embed_batch(batch_texts, ollama_host)
                if embeddings and len(embeddings) == len(batch_texts):
                    break
                wait = (attempt + 1) * 5  # 5s, 10s, 15s, 20s
                log.warning(
                    "Step 3: Embed failed for %s batch %d/%d, retrying in %ds...",
                    ticker, batch_num, total_batches, wait,
                )
                time.sleep(wait)

            if not embeddings or len(embeddings) != len(batch_texts):
                log.warning("Step 3: giving up on embedding batch %d/%d for %s",
                            batch_num, total_batches, ticker)
                return None

            all_embeddings.extend(embeddings)

            if batch_num % 20 == 0 or batch_num == total_batches:
                log.info("Step 3: embedded %d/%d batches for %s",
                         batch_num, total_batches, ticker)

        # Upsert pre-computed embeddings into ChromaDB (no Ollama call needed).
        upsert_batch_size = 50
        for start in range(0, len(chunks), upsert_batch_size):
            end = start + upsert_batch_size
            collection.upsert(
                ids=ids[start:end],
                documents=chunks[start:end],
                embeddings=all_embeddings[start:end],
                metadatas=metadatas[start:end],
            )

        log.info("Step 3 OK: embedded %d chunks for %s into ChromaDB", len(chunks), ticker)
        return source_id
    except Exception as exc:
        log.warning("Step 3 WARN: ChromaDB embedding failed for %s (Ollama available?): %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Step 4: LLM structured extraction
# ---------------------------------------------------------------------------

# Focused drug-extraction prompt — smaller schema = reliable JSON compliance for 8B models
_DRUG_CHUNK_PROMPT = """You are reading a section of a 10-K SEC filing for {ticker}.

List EVERY drug, medicine, vaccine, or therapeutic product name mentioned in the text below.
Return ONLY a JSON object with a single key:
{{"drug_names": ["DrugName1", "DrugName2"]}}

Include brand names (e.g. Keytruda), generic names (e.g. pembrolizumab), and pipeline codes (e.g. mRNA-4157).
If no drugs are mentioned, return {{"drug_names": []}}.
Do not include company names, diseases, or non-drug terms.

TEXT:
{text}
"""

# Metadata prompt — run only on the first chunk to get company-level fields
_METADATA_PROMPT = """You are reading a section of a 10-K SEC filing for {ticker}.

Extract the following as JSON:
{{
  "nct_ids": ["list of NCT IDs (format NCT followed by 8 digits, e.g. NCT01234567)"],
  "pipeline_phases": {{"drug_name": "Phase 1/2/3/NDA"}},
  "ceo_name": "current CEO name or null",
  "revenue_stage": "PRE_REVENUE or COMMERCIAL or null",
  "annual_revenue_usd": null,
  "total_cash_usd": null,
  "burn_rate_monthly_usd": null,
  "runway_months": null,
  "patent_cliff_dates": {{}},
  "top_risks": ["up to 3 key risk factors as short phrases"]
}}

Return ONLY valid JSON. No explanation. Use null for unknown values.

TEXT:
{text}
"""


def _call_ollama(prompt: str, model: str = "llama3.2:3b", ollama_host: str | None = None) -> str | None:
    """Call Ollama local LLM. Returns response text or None."""
    if ollama_host is None:
        ollama_host = _OLLAMA_HOSTS[0]
    try:
        response = requests.post(
            f"{ollama_host}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_ctx": 24576,
                    "temperature": 0.0
                }
            },
            timeout=180
        )
        response.raise_for_status()
        return response.json().get("response", "")
    except requests.exceptions.Timeout:
        log.error("Ollama timeout for %s on %s", model, ollama_host)
        raise TimeoutError(f"Ollama extraction timed out for {model} on {ollama_host}")
    except Exception as e:
        log.error("Ollama error on %s: %s", ollama_host, e)
        raise


def step4_llm_extract(ticker: str, text: str, ollama_host: str | None = None) -> dict:
    """
    LLM structured extraction from 10-K text using local Ollama inference only.

    Strategy:
      1. Extract the Item 1 Business section (skipping TOC stubs).
      2. Split into 6,000-char chunks with 500-char overlap.
      3. Run a focused "drug names only" prompt on each chunk — small schema
         = reliable JSON compliance from 8B models.
      4. Run a single metadata prompt on the first chunk for CEO/revenue/cash.
      5. Merge and deduplicate drug_names; merge regex NCT IDs.
      6. Falls back to regex-only if Ollama is unavailable.

    Local inference only (llama3.1:8b via Ollama). No cloud dependencies.
    """
    if ollama_host is None:
        ollama_host = _OLLAMA_HOSTS[0]

    # Regex fallback: always extract NCT IDs directly from text (high recall)
    nct_regex = list(set(_NCT_PATTERN.findall(text)))

    business_section = text
    if not business_section:
        log.info("Step 4: using regex-only fallback for %s (no sections found)", ticker)
        return _empty_extraction(nct_regex)

    # --- Pass 1: Chunked drug-name extraction (Ollama local inference — primary) ---
    all_drug_names: list[str] = []
    chunk_size = 6_000
    overlap = 500
    chunks: list[str] = []
    i = 0
    while i < len(business_section):
        chunks.append(business_section[i:i + chunk_size])
        i += chunk_size - overlap

    for idx, chunk in enumerate(chunks):
        prompt = _DRUG_CHUNK_PROMPT.format(ticker=ticker, text=chunk)
        response = _call_ollama(prompt, ollama_host=ollama_host)
        if response:
            try:
                cleaned = re.sub(r"```(?:json)?", "", response).strip().rstrip("`")
                parsed = json.loads(cleaned)
                names = parsed.get("drug_names", [])
                if isinstance(names, list):
                    all_drug_names.extend(n for n in names if isinstance(n, str) and n)
            except (json.JSONDecodeError, KeyError):
                pass  # skip bad chunk silently

    # Deduplicate drug names, preserve order
    drug_names: list[str] = []
    seen: set[str] = set()
    for name in all_drug_names:
        key = name.lower().strip()
        if key and key not in seen:
            seen.add(key)
            drug_names.append(name)

    # --- Pass 2: Metadata extraction (Ollama) ---
    metadata: dict = {}
    meta_prompt = _METADATA_PROMPT.format(ticker=ticker, text=business_section[:6_000])
    meta_response = _call_ollama(meta_prompt, ollama_host=ollama_host)
    if meta_response:
        try:
            cleaned = re.sub(r"```(?:json)?", "", meta_response).strip().rstrip("`")
            metadata = json.loads(cleaned)
        except (json.JSONDecodeError, KeyError):
            pass

    # Merge everything — guard against None values from LLM JSON (key present but null)
    merged_nct = list(set((metadata.get("nct_ids") or []) + nct_regex))
    extracted = {
        "drug_names": drug_names,
        "nct_ids": merged_nct,
        "pipeline_phases": metadata.get("pipeline_phases") or {},
        "ceo_name": metadata.get("ceo_name"),
        "revenue_stage": metadata.get("revenue_stage"),
        "annual_revenue_usd": metadata.get("annual_revenue_usd"),
        "total_cash_usd": metadata.get("total_cash_usd"),
        "burn_rate_monthly_usd": metadata.get("burn_rate_monthly_usd"),
        "runway_months": metadata.get("runway_months"),
        "patent_cliff_dates": metadata.get("patent_cliff_dates") or {},
        "top_risks": metadata.get("top_risks") or [],
    }

    log.info("Step 4 OK: LLM extracted %d drugs, %d NCT IDs for %s (%d chunks processed)",
             len(drug_names), len(merged_nct), ticker, len(chunks))
    return extracted


def _empty_extraction(nct_regex: list[str]) -> dict:
    return {
        "drug_names": [],
        "nct_ids": nct_regex,
        "pipeline_phases": {},
        "ceo_name": None,
        "revenue_stage": None,
        "annual_revenue_usd": None,
        "total_cash_usd": None,
        "burn_rate_monthly_usd": None,
        "runway_months": None,
        "patent_cliff_dates": {},
        "top_risks": [],
    }


def _extract_and_clean_relevant_sections(text: str, form_type: str) -> str:
    """
    Regex-first section extraction using boundary strings.
    Extracts Items 1, 1A, 7 for 10-K, and equivalents for 20-F.
    Drops financials/exhibits entirely. Normalizes whitespace and removes junk.
    """
    if form_type in ("S-1", "S-1/A", "F-1", "F-1/A"):
        # S-1/F-1 Prospectus filings don't use standardized Item 1/1A headers.
        # But pipeline details are immediately loaded in the Prospectus Summary.
        extracted = text[:150_000]
    else:
        boundaries = _SECTION_BOUNDARIES_20F if form_type == "20-F" else _SECTION_BOUNDARIES_10K
        
        combined_text = []
        for section_name, (start_pattern, end_pattern) in boundaries.items():
            try:
                matches = list(re.finditer(start_pattern, text, re.IGNORECASE))
                if not matches:
                    continue

                for match in matches:
                    start = match.start()
                    end_match = re.search(end_pattern, text[start:], re.IGNORECASE)
                    end = start + end_match.start() if end_match else start + 80_000
                    section_text = text[start:end]
                    # Skip TOC stubs — real sections are always longer than 500 chars
                    if len(section_text) > 500:
                        combined_text.append(section_text)
                        break
                else:
                    # Fallback: return last match regardless of length
                    start = matches[-1].start()
                    end_match = re.search(end_pattern, text[start:], re.IGNORECASE)
                    end = start + end_match.start() if end_match else start + 80_000
                    combined_text.append(text[start:end])
            except Exception:
                continue
                
        extracted = "\n\n".join(combined_text)
        if not extracted.strip():
            extracted = text[:150_000]
        
    # Content Cleanup
    extracted = re.sub(r'[\-\.=]{5,}', ' ', extracted)
    extracted = re.sub(r'\s{3,}', ' ', extracted)
    
    return extracted.strip()


# ---------------------------------------------------------------------------
# Step 5: Upsert into SQLite
# ---------------------------------------------------------------------------


def step5_upsert_sqlite(session, ticker: str, company_data: dict,
                         extracted: dict, filing: dict, source_id: str | None) -> None:
    """
    Upsert companies, interventions, sec_filings rows.
    REQ-004 enforced via data_manager upsert helpers.
    """
    # Update companies with extracted financial data
    company_update = {
        "ticker": ticker,
        "onboarding_status": "COMPLETE",
        "last_filing_parsed": date.today(),
    }
    for field in ("annual_revenue_usd", "total_cash_usd", "burn_rate_monthly_usd", "runway_months"):
        val = extracted.get(field)
        if val is not None:
            company_update[field] = val

    # Classify revenue stage (REQ-026)
    revenue = extracted.get("annual_revenue_usd") or company_data.get("annual_revenue_usd", 0)
    if revenue and revenue > 0:
        company_update["sector"] = company_data.get("sector")   # preserve existing

    upsert_company(session, {**company_data, **company_update})

    # Upsert interventions for each drug
    for drug_name in extracted.get("drug_names", []):
        if not drug_name:
            continue
        phase = extracted.get("pipeline_phases", {}).get(drug_name)
        upsert_intervention(session, {
            "nct_id": "ONBOARDING",  # placeholder until trial linked
            "drug_name": drug_name,
            "ticker": ticker,
            "indication": None,
            "mechanism_of_action": None,
        })

    # Upsert sec_filings row
    upsert_sec_filing(session, {
        "ticker": ticker,
        "filing_date": filing["filing_date"],
        "filing_type": filing.get("filing_type", "10-K"),
        "edgar_url": filing["edgar_url"],
        "local_rag_source_id": source_id,
        "uploaded_to_rag": source_id is not None,
    })


# ---------------------------------------------------------------------------
# Step 6: 4-pass clinical trial linkage
# ---------------------------------------------------------------------------


def step6_link_trials(session, ticker: str, extracted: dict,
                       company_name: str | None) -> int:
    """
    4-pass trial linkage. Returns total trial_pipeline rows created/updated.
    Priority: 10K_CITED > DRUG_NAME_MATCH > COMPANY_NAME_MATCH > ENTITY_RESOLVED
    """
    linked = 0

    # Pass 1: NCT IDs cited verbatim in 10-K (highest confidence)
    nct_ids = extracted.get("nct_ids", [])
    if nct_ids:
        log.info("Pass 1: looking up %d NCT IDs directly from 10-K", len(nct_ids))
        trials = search_by_nct_ids_batch(nct_ids)
        for trial in trials:
            nct_id = trial.get("nct_id")
            if not nct_id:
                continue
            upsert_study(session, trial)
            upsert_trial_pipeline(session, ticker, nct_id, "10K_CITED")
            linked += 1

    # Pass 2: Drug name search
    drug_names = extracted.get("drug_names", [])
    for drug_name in drug_names:
        if not drug_name:
            continue
        log.debug("Pass 2: drug_name search for %s (%s)", drug_name, ticker)
        trials = search_by_drug_name(drug_name)
        for trial in trials:
            nct_id = trial.get("nct_id")
            if not nct_id:
                continue
            upsert_study(session, trial)
            upsert_trial_pipeline(session, ticker, nct_id, "DRUG_NAME_MATCH")
            linked += 1

    # Pass 3: Company name → sponsor search
    if company_name:
        log.debug("Pass 3: sponsor search for company '%s' (%s)", company_name, ticker)
        trials = search_by_sponsor(company_name)
        for trial in trials:
            nct_id = trial.get("nct_id")
            if not nct_id:
                continue
            upsert_study(session, trial)
            upsert_trial_pipeline(session, ticker, nct_id, "COMPANY_NAME_MATCH")
            linked += 1

    # Pass 4: Detective entity_aliases → resolved sponsor names
    aliases = session.exec(
        __import__("sqlmodel").select(EntityAlias).where(EntityAlias.ticker == ticker)
    ).all()
    for alias_row in aliases:
        alias_name = alias_row.canonical_name or alias_row.alias
        log.debug("Pass 4: entity alias search for '%s' (%s)", alias_name, ticker)
        trials = search_by_sponsor(alias_name)
        for trial in trials:
            nct_id = trial.get("nct_id")
            if not nct_id:
                continue
            upsert_study(session, trial)
            upsert_trial_pipeline(session, ticker, nct_id, "ENTITY_RESOLVED")
            linked += 1

    log.info("Step 6 OK: linked %d trial rows for %s", linked, ticker)
    return linked


# ---------------------------------------------------------------------------
# Step 7: FDA lookups
# ---------------------------------------------------------------------------


def step7_fda_lookups(session, ticker: str, drug_names: list[str]) -> int:
    """
    Orphan DB + Orange/Purple Book lookups.
    REQ-086: NOT_FOUND logged and skipped — does not block COMPLETE status.
    Returns count of successful lookups.
    """
    # Ensure FK parent row exists before any ONBOARDING-keyed intervention insert.
    ensure_onboarding_study_sentinel(session)

    ob_rows = _download_orange_book()
    count = 0

    for drug_name in drug_names:
        if not drug_name:
            continue

        # Orange Book (small molecules)
        ob = lookup_orange_book(drug_name, ob_rows)
        if ob:
            upsert_intervention(session, {
                "nct_id": "ONBOARDING",
                "drug_name": drug_name,
                **ob,
            })
            count += 1
            continue

        # Purple Book (biologics)
        pb = lookup_purple_book(drug_name)
        if pb:
            upsert_intervention(session, {
                "nct_id": "ONBOARDING",
                "drug_name": drug_name,
                **pb,
            })
            count += 1
            continue

        # Pipeline asset fallback: ensure we save the drug even if not in FDA books
        upsert_intervention(session, {
            "nct_id": "ONBOARDING",
            "drug_name": drug_name,
            "ticker": ticker,
        })
        count += 1

        log.debug("REQ-086: %s/%s NOT_FOUND in Orange/Purple Book (investigational)", ticker, drug_name)

    # Orphan lookups
    orphan_rows = run_orphan_lookup(ticker, drug_names)
    for row in orphan_rows:
        upsert_orphan(session, row)
        count += 1

    log.info("Step 7 OK: %d FDA lookups completed for %s", count, ticker)
    return count


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def onboard(ticker: str, worker_id: int = 0) -> str:
    """
    Run the full 7-step onboarding pipeline for a single ticker.
    Returns final onboarding_status: COMPLETE | PARTIAL | FAILED

    Args:
        ticker: Stock ticker symbol
        worker_id: Worker index for GPU routing (0=GPU0, 1=GPU1, 2+=shares GPU0)
    """
    ticker = ticker.upper().strip()

    # Stagger worker startup to avoid thundering herd on GPUs
    if worker_id > 0:
        delay = worker_id * 5.0
        log.info("Staggering worker %d startup: sleeping %.1fs...", worker_id, delay)
        time.sleep(delay)

    # Resolve Ollama host for this worker's GPU
    gpu_idx = worker_id if worker_id < len(_OLLAMA_HOSTS) else 0
    ollama_host = _OLLAMA_HOSTS[gpu_idx]
    log.info("=" * 60)
    log.info("Onboarding: %s (worker=%d, gpu=%s)", ticker, worker_id, ollama_host)

    init_db()
    audit: dict[str, Any] = {
        "ticker": ticker,
        "trigger_source": "MANUAL_TICKER",
        "drugs_extracted": 0,
        "nct_ids_cited": 0,
        "trials_linked": 0,
        "orphan_lookups": 0,
        "status": "FAILED",
        "error_notes": None,
    }

    # ── Step 1: Validate ticker ──────────────────────────────────────
    company_data = step1_validate_ticker(ticker)
    if not company_data:
        audit["error_notes"] = "Step 1: ticker not found on yfinance"
        _write_audit(audit)
        return "FAILED"

    with get_session() as session:
        upsert_company(session, {**company_data, "onboarding_status": "PENDING"})
        session.commit()

    # ── Step 2: Fetch 10-K ───────────────────────────────────────────
    filing = step2_fetch_10k(ticker, cik=None)
    if not filing:
        audit["error_notes"] = "Step 2: no 10-K found on EDGAR"
        _write_audit(audit)
        return "FAILED"
    audit["sec_edgar_url"] = filing["edgar_url"]
    audit["filing_date"] = filing["filing_date"]

    # ── Clean text BEFORE embed/extract ──────────────────────────────
    cleaned_text = _extract_and_clean_relevant_sections(filing["text"], filing.get("filing_type", "10-K"))

    # ── Step 3: Embed into ChromaDB ──────────────────────────────────
    source_id = step3_embed_10k(ticker, cleaned_text, filing["edgar_url"], ollama_host=ollama_host)

    # ── Step 4: LLM extraction ───────────────────────────────────────
    try:
        extracted = step4_llm_extract(ticker, cleaned_text, ollama_host=ollama_host)
        drug_names = extracted.get("drug_names", [])
        nct_ids = extracted.get("nct_ids", [])

        audit["drugs_extracted"] = len(drug_names)
        audit["nct_ids_cited"] = len(nct_ids)
        audit["extraction_confidence"] = (
            "HIGH" if len(drug_names) > 3
            else "MEDIUM" if 1 <= len(drug_names) <= 3
            else "LOW"
        )
    except Exception as e:
        log.error(f"Step 4 failure for {ticker}: {e}")
        audit["status"] = "FAILED"
        audit["error_notes"] = f"Step 4: {str(e)}"
        _write_audit(audit)
        return "FAILED"


    # ── Step 5: Upsert SQLite ────────────────────────────────────────
    with get_session() as session:
        # Guarantee the ONBOARDING FK parent row exists in `studies` before
        # step5 inserts any interventions using that placeholder nct_id.
        ensure_onboarding_study_sentinel(session)
        step5_upsert_sqlite(session, ticker, company_data, extracted, filing, source_id)
        session.commit()

    # ── Step 6: Link clinical trials ─────────────────────────────────
    company_name = company_data.get("company_name")
    with get_session() as session:
        linked = step6_link_trials(session, ticker, extracted, company_name)
        session.commit()
    audit["trials_linked"] = linked

    # ── Step 7: FDA lookups ──────────────────────────────────────────
    with get_session() as session:
        fda_count = step7_fda_lookups(session, ticker, drug_names)
        audit["orphan_lookups"] = fda_count
        session.commit()

    # ── Finalize ─────────────────────────────────────────────────────
    final_status = "COMPLETE" if audit["extraction_confidence"] != "LOW" or linked > 0 else "PARTIAL"
    audit["status"] = final_status

    _write_audit(audit)
    log.info("Onboarding %s: %s", ticker, final_status)
    return final_status


def _write_audit(audit: dict) -> None:
    """Write company_onboarding_log row and output JSON.

    DB retry logic is handled by the @retry_on_db_lock decorator on
    write_onboarding_log and mark_company_onboarding_status — no manual
    retry loop needed here (would cause double-retry on lock contention).
    """
    with get_session() as session:
        write_onboarding_log(session, {**audit, "onboarding_date": datetime.utcnow()})
        mark_company_onboarding_status(session, audit["ticker"], audit["status"])
        session.commit()
    write_agent_json_output(audit["ticker"], "onboarding", audit)


def _worker_onboard(args: tuple) -> tuple[str, str]:
    """
    Picklable worker function for multiprocessing.Pool.
    Args: (ticker, worker_id)
    Returns: (ticker, final_status)
    """
    ticker, worker_id = args
    # Each worker process must reconfigure logging
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [W{worker_id}] %(levelname)s %(message)s",
        force=True,
    )
    try:
        status = onboard(ticker, worker_id=worker_id)
        return (ticker, status)
    except Exception as exc:
        log.error("Onboarding failed for %s: %s", ticker, exc)
        return (ticker, "FAILED")


def run_pending(workers: int = 3, retry_failed: bool = False) -> None:
    """
    Process all tickers with onboarding_status = PENDING or STALE.
    With workers > 1, uses multiprocessing.Pool for parallel execution.

    Args:
        workers: Number of parallel worker processes (default 3).
                 Workers are assigned GPUs: 0→GPU0, 1→GPU1, 2+→GPU0.
        retry_failed: If True, also re-process tickers with status FAILED.
    """
    init_db()

    statuses_to_process = ["PENDING", "STALE"]
    if retry_failed:
        statuses_to_process.append("FAILED")

    with get_session() as session:
        from sqlmodel import select
        pending = session.exec(
            select(Company).where(
                Company.onboarding_status.in_(statuses_to_process),
                Company.is_active == True,
            )
        ).all()
        tickers = [co.ticker for co in pending]

    total = len(tickers)
    log.info("run_pending: %d tickers to onboard (workers=%d, retry_failed=%s)",
             total, workers, retry_failed)

    if total == 0:
        log.info("No tickers to process.")
        return

    start_time = time.time()

    if workers <= 1:
        # Sequential mode (original behavior)
        for i, ticker in enumerate(tickers):
            log.info("Progress: %d/%d (%.0f%%)", i + 1, total, (i + 1) / total * 100)
            try:
                onboard(ticker, worker_id=0)
            except Exception as exc:
                log.error("Onboarding failed for %s: %s", ticker, exc)
    else:
        # Parallel mode: assign each ticker a worker_id round-robin
        work_items = [(ticker, i % workers) for i, ticker in enumerate(tickers)]

        completed = 0
        with multiprocessing.Pool(processes=workers) as pool:
            for ticker, status in pool.imap_unordered(_worker_onboard, work_items):
                completed += 1
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                remaining = (total - completed) / rate if rate > 0 else 0
                log.info(
                    "Progress: %d/%d (%.0f%%) | %s → %s | %.1f tickers/hr | ETA %.0f min",
                    completed, total, completed / total * 100,
                    ticker, status,
                    rate * 3600,
                    remaining / 60,
                )

    elapsed_total = time.time() - start_time
    log.info("run_pending complete: %d tickers processed in %.1f min (%.1f tickers/hr)",
             total, elapsed_total / 60, total / elapsed_total * 3600 if elapsed_total > 0 else 0)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Biotech ticker onboarding pipeline (7 steps)."
    )
    parser.add_argument(
        "tickers", nargs="*",
        help="Specific tickers to onboard. If omitted, processes all PENDING/STALE."
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=3,
        help="Number of parallel worker processes (default: 3). "
             "Workers 0/1 get dedicated GPUs, worker 2+ shares GPU 0."
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Also re-process tickers with status FAILED."
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = _parse_args()
    if args.tickers:
        for t in args.tickers:
            onboard(t)
    else:
        run_pending(workers=args.workers, retry_failed=args.retry_failed)
