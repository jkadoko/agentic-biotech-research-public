"""
Agent 001 — Detective: Entity Resolution Specialist

Spec: docs/AGENT_001_DETECTIVE.md v2.2
Crew: Data Collection (Crew 1)
Model: llama3.1:8b (Ollama GPU0)

Resolves ClinicalTrials.gov INDUSTRY sponsor names → public ticker symbols.
5-step logic: alias cache → exact match → fuzzy → web search → alias registration.
"""

import os

from crewai import Agent, LLM, Task

from src.tools.clinicaltrials_tool import ClinicalTrialsTool
from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.search_tool import DuckDuckGoSearchTool

_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")


def make_detective_agent() -> Agent:
    llm = LLM(model="ollama/llama3.1:8b", base_url=_GPU0)
    return Agent(
        role="Entity Resolution Specialist",
        goal=(
            "Map every ClinicalTrials.gov INDUSTRY sponsor name to its public ticker "
            "symbol with ≥90% resolution rate and ≤1% false positives. "
            "Maintain the entity_aliases cache so future lookups are instant."
        ),
        backstory=(
            "You are a biotech M&A genealogist with encyclopedic knowledge of "
            "pharmaceutical acquisitions. You know that Kite Pharma → GILD, "
            "Immunomedics → GILD, Celgene → BMY, Spark → RHHBY, and hundreds more. "
            "You always check the alias cache first and only do expensive web searches "
            "when the cache and fuzzy matching both fail. You never map NIH, NCI, "
            "or academic sponsors to tickers — they are not investable entities."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            DuckDuckGoSearchTool(),
            ClinicalTrialsTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=15,
    )


def make_detective_task(agent: Agent, sponsor_names: list[str]) -> Task:
    sponsors_str = "\n".join(f"- {s}" for s in sponsor_names)
    return Task(
        description=(
            f"Resolve the following ClinicalTrials.gov INDUSTRY sponsor names to public "
            f"ticker symbols:\n{sponsors_str}\n\n"
            "STEP 1 — Pre-Triage (Rule-Based):\n"
            "  - Skip any sponsor where sponsor_class is NIH, FED, NETWORK, or OTHER.\n"
            "  - Query: SELECT ticker FROM entity_aliases WHERE LOWER(alias_name) = LOWER(sponsor).\n"
            "  - If found with confidence >= 0.85: use it (source=CACHE_HIT), skip Steps 2–3.\n"
            "  - Query: SELECT ticker FROM companies WHERE LOWER(company_name) = LOWER(sponsor).\n"
            "  - If exact match: use it (confidence=0.99, source=EXACT_MATCH).\n\n"
            "STEP 2 — Fuzzy Pre-Screen (only if Step 1 missed):\n"
            "  - Compute token-sort ratio against companies.company_name.\n"
            "  - Ratio >= 0.95: HIGH_CONFIDENCE (source=FUZZY_MATCH, confidence=0.92), done.\n"
            "  - Ratio 0.85–0.94: LOW_CONFIDENCE, proceed to Step 3.\n"
            "  - Ratio < 0.85: UNRESOLVED, proceed to Step 3.\n\n"
            "STEP 3 — Web Investigation (only if Steps 1–2 failed):\n"
            "  - DuckDuckGoSearchTool: '{sponsor} acquired by pharmaceutical company'\n"
            "  - DuckDuckGoSearchTool: '{sponsor} parent company public stock ticker'\n"
            "  - Check acquisition date vs trial start_date in studies table.\n"
            "    If trial started BEFORE acquisition: map to PARENT company ticker.\n"
            "    If trial started AFTER acquisition: standard mapping.\n\n"
            "STEP 4 — Alias Registration (DatabaseWriteTool on entity_aliases):\n"
            "  - Write each resolved mapping with: alias_name, ticker, confidence, source.\n"
            "  - confidence: EXACT_MATCH=0.99, FUZZY_MATCH=0.92, WEB_SEARCH=0.85, UNRESOLVED=0.0.\n\n"
            "CRITICAL SCOPE GUARD: Do NOT process sponsors whose company has "
            "onboarding_status = 'COMPLETE' — trial linkage for those is owned by "
            "onboard_company.py.\n\n"
            "Return JSON: [{\"sponsor\": str, \"ticker\": str|null, "
            "\"confidence\": float, \"source\": str}]"
        ),
        expected_output=(
            "JSON array: each element has sponsor (str), ticker (str or null), "
            "confidence (float 0.0–1.0), source "
            "(CACHE_HIT | EXACT_MATCH | FUZZY_MATCH | WEB_SEARCH | UNRESOLVED)."
        ),
        agent=agent,
    )
