"""
OllamaLLMTool — run local LLM inference via Ollama.

Spec: docs/CREWAI_TOOLS.md v2.0, Section 8
Used by: Agent 006 (Strategist — deepseek-r1 reasoning), Agent 010 (Partnership — fast extraction)

VRAM budget (Tesla P4, 8GB each):
  GPU0: llama3.1:8b (~5.5GB) — primary model for most agents
  GPU1: deepseek-r1:7b-q4_K_M (~4.5GB) + llama3.2:3b (~2.0GB) = 6.5GB — fits on single P4

All data processed stays on-premises (no cloud calls).
"""

import json
import os

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

OLLAMA_HOST_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")
OLLAMA_HOST_GPU1 = os.environ.get("OLLAMA_HOST_GPU1", "http://ollama-gpu1:11435")

# Route reasoning models to GPU1, primary models to GPU0
_MODEL_HOST_MAP = {
    "deepseek-r1:7b-q4_K_M": OLLAMA_HOST_GPU1,
    "llama3.2:3b": OLLAMA_HOST_GPU1,
    "llama3.1:8b": OLLAMA_HOST_GPU0,
    "mxbai-embed-large:latest": OLLAMA_HOST_GPU0,
}


class OllamaInferenceInput(BaseModel):
    prompt: str = Field(description="Full prompt to send to the local LLM")
    model: str = Field(
        default="deepseek-r1:7b-q4_K_M",
        description=(
            "Ollama model name. Options: "
            "deepseek-r1:7b-q4_K_M (chain-of-thought reasoning — Strategist), "
            "llama3.2:3b (fast structured extraction — Partnership/Volatility), "
            "llama3.1:8b (primary general model)"
        ),
    )
    temperature: float = Field(
        default=0.1,
        description="Temperature (0.0–1.0). Use 0.0–0.1 for structured extraction, 0.3–0.5 for reasoning.",
    )


class OllamaLLMTool(BaseTool):
    name: str = "ollama_inference"
    description: str = (
        "Run inference on a local Ollama LLM. "
        "Use deepseek-r1:7b-q4_K_M for chain-of-thought reasoning "
        "(investment synthesis, BAS scoring, strategy evaluation). "
        "Use llama3.2:3b for fast structured extraction "
        "(partnership entity extraction from SEC text, JSON parsing). "
        "All data processed here stays on-premises — never use for personally identifiable data."
    )
    args_schema: type[BaseModel] = OllamaInferenceInput
    ollama_host: str = OLLAMA_HOST_GPU1   # Default to GPU1 (deepseek-r1 home)

    def _run(
        self,
        prompt: str,
        model: str = "deepseek-r1:7b-q4_K_M",
        temperature: float = 0.1,
    ) -> str:
        try:
            # Route to correct GPU based on model.
            # If the user has changed the host from the module-level default, use that.
            # Otherwise, use the model host map.
            host = self.ollama_host
            if host == OLLAMA_HOST_GPU1 and model in _MODEL_HOST_MAP:
                host = _MODEL_HOST_MAP[model]
            payload = {
                "model": model,
                "prompt": prompt,
                "temperature": temperature,
                "stream": False,
            }
            resp = requests.post(
                f"{host}/api/generate",
                json=payload,
                timeout=180,   # deepseek-r1 reasoning can take up to 3 minutes
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except requests.exceptions.Timeout:
            return "ERROR: Ollama inference timed out. Model may be loading or under load."
        except requests.exceptions.ConnectionError:
            return (
                f"ERROR: Cannot connect to Ollama at {host}. "
                "Ollama service must be running (docker-compose --profile gpu up ollama-gpu0 ollama-gpu1)."
            )
        except requests.exceptions.HTTPError as e:
            return f"ERROR: HTTP {e.response.status_code} — {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
