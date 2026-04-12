"""
Agent Signals Feed component — left sidebar cards for Page 1.

Shows the 10 most recent agent output rows across all active tickers,
pulled from the last 48h of agent output tables.

UI spec: docs/ui-ux/UI_CONCEPT.md v3.5p Section 1.3
"""

from __future__ import annotations

import html
from datetime import date, timedelta

import streamlit as st


@st.cache_data(ttl=300)
def _load_recent_signals() -> list[dict]:
    """Load agent signals from the last 48h across all agent output tables."""
    from app.queries import get_session, get_all_active_tickers
    from sqlmodel import select, desc
    from src.db.models import (
        AgentInvestmentMemo,
        AgentInsiderFinding,
        AgentSmartMoneyFinding,
        AgentVolatilityFinding,
    )

    cutoff = date.today() - timedelta(days=2)
    signals = []

    # Get active tickers and cap scan to 100 to avoid timeout
    tickers = get_all_active_tickers()[:100]
    if not tickers:
        return []

    with get_session() as session:
        # ⚡ Bolt: Fix N+1 queries. We were running 4 individual queries per ticker in a loop (up to 400 queries).
        # We now query in bulk and deduplicate on the Python side, dropping DB calls to 5.

        # 1. Investment Memos
        memos = session.exec(
            select(AgentInvestmentMemo)
            .where(AgentInvestmentMemo.ticker.in_(tickers))
            .where(AgentInvestmentMemo.memo_date >= cutoff)
            .order_by(desc(AgentInvestmentMemo.memo_date))
        ).all()
        seen = set()
        for m in memos:
            if m.ticker not in seen:
                seen.add(m.ticker)
                signals.append(
                    {
                        "ticker": m.ticker,
                        "agent": "Strategist",
                        "signal": m.primary_recommendation or "—",
                        "score": m.biotech_alpha_score,
                        "date": m.memo_date,
                        "icon": "🎯",
                    }
                )

        # 2. Insider Findings
        insiders = session.exec(
            select(AgentInsiderFinding)
            .where(AgentInsiderFinding.ticker.in_(tickers))
            .where(AgentInsiderFinding.scan_date >= cutoff)
            .order_by(desc(AgentInsiderFinding.scan_date))
        ).all()
        seen = set()
        for i in insiders:
            if i.ticker not in seen:
                seen.add(i.ticker)
                signals.append(
                    {
                        "ticker": i.ticker,
                        "agent": "Insider",
                        "signal": i.signal or "—",
                        "score": (i.conviction_score or 0) * 10,
                        "date": i.scan_date,
                        "icon": "👤",
                    }
                )

        # 3. Smart Money Findings
        sm = session.exec(
            select(AgentSmartMoneyFinding)
            .where(AgentSmartMoneyFinding.ticker.in_(tickers))
            .where(AgentSmartMoneyFinding.scan_date >= cutoff)
            .order_by(desc(AgentSmartMoneyFinding.scan_date))
        ).all()
        seen = set()
        for s in sm:
            if s.ticker not in seen:
                seen.add(s.ticker)
                signals.append(
                    {
                        "ticker": s.ticker,
                        "agent": "Smart Money",
                        "signal": s.signal or "—",
                        "score": (s.conviction_score or 0) * 10,
                        "date": s.scan_date,
                        "icon": "🏦",
                    }
                )

        # 4. Volatility Findings
        vols = session.exec(
            select(AgentVolatilityFinding)
            .where(AgentVolatilityFinding.ticker.in_(tickers))
            .where(AgentVolatilityFinding.scan_date >= cutoff)
            .order_by(desc(AgentVolatilityFinding.scan_date))
        ).all()
        seen = set()
        for v in vols:
            if v.ticker not in seen:
                seen.add(v.ticker)
                signals.append(
                    {
                        "ticker": v.ticker,
                        "agent": "Volatility",
                        "signal": v.status or "—",
                        "score": int((v.annualized_return_pct or 0) * 100),
                        "date": v.scan_date,
                        "icon": "📈",
                    }
                )

    # Sort by date descending, then score
    signals.sort(key=lambda x: (str(x["date"]), x.get("score", 0)), reverse=True)
    return signals[:30]


def render_signals_feed() -> None:
    """Render the Agent Signals Feed in a scrollable container."""
    st.markdown("### Agent Signals (48h)")

    signals = _load_recent_signals()

    if not signals:
        st.caption(
            "No agent outputs in the last 48h. Run the full pipeline to populate."
        )
        return

    _SIGNAL_COLORS = {
        "CLUSTER_BUY": "#22c55e",
        "ISOLATED_BUY": "#86efac",
        "HARD_SELL": "#ef4444",
        "AVOID": "#f87171",
        "APPROVED": "#22c55e",
        "REJECTED": "#ef4444",
        "CAUTION": "#f97316",
        "MOONSHOT": "#a78bfa",
        "DEEP_VALUE": "#6ee7b7",
        "CSP_CANDIDATE": "#93c5fd",
    }

    for s in signals:
        color = _SIGNAL_COLORS.get(s["signal"], "#94a3b8")
        score_str = f" · {s['score']}" if s.get("score") is not None else ""

        # 🔒 SECURITY: Escape database values rendered in raw HTML to prevent XSS
        safe_ticker = html.escape(str(s.get("ticker", "")))
        safe_agent = html.escape(str(s.get("agent", "")))
        safe_signal = html.escape(str(s.get("signal", "")))

        st.markdown(
            f'<div class="glass-card">'
            f'{s["icon"]} <b>{safe_ticker}</b> — {safe_agent}<br>'
            f'<span style="color:{color}; font-weight:600">{safe_signal}</span>'
            f"{score_str}<br>"
            f'<small style="color:#64748b">{s["date"]}</small>'
            f"</div>",
            unsafe_allow_html=True,
        )

        if st.button(
            f"Analyze {safe_ticker} →",
            key=f"_sig_{s['ticker']}_{s['agent']}_{s['date']}",
            use_container_width=True,
            help=f"Open Analyst Workspace for {safe_ticker}"
        ):
            import streamlit as _st

            _st.session_state.selected_ticker = s["ticker"]
            _st.session_state.active_page = 2
            _st.rerun()
