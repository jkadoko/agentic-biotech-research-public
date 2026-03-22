import asyncio
import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# We check for the library import here instead of top-level to avoid
# requirement issues if only basic tools are used.
try:
    from notebooklm.client import NotebookLMClient
except ImportError:
    NotebookLMClient = None

log = logging.getLogger(__name__)

# Prompt used for extraction
_EXTRACTION_PROMPT = """Find the latest 10-K or 20-F for {ticker} in this notebook.
List ALL drugs, therapeutics, and pipeline assets.

FORMAT as clean JSON:
{{
  "drug_names": ["NAME1", "NAME2"],
  "nct_ids": ["NCT12345678"],
  "pipeline_phases": {{"NAME1": "Phase 3"}}
}}
Return ONLY the JSON. 
"""

class NotebookLMTool:
    """
    Tool to query NotebookLM as a fallback for high-accuracy 10-K extraction.
    """

    def __init__(self):
        self.notebook_id = os.environ.get("NOTEBOOKLM_BIOTECH_ID")
        if not self.notebook_id:
            log.warning("NOTEBOOKLM_BIOTECH_ID not found in environment")

    async def _query_async(self, ticker: str) -> dict | None:
        """Internal async implementation of the query."""
        if not NotebookLMClient:
            log.error("notebooklm-py library not installed")
            return None

        if not self.notebook_id:
            log.error("No notebook ID configured for NotebookLMTool")
            return None

        try:
            # Prioritize storage_state.json in project root (for Docker visibility)
            auth_path = "/app/storage_state.json" if os.path.exists("/app/storage_state.json") else None
            
            # Increase timeout to 90s for complex notebook queries
            async with await NotebookLMClient.from_storage(path=auth_path, timeout=90) as client:
                prompt = _EXTRACTION_PROMPT.format(ticker=ticker)
                
                log.info("Querying NotebookLM for ticker: %s", ticker)
                result = await client.chat.ask(self.notebook_id, prompt)
                
                if not result or not result.answer:
                    log.warning("NotebookLM returned an empty response for %s", ticker)
                    return None

                # Clean up response to get only JSON
                text = result.answer.strip()
                # Remove markdown code blocks if present
                text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`")
                
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    log.error("Failed to parse JSON from NotebookLM: %s", text[:200])
                    return None

        except Exception as e:
            log.error("NotebookLM API error for %s: %s", ticker, e)
            return None

    def query_ticker(self, ticker: str) -> dict | None:
        """
        Synchronous wrapper to query the notebook for a specific ticker.
        Returns a dict with extracted info or None on failure.
        """
        try:
            return asyncio.run(self._query_async(ticker))
        except Exception as e:
            log.error("Async execution failed for NotebookLMTool: %s", e)
            return None

if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)
    tool = NotebookLMTool()
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MRNA"
    res = tool.query_ticker(ticker)
    print(json.dumps(res, indent=2))
