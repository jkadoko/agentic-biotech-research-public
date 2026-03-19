"""
LocalRAGTool — semantic search over ChromaDB local vector store.

Spec: docs/CREWAI_TOOLS.md v2.0, Section 3
Embedding model: mxbai-embed-large:latest via OllamaEmbeddingFunction
Collections: sec_filings, trial_protocols, agent_memos

Collections must be initialized via scripts/init_chromadb.py before first use.
"""

import json
import os

import chromadb
from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

CHROMADB_HOST = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMADB_PORT = int(os.environ.get("CHROMADB_PORT", "8000"))
OLLAMA_HOST = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")
EMBED_MODEL = "mxbai-embed-large:latest"

VALID_COLLECTIONS = {"sec_filings", "agent_memos", "trial_protocols"}


class LocalRAGQueryInput(BaseModel):
    query: str = Field(
        description=(
            "Natural language question to ask the ChromaDB knowledge base. "
            "Contains: 10-K/10-Q SEC filings, 8-K event filings, prior investment memos, "
            "scientific audit reports, and trial protocols. "
            "Example: 'What are the key patent risks in MRNA latest 10-K?'"
        )
    )
    collection: str = Field(
        description=(
            "ChromaDB collection to query. Options: 'sec_filings', 'agent_memos', "
            "'trial_protocols'. "
            "Use 'sec_filings' for 10-K/8-K/Form4. Use 'agent_memos' for historical recommendations."
        )
    )
    ticker: str = Field(
        default=None,
        description="Optional: scope the search to a specific ticker (e.g., 'MRNA')",
    )
    n_results: int = Field(
        default=5,
        description="Number of top results to return (1–10)",
    )


def _get_chroma_client() -> chromadb.HttpClient:
    return chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)


def _get_embed_fn() -> OllamaEmbeddingFunction:
    return OllamaEmbeddingFunction(model_name=EMBED_MODEL, url=OLLAMA_HOST)


class LocalRAGTool(BaseTool):
    name: str = "local_rag_query"
    description: str = (
        "Semantic search over the ChromaDB local knowledge base containing SEC filings, "
        "trial protocols, and prior agent memos. Specify the collection to search. "
        "Use for complex queries that SQL cannot answer: "
        "'Find similar Phase 3 CAR-T failures in DLBCL', "
        "'What were partnership terms in comparable oncology deals?'"
    )
    args_schema: type[BaseModel] = LocalRAGQueryInput

    def _run(self, query: str, collection: str, ticker: str = None, n_results: int = 5) -> str:
        try:
            if collection not in VALID_COLLECTIONS:
                return (
                    f"Error: Unknown collection '{collection}'. "
                    f"Valid options: {sorted(VALID_COLLECTIONS)}"
                )

            client = _get_chroma_client()
            embed_fn = _get_embed_fn()

            coll = client.get_or_create_collection(
                name=collection,
                embedding_function=embed_fn,
            )

            # Scope query by ticker if provided
            scoped_query = f"[{ticker}] {query}" if ticker else query
            where = {"ticker": ticker} if ticker else None

            kwargs: dict = {
                "query_texts": [scoped_query],
                "n_results": max(1, min(n_results, 10)),
            }
            if where:
                kwargs["where"] = where

            results = coll.query(**kwargs)
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            output = [
                {
                    "text": d,
                    "source": m,
                    "relevance_score": round(1 - dist, 4) if dist is not None else None,
                }
                for d, m, dist in zip(docs, metas, distances)
            ]
            return json.dumps(output)
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
