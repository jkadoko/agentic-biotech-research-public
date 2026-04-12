"""
Nightly market data ingestion (yfinance + E*TRADE fallback).

Schedule: nightly 06:30 via APScheduler (scripts/scheduler.py)
REQ-071: only processes tickers WHERE is_active = 1

Fetches per active ticker:
  - price_current, market_cap_usd, shares_outstanding
  - total_cash_usd, total_debt_usd, annual_revenue_usd
  - book_value_per_share, week52_high, week52_low
  - 90 days of historical OHLCV

Derives and stores:
  - cash_per_share  = total_cash_usd / shares_outstanding
  - floor_price     = MAX(cash_per_share, book_value_per_share, week52_low × 0.90) [REQ-066]
  - runway_months   = total_cash_usd / burn_rate_monthly_usd (if burn available)
"""

import logging
import os
from datetime import date, timedelta

import yfinance as yf

from src.db.data_manager import (
    bulk_upsert_prices,
    get_active_tickers,
    get_session,
    upsert_company,
)
from src.db.models import init_db

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIELDS = {
    "currentPrice": "price_current",
    "marketCap": "market_cap_usd",
    "sharesOutstanding": "shares_outstanding",
    "totalCash": "total_cash_usd",
    "totalDebt": "total_debt_usd",
    "totalRevenue": "annual_revenue_usd",
    "bookValue": "book_value_per_share",
    "fiftyTwoWeekHigh": "week52_high",
    "fiftyTwoWeekLow": "week52_low",
}


def _compute_derived(row: dict) -> dict:
    """Derive floor_price, cash_per_share, runway_months."""
    cash = row.get("total_cash_usd")
    shares = row.get("shares_outstanding")
    if cash and shares and shares > 0:
        row["cash_per_share"] = cash / shares

    bv = row.get("book_value_per_share") or 0.0
    cps = row.get("cash_per_share") or 0.0
    low = row.get("week52_low") or 0.0
    candidates = [v for v in [cps, bv, low * 0.90] if v and v > 0]
    if candidates:
        row["floor_price"] = max(candidates)

    burn = row.get("burn_rate_monthly_usd")
    if cash and burn and burn > 0:
        row["runway_months"] = int(cash / burn)

    return row


def fetch_ticker_fundamentals(ticker: str) -> dict:
    """Pull fundamentals from yfinance for one ticker."""
    try:
        info = yf.Ticker(ticker).info
        row: dict = {"ticker": ticker}
        for yf_key, db_col in _FIELDS.items():
            val = info.get(yf_key)
            if val is not None:
                row[db_col] = val
        return _compute_derived(row)
    except Exception as exc:
        log.warning("yfinance fundamentals failed for %s: %s", ticker, exc)
        return {"ticker": ticker}


def fetch_historical_prices(ticker: str, days: int = 90) -> list[dict]:
    """Fetch OHLCV history for `days` days."""
    try:
        start = (date.today() - timedelta(days=days)).isoformat()
        df = yf.download(ticker, start=start, auto_adjust=True, progress=False)
        if df.empty:
            return []
        rows = []
        # ⚡ Bolt: Fast iteration using to_dict('index') instead of slow df.iterrows()
        for ts, row in df.to_dict("index").items():
            rows.append(
                {
                    "ticker": ticker,
                    "date": ts.date(),
                    "open": float(row["Open"]) if "Open" in row else None,
                    "high": float(row["High"]) if "High" in row else None,
                    "low": float(row["Low"]) if "Low" in row else None,
                    "close": float(row["Close"]) if "Close" in row else None,
                    "volume": int(row["Volume"]) if "Volume" in row else None,
                }
            )
        return rows
    except Exception as exc:
        log.warning("yfinance history failed for %s: %s", ticker, exc)
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(tickers: list[str] | None = None) -> None:
    init_db()
    with get_session() as session:
        active = tickers or get_active_tickers(session)
        log.info("fetch_market_data: processing %d tickers", len(active))

        for ticker in active:
            # Fundamentals
            fundamentals = fetch_ticker_fundamentals(ticker)
            upsert_company(session, fundamentals)

            # Historical prices
            price_rows = fetch_historical_prices(ticker)
            if price_rows:
                bulk_upsert_prices(session, price_rows)

        session.commit()
        log.info("fetch_market_data: complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()
