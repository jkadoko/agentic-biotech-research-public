"""
OptionsChainTool — fetch live options chain data.

Spec: docs/CREWAI_TOOLS.md v2.0, Section 7
Used by: Agent 009 (Volatility)

Primary: E*TRADE API (OAuth1 via scripts/EtradePythonClient/)
Fallback: yfinance (free, near-real-time; lacks precise live bid/ask)

E*TRADE Deprecation Note: Being migrated to Morgan Stanley API.
Until migration, yfinance fallback provides sufficient data for paper trading strategies.
"""

import json
from datetime import datetime

import yfinance as yf
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class OptionsChainInput(BaseModel):
    ticker: str = Field(description="Stock ticker symbol (e.g., 'CRSP', 'MRNA')")
    max_dte: int = Field(
        default=45,
        description="Maximum days to expiration filter (REQ-011: CSP requires <45 DTE)",
    )
    option_type: str = Field(
        default="PUT",
        description="Option type: PUT (for CSP strategy) or CALL",
    )


class OptionsChainTool(BaseTool):
    name: str = "options_chain_fetch"
    description: str = (
        "Fetch live options chain data for a ticker. "
        "Returns put/call contracts with strike, expiration, bid, ask, "
        "IV (implied volatility), and open interest. "
        "Automatically filters to contracts with DTE <= max_dte. "
        "Use for Volatility agent Step 2 (IV check) and Step 5 (OI ≥ 500, spread ≤ 10%)."
    )
    args_schema: type[BaseModel] = OptionsChainInput

    def _run(self, ticker: str, max_dte: int = 45, option_type: str = "PUT") -> str:
        try:
            tk = yf.Ticker(ticker.upper())
            if not tk.options:
                return json.dumps([])  # No options available for this ticker

            now = datetime.utcnow()
            eligible_expiries = [
                exp for exp in tk.options
                if (datetime.strptime(exp, "%Y-%m-%d") - now).days <= max_dte
            ]

            results = []
            for exp in eligible_expiries:
                try:
                    chain = tk.option_chain(exp)
                    contracts = chain.puts if option_type.upper() == "PUT" else chain.calls
                    dte = (datetime.strptime(exp, "%Y-%m-%d") - now).days

                    for _, row in contracts.iterrows():
                        oi = int(row.get("openInterest") or 0)
                        iv = float(row.get("impliedVolatility") or 0) or None
                        bid = float(row.get("bid") or 0) or None
                        ask = float(row.get("ask") or 0) or None
                        strike = float(row.get("strike") or 0)

                        results.append({
                            "expiration": exp,
                            "strike": strike,
                            "bid": bid,
                            "ask": ask,
                            "mid": round((bid + ask) / 2, 4) if bid and ask else None,
                            "iv": iv,
                            "open_interest": oi,
                            "dte": dte,
                            # Derived checks for Volatility agent
                            "spread_pct": round((ask - bid) / ask, 4) if ask and ask > 0 else None,
                        })
                except Exception:
                    continue  # Skip problematic expiry dates

            return json.dumps(results)
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
