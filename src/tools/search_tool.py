"""
DuckDuckGoSearchTool — web search via DuckDuckGo (no API key required).

Spec: docs/CREWAI_TOOLS.md v2.1, Section 6
Used by: Agents 001, 002, 003, 004, 008

Replaces GoogleSearchTool (removed — required paid API key).
DuckDuckGo search is free, requires no credentials, and returns titles, URLs, and snippets.

Requires: duckduckgo-search (pip install duckduckgo-search)
"""

import json

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class DuckDuckGoSearchInput(BaseModel):
    query: str = Field(
        description=(
            "Search query string. Be specific. "
            "Include company names, drug names, and dates for best results. "
            "Example: 'Moderna mRNA-1345 RSV vaccine FDA approval PDUFA 2025'"
        )
    )
    num_results: int = Field(
        default=5,
        description="Number of search results to return (1–10)",
    )


class DuckDuckGoSearchTool(BaseTool):
    name: str = "web_search"
    description: str = (
        "Search the web for current information about biotech companies, FDA decisions, "
        "clinical trial results, acquisitions, and PDUFA dates. "
        "Returns titles, URLs, and snippets from top results. "
        "Use when the database or ChromaDB does not have current information. "
        "No API key required."
    )
    args_schema: type[BaseModel] = DuckDuckGoSearchInput

    def _run(self, query: str, num_results: int = 5) -> str:
        try:
            from duckduckgo_search import DDGS

            n = max(1, min(num_results, 10))
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=n):
                    results.append({
                        "title": r.get("title"),
                        "url": r.get("href"),
                        "snippet": r.get("body"),
                    })
            return json.dumps(results)
        except ImportError:
            return (
                "ERROR: duckduckgo-search not installed. "
                "Run: pip install duckduckgo-search"
            )
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
