"""
Page 1 — Catalyst Timeline (Macro View)

Components:
  - Portfolio HUD: active count, Negative EV alerts, M&A rumor count
  - Interactive Gantt chart: 12–18 month catalyst calendar (Plotly)
  - Agent Signals Feed: left sidebar, last 48h of agent outputs
  - News Headlines sub-feed: category badges
  - M&A Rumor Alerts: amber banner
  - Filter panel: signal type, event type, company_type
  - CSP Workbench: options expiration overlay (bottom drawer)

UI spec: docs/ui-ux/UI_CONCEPT.md v3.5p Section 1
"""

from __future__ import annotations

import json
from datetime import date

import html
import streamlit as st

from app.queries import (
    get_all_active_tickers,
    get_catalysts,
    get_companies_with_ma_alerts,
    get_news_feed,
    get_portfolio_summary,
)
from app.components.gantt import render_gantt
from app.components.signals_feed import render_signals_feed
from app.components.csp_workbench import render_csp_workbench

# Impact score → Plotly color
_IMPACT_COLOR = {
    "high": "#ef4444",  # ≥ 8 — PDUFA, Phase 3 top-line
    "medium": "#f97316",  # 6–7 — AdComm, interim analysis
    "low": "#3b82f6",  # 4–5 — conference
    "signal": "#22c55e",  # RSS_NEWS sourced
}


def _impact_tier(score: int | None, source_type: str | None) -> str:
    if source_type == "RSS_NEWS":
        return "signal"
    if score is None:
        return "low"
    if score >= 8:
        return "high"
    if score >= 6:
        return "medium"
    return "low"


@st.cache_data(ttl=300)
def _load_catalysts(
    company_type_filter: str, event_type_filter: list[str]
) -> list[dict]:
    """Cache catalyst load for 5 minutes."""
    tickers = get_all_active_tickers(
        company_type=None if company_type_filter == "All" else company_type_filter
    )
    ticker_set = set(tickers)

    catalysts = get_catalysts(days_ahead=540)
    result = []
    for c in catalysts:
        if c.ticker not in ticker_set:
            continue
        if event_type_filter and c.event_type not in event_type_filter:
            continue
        result.append(
            {
                "ticker": c.ticker,
                "event_type": c.event_type,
                "event_date": c.event_date,
                "event_name": c.event_name or c.event_type,
                "drug_name": c.drug_name or "",
                "market_impact_score": c.market_impact_score,
                "source_type": c.source_type,
                "color": _IMPACT_COLOR[
                    _impact_tier(c.market_impact_score, c.source_type)
                ],
            }
        )
    return result


def render_page1():
    """Render the Catalyst Timeline page."""

    # ── Sidebar: Filter Panel ─────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### Filters (F)")

        company_type_filter = st.selectbox(
            "Company Type",
            ["All", "THERAPEUTIC", "PLATFORM"],
            key="filter_company_type",
        )

        event_type_filter = st.multiselect(
            "Event Type",
            [
                "PDUFA_DATE",
                "ADCOMM_DATE",
                "DATA_READOUT",
                "CONFERENCE",
                "INTERIM_ANALYSIS",
            ],
            key="filter_event_type",
            placeholder="All event types",
        )

        st.divider()
        st.markdown("### Jump To")
        if st.button(
            "Gantt Timeline (T)",
            key="_p1_gantt_jump",
            help="Scroll to the Catalyst Timeline view",
        ):
            st.markdown(
                '<script>document.getElementById("gantt_anchor").scrollIntoView({behavior: "smooth"})</script>',
                unsafe_allow_html=True,
            )
        if st.button(
            "News Feed (N)",
            key="_p1_news_jump",
            help="Scroll to the News Headlines view",
        ):
            st.markdown(
                '<script>document.getElementById("news_anchor").scrollIntoView({behavior: "smooth"})</script>',
                unsafe_allow_html=True,
            )

    # ── Portfolio HUD ─────────────────────────────────────────────────────
    summary = get_portfolio_summary()
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Active Tickers", summary["active_count"])
    with c2:
        neg_ev = summary["negative_ev_count"]
        st.metric(
            "Negative EV",
            neg_ev,
            help="Companies where total_cash > market_cap + debt — potential deep value",
            delta=None,
        )
        if neg_ev > 0:
            tickers_str = ", ".join(html.escape(str(t)) for t in summary["negative_ev_tickers"][:5])
            st.markdown(
                f'<span class="neg-ev-badge">NEG EV: {tickers_str}</span>',
                unsafe_allow_html=True,
            )
    with c3:
        ma_companies = get_companies_with_ma_alerts()
        st.metric("M&A Alerts", len(ma_companies))
    with c4:
        catalysts_all = get_catalysts(days_ahead=90)
        high_impact = [c for c in catalysts_all if (c.market_impact_score or 0) >= 8]
        st.metric(
            "Catalysts (90d)",
            len(catalysts_all),
            help=f"{len(high_impact)} high-impact (PDUFA/Phase3)",
        )

    st.divider()

    # ── M&A Rumor Alerts ──────────────────────────────────────────────────
    if ma_companies:
        alerts_html = " | ".join(
            f"<strong>{html.escape(str(co.ticker))}</strong>{_ma_badge_detail(co)}"
            for co in ma_companies[:5]
        )
        st.markdown(
            f'<div class="rumor-alert-banner">⚠️ <b>M&A Rumor Alerts:</b> {alerts_html}</div>',
            unsafe_allow_html=True,
        )

    # ── Main layout: Gantt (center) + Signals Feed (right) ───────────────
    col_main, col_signals = st.columns([3, 1])

    with col_main:
        st.markdown('<div id="gantt_anchor"></div>', unsafe_allow_html=True)
        st.subheader("Catalyst Timeline (T)")

        catalyst_data = _load_catalysts(company_type_filter, event_type_filter)

        if catalyst_data:
            render_gantt(catalyst_data)
        else:
            st.info(
                "No upcoming catalysts found. Run Crew 1 (Data Collection) to populate the calendar."
            )

        # ── CSP Workbench (bottom drawer) ─────────────────────────────
        with st.expander("CSP Workbench — Options Expiration Overlay", expanded=False):
            render_csp_workbench(catalyst_data)

    with col_signals:
        render_signals_feed()

    # ── News feed ─────────────────────────────────────────────────────────
    st.divider()
    st.markdown('<div id="news_anchor"></div>', unsafe_allow_html=True)
    st.subheader("News Headlines (N) — Last 48h")

    news_category = st.selectbox(
        "Category",
        ["All", "m&a", "fda", "conference", "partnership", "analyst"],
        key="_p1_news_cat",
        label_visibility="collapsed",
    )

    news_items = get_news_feed(
        category=None if news_category == "All" else news_category,
        hours=48,
    )

    _BADGE_HTML = {
        "m&a": '<span class="badge-ma">M&A</span>',
        "fda": '<span class="badge-fda">FDA</span>',
        "conference": '<span class="badge-conf">CONF</span>',
        "partnership": '<span class="badge-partner">PARTNER</span>',
        "analyst": '<span class="badge-analyst">ANALYST</span>',
    }

    if news_items:
        for item in news_items[:50]:
            badge = _BADGE_HTML.get(item.category or "", "")
            ts = item.published_at.strftime("%m/%d %H:%M") if item.published_at else ""
            ticker_tag = (
                f"**{html.escape(item.ticker or '')}** — " if item.ticker else ""
            )
            st.markdown(
                f"{badge} {ticker_tag}{html.escape(item.headline or '')} "
                f"<small style='color:#64748b'>{html.escape(item.source or '')} · {ts}</small>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("No recent headlines. News ingestion runs every 4 hours.")


def _ma_badge_detail(co) -> str:
    """Extract the latest MA_RUMOR_FLAG headline for display."""
    try:
        flags = json.loads(co.watchlist_flags or "[]")
        for f in flags:
            if isinstance(f, dict) and f.get("flag") == "MA_RUMOR_FLAG":
                return f": {html.escape(f.get('headline', '')[:60])}"
    except Exception:
        pass
    return ""
