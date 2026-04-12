"""
Page 2 — Analyst Workspace (Micro View / Ticker Deep Dive)

Components:
  - Context header: price, cash, runway, floor, company_type, Negative EV badge
  - Top action bar: Force Refresh (trigger onboarding), PDF export placeholder
  - Left sidebar: ticker navigation, Document Vault
  - Strategist's Investment Memo (center): full markdown synthesis
  - Active Trials Module (inline): Phase 2/3 trials, Peer Reviewer verdict
  - Partnerships Module (inline): BD&L intelligence
  - News Feed (inline, collapsible): 90-day ticker-specific headlines
  - Options Chain Snapshot (bottom): live strikes, IV curve
  - Ask the RAG (right float): ChromaDB + Ollama chat panel

UI spec: docs/ui-ux/UI_CONCEPT.md v3.5p Section 2
"""

from __future__ import annotations

import html
import json

import streamlit as st

from app.components.rag_panel import render_rag_panel
from app.queries import (
    get_all_active_tickers,
    get_company,
    get_endpoints_for_trials,
    get_latest_insider,
    get_latest_investment_memo,
    get_latest_profiler,
    get_latest_scientific_audit,
    get_latest_smart_money,
    get_latest_volatility,
    get_news_for_ticker,
    get_options_for_ticker,
    get_partnerships_for_ticker,
    get_trials_for_ticker,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_usd(val: float | None, divisor: float = 1e9, suffix: str = "B") -> str:
    if val is None:
        return "—"
    return f"${val / divisor:.2f}{suffix}"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "—"
    return f"{val * 100:.1f}%"


def _score_color(score: int | None, good_threshold: int = 70) -> str:
    if score is None:
        return "#94a3b8"
    if score >= good_threshold:
        return "#22c55e"
    if score >= 40:
        return "#f97316"
    return "#ef4444"


def _partner_tier_label(tier: int | None) -> str:
    return {1: "Tier 1 — Major Pharma", 2: "Tier 2 — Mid-Tier", 3: "Tier 3"}.get(
        tier or 0, "Unknown"
    )


# ---------------------------------------------------------------------------
# Context Header
# ---------------------------------------------------------------------------


def _render_context_header(ticker: str):
    co = get_company(ticker)
    if co is None:
        st.toast(
            f"Ticker {ticker} not found in database. Use Quick Onboard (O) to add it.",
            icon="❌",
        )
        return

    neg_ev = (
        co.total_cash_usd
        and co.market_cap_usd
        and co.total_cash_usd > (co.market_cap_usd + (co.total_debt_usd or 0))
    )

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    with c1:
        badge = '<span class="neg-ev-badge">NEG EV</span>' if neg_ev else ""
        st.markdown(f"**{html.escape(str(ticker))}** {badge}", unsafe_allow_html=True)
        st.caption(co.company_name or "")
    with c2:
        st.metric("Price", f"${co.price_current:.2f}" if co.price_current else "—")
    with c3:
        st.metric("Market Cap", _fmt_usd(co.market_cap_usd))
    with c4:
        st.metric("Cash", _fmt_usd(co.total_cash_usd))
    with c5:
        st.metric("Runway", f"{co.runway_months}m" if co.runway_months else "—")
    with c6:
        st.metric(
            "Floor Price",
            f"${co.floor_price:.2f}" if co.floor_price else "—",
            help="MAX(cash/share, book_value/share, week52_low × 0.90)",
        )
    with c7:
        profiler = get_latest_profiler(ticker)
        ctype = profiler.company_type if profiler and profiler.company_type else "—"
        st.metric("Type", ctype)

    # Action bar
    a1, a2, a3 = st.columns([1, 1, 6])
    with a1:
        if st.button("Force Refresh", key="_p2_refresh", help="Re-run the onboarding pipeline and refresh data"):
            with st.spinner(f"Re-onboarding {ticker}..."):
                try:
                    from scripts.onboard_company import onboard

                    status = onboard(ticker)
                    st.toast(f"Onboarding: {status}", icon="✅")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    import logging
                    logging.exception("Force refresh failed")
                    st.toast("Refresh failed. Check connection and try again.", icon="❌")
    with a2:
        st.button(
            "Export PDF", key="_p2_pdf", disabled=True, help="PDF export — coming soon"
        )


# ---------------------------------------------------------------------------
# Investment Memo
# ---------------------------------------------------------------------------


def _render_memo(ticker: str):
    st.subheader("Investment Memo (M)")
    memo = get_latest_investment_memo(ticker)

    if memo is None:
        st.info(
            "No investment memo yet. Run the full analysis pipeline for this ticker."
        )
        return

    bas = memo.biotech_alpha_score
    color = _score_color(bas, good_threshold=65)

    # Primary recommendation badge
    rec = memo.primary_recommendation or "—"
    rec_colors = {
        "CSP_CANDIDATE": "#1e3a5f",
        "MOONSHOT": "#2d1e4a",
        "DEEP_VALUE": "#064e3b",
        "HOLD": "#1c1c1c",
        "AVOID": "#450a0a",
        "HARD_SELL": "#7f1d1d",
    }
    bg = rec_colors.get(rec, "#1c1c1c")

    # Premium combined score & recommendation card
    st.markdown(
        f'<div class="glass-card" style="display: flex; justify-content: space-between; align-items: center; border-left: 4px solid {color};">'
        f'<div><span style="color:#94a3b8; font-size:12px; text-transform:uppercase; font-weight:700; letter-spacing:0.05em;">Biotech Alpha Score</span><br>'
        f'<span style="color:{color}; font-size:2.2rem; font-weight:800; line-height:1;">{bas}</span><span style="color:#64748b; font-size:1.2rem; font-weight:600;">/100</span></div>'
        f'<div><span style="background:{bg}; border: 1px solid {bg}80; color:#f8fafc; border-radius:8px; padding:8px 16px; font-weight:700; font-size:15px; letter-spacing:0.05em; box-shadow: 0 4px 15px {bg}60;">{html.escape(str(rec))}</span></div>'
        f"</div>",
        unsafe_allow_html=True,
    )

    if memo.kill_switch:
        st.markdown(
            f'<div class="glass-card" style="border-left: 4px solid #ef4444; background: rgba(239, 68, 68, 0.05);">'
            f'<h4 style="color:#ef4444; margin-top:0; font-weight:700; letter-spacing:0.05em;">⚠️ KILL SWITCH ENGAGED</h4>'
            f'<p style="margin-bottom:0; color:#f8fafc; font-size:15px;">{html.escape(str(memo.kill_switch_reason or "See memo details"))} — no further analysis should be relied upon.</p>'
            f"</div>",
            unsafe_allow_html=True,
        )

    # Render full memo JSON if available
    if memo.full_json:
        try:
            data = json.loads(memo.full_json)
            for section, content in data.items():
                if section in (
                    "ticker",
                    "memo_date",
                    "biotech_alpha_score",
                    "primary_recommendation",
                ):
                    continue
                with st.expander(
                    section.replace("_", " ").title(), expanded=(section == "verdict")
                ):
                    if isinstance(content, dict):
                        for k, v in content.items():
                            st.markdown(f"**{k}:** {v}")
                    elif isinstance(content, list):
                        for item in content:
                            st.markdown(f"- {item}")
                    else:
                        st.markdown(str(content))
        except (json.JSONDecodeError, TypeError):
            st.markdown(memo.full_json)

    # Strategy fit
    if memo.strategy_fit:
        st.markdown("**Strategy Fit:**")
        try:
            fit = json.loads(memo.strategy_fit)
            cols = st.columns(len(fit))
            for i, (strat, status) in enumerate(fit.items()):
                with cols[i]:
                    color = "#22c55e" if status == "ELIGIBLE" else "#64748b"
                    st.markdown(
                        f'<div style="text-align:center; color:{color}; font-size:11px;">'
                        f"<b>{html.escape(str(strat))}</b><br>{html.escape(str(status))}</div>",
                        unsafe_allow_html=True,
                    )
        except Exception:
            st.caption(memo.strategy_fit)

    # Tags
    if memo.tags:
        try:
            tags = json.loads(memo.tags)
            st.markdown(" ".join(f"`{t}`" for t in tags))
        except Exception:
            st.caption(memo.tags)

    st.caption(f"Generated: {memo.memo_date}")


# ---------------------------------------------------------------------------
# Active Trials Module
# ---------------------------------------------------------------------------


def _render_trials(ticker: str):
    st.subheader("Active Trials")
    trials = get_trials_for_ticker(ticker)

    if not trials:
        st.caption("No trials linked. Run onboarding or Crew 1 to populate.")
        return

    audit = get_latest_scientific_audit(ticker)

    # Pre-fetch all endpoints for all trials to avoid N+1 queries
    nct_ids = [t["nct_id"] for t in trials]
    all_endpoints = get_endpoints_for_trials(nct_ids)

    # Group endpoints by nct_id
    from collections import defaultdict

    endpoints_by_trial = defaultdict(list)
    for ep in all_endpoints:
        endpoints_by_trial[ep.nct_id].append(ep)

    for trial in trials:
        nct = trial["nct_id"]
        phase = trial.get("phase") or "—"
        status = trial.get("status") or "—"
        title = trial.get("title") or nct

        verdict = None
        sci_score = None
        if audit and audit.nct_id == nct:
            verdict = audit.verdict
            sci_score = audit.validity_score

        verdict_color = {
            "STRONG_SCIENCE": "#22c55e",
            "SOLID": "#86efac",
            "WEAK": "#f97316",
            "VERY_WEAK": "#ef4444",
            "FRAUD_RISK": "#7f1d1d",
        }.get(verdict or "", "#64748b")

        with st.expander(
            f"**{phase}** | {nct} — {title[:70]}{'...' if len(title) > 70 else ''}",
            expanded=False,
        ):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Status", status)
            c2.metric("Relationship", trial.get("relationship_type") or "—")
            enrollment = trial.get("enrollment")
            c3.metric(
                "Enrollment",
                str(enrollment) if enrollment else "—",
                help="Actual enrollment only (enrollment_is_actual=1)",
            )
            if sci_score is not None:
                c4.markdown(
                    f"Science Score: <span style='color:{verdict_color}; font-weight:700'>"
                    f"{sci_score}/100 ({html.escape(str(verdict))})</span>",
                    unsafe_allow_html=True,
                )

            # Endpoints
            endpoints = endpoints_by_trial.get(nct, [])
            primary = [e.measure for e in endpoints if e.outcome_type == "primary"]
            secondary = [e.measure for e in endpoints if e.outcome_type == "secondary"]
            if primary:
                st.markdown(f"**Primary endpoint(s):** {'; '.join(primary)}")
            if secondary:
                st.markdown(f"**Secondary:** {'; '.join(secondary[:3])}")

            if audit and audit.nct_id == nct and audit.red_flags:
                try:
                    flags = json.loads(audit.red_flags)
                    if flags:
                        st.warning(
                            "Red flags: "
                            + "; ".join(f.get("phrase", str(f)) for f in flags[:3])
                        )
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Partnerships Module
# ---------------------------------------------------------------------------


def _render_partnerships(ticker: str):
    st.subheader("Partnerships (P)")
    partnerships = get_partnerships_for_ticker(ticker)

    if not partnerships:
        st.caption("No partnerships found. Run Crew 2 (Analysis) to populate.")
        return

    _SOURCE_BADGE = {
        "SEC_10K": "10-K",
        "CLINICALTRIALS": "CT.gov",
        "SEC_10K_AND_CLINICALTRIALS": "10-K+CT",
        "RSS_NEWS": "RSS",
    }

    for p in partnerships:
        tier_label = _partner_tier_label(p.partner_tier)
        conf_color = {"HIGH": "#22c55e", "MEDIUM": "#f97316", "LOW": "#94a3b8"}.get(
            p.confidence or "", "#94a3b8"
        )
        source_badge = _SOURCE_BADGE.get(p.source_type or "", p.source_type or "")

        with st.container():
            c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
            c1.markdown(f"**{p.partner_name}** — {p.drug_asset}")
            c2.caption(tier_label)
            c3.caption(p.partnership_type or "—")
            c4.markdown(
                f'<span style="color:{conf_color}">{html.escape(str(p.confidence or "—"))}</span>',
                unsafe_allow_html=True,
            )
            c5.caption(source_badge)

            if p.quality_score is not None:
                quality_color = "#22c55e" if p.quality_score >= 3 else "#f97316"
                st.markdown(
                    f"Quality score: <span style='color:{quality_color}'>"
                    f"{p.quality_score}/4</span>",
                    unsafe_allow_html=True,
                )

        st.divider()


# ---------------------------------------------------------------------------
# News Feed
# ---------------------------------------------------------------------------


def _render_news(ticker: str):
    _BADGE = {
        "m&a": '<span class="badge-ma">M&A</span>',
        "fda": '<span class="badge-fda">FDA</span>',
        "conference": '<span class="badge-conf">CONF</span>',
        "partnership": '<span class="badge-partner">PARTNER</span>',
        "analyst": '<span class="badge-analyst">ANALYST</span>',
    }

    with st.expander("News Feed (W) — Last 90 Days", expanded=False):
        news_tabs = st.tabs(
            ["All", "M&A", "FDA", "Conference", "Partnership", "Analyst"]
        )
        categories = [None, "m&a", "fda", "conference", "partnership", "analyst"]

        # ⚡ Bolt: Fetch all news once to prevent N+1 queries (6 DB calls -> 1 DB call)
        all_items = get_news_for_ticker(ticker, days=90, category=None)

        for tab, cat in zip(news_tabs, categories):
            with tab:
                if cat is None:
                    items = all_items
                else:
                    items = [item for item in all_items if item.category == cat]

                if not items:
                    st.caption("No headlines in this category.")
                    continue
                for item in items[:30]:
                    badge = _BADGE.get(item.category or "", "")
                    ts = (
                        item.published_at.strftime("%m/%d") if item.published_at else ""
                    )
                    st.markdown(
                        f"{badge} {html.escape(item.headline or '')} "
                        f"<small style='color:#64748b'>{html.escape(item.source or '')} · {ts}</small>",
                        unsafe_allow_html=True,
                    )


# ---------------------------------------------------------------------------
# Options Chain Snapshot
# ---------------------------------------------------------------------------


def _render_options(ticker: str):
    with st.expander("Options Chain Snapshot (C)", expanded=False):
        options = get_options_for_ticker(ticker, option_type="PUT")

        if not options:
            st.caption(
                "No options data. Run fetch_options ingestion or check E*TRADE connection."
            )
            return

        import pandas as pd
        import plotly.express as px

        rows = [
            {
                "Expiration": str(o.expiration),
                "Strike": o.strike,
                "IV %": f"{o.iv * 100:.1f}%" if o.iv else "—",
                "OI": o.oi or 0,
                "Bid": f"${o.bid:.2f}" if o.bid else "—",
                "Ask": f"${o.ask:.2f}" if o.ask else "—",
            }
            for o in options[:50]
        ]
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # IV curve by expiration
        iv_data = [
            {"expiration": str(o.expiration), "iv": (o.iv or 0) * 100}
            for o in options
            if o.iv
        ]
        if iv_data:
            iv_df = pd.DataFrame(iv_data)
            fig = px.line(
                iv_df,
                x="expiration",
                y="iv",
                title="IV % by Expiration",
                template="plotly_dark",
                labels={"expiration": "Expiration", "iv": "Implied Volatility (%)"},
            )
            fig.update_layout(paper_bgcolor="#0a0e1a", plot_bgcolor="#111827")
            st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Agent Signal Summary (sidebar cards)
# ---------------------------------------------------------------------------


def _render_agent_cards(ticker: str):
    st.markdown("### Agent Signals")

    profiler = get_latest_profiler(ticker)
    insider = get_latest_insider(ticker)
    smart_money = get_latest_smart_money(ticker)
    volatility = get_latest_volatility(ticker)

    if profiler:
        score = profiler.profiler_score or 0
        color = _score_color(score)
        st.markdown(
            f'<div class="glass-card">🏢 <b>Profiler</b><br>'
            f'Score: <span style="color:{color}">{score}/100</span><br>'
            f"TAM: {_fmt_usd(profiler.tam_estimate_usd)}<br>"
            f'Competition: {html.escape(str(profiler.competition_score or "—"))}</div>',
            unsafe_allow_html=True,
        )

    if insider:
        sig_color = (
            "#22c55e"
            if "BUY" in (insider.signal or "")
            else "#ef4444" if "SELL" in (insider.signal or "") else "#64748b"
        )
        st.markdown(
            f'<div class="glass-card">👤 <b>Insider</b><br>'
            f'Signal: <span style="color:{sig_color}">{html.escape(str(insider.signal or "—"))}</span><br>'
            f'Conviction: {insider.conviction_score or "—"}/10</div>',
            unsafe_allow_html=True,
        )

    if smart_money:
        sig_color = "#22c55e" if "CLUSTER" in (smart_money.signal or "") else "#64748b"
        conflict = "⚠️ Conflicting" if smart_money.conflicting_signal else ""
        st.markdown(
            f'<div class="glass-card">🏦 <b>Smart Money</b><br>'
            f'Signal: <span style="color:{sig_color}">{html.escape(str(smart_money.signal or "—"))}</span><br>'
            f'Conviction: {smart_money.conviction_score or "—"}/10 {conflict}</div>',
            unsafe_allow_html=True,
        )

    if volatility:
        status_color = {
            "APPROVED": "#22c55e",
            "REJECTED": "#ef4444",
            "CAUTION": "#f97316",
        }.get(volatility.status or "", "#64748b")
        st.markdown(
            f'<div class="glass-card">📈 <b>Volatility / CSP</b><br>'
            f'Status: <span style="color:{status_color}">{html.escape(str(volatility.status or "—"))}</span><br>'
            f"Annualized: {_fmt_pct(volatility.annualized_return_pct)}<br>"
            f'Strike: ${volatility.selected_strike or "—"}</div>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------


def render_page2(ticker: str | None):
    """Render the Analyst Workspace for a given ticker."""

    # ── Ticker selector in sidebar ────────────────────────────────────────
    with st.sidebar:
        st.markdown("### Tickers")
        tickers = get_all_active_tickers()

        if not tickers:
            st.caption("No active tickers. Load companies.csv or use Quick Onboard.")
            return

        selected = st.selectbox(
            "Select ticker",
            tickers,
            index=tickers.index(ticker) if ticker in tickers else None,
            placeholder="Choose a ticker...",
            key="_p2_ticker_select",
        )
        if selected != ticker:
            st.session_state.selected_ticker = selected
            st.rerun()

        ticker = selected

        st.divider()
        if ticker:
            _render_agent_cards(ticker)

    if not ticker:
        st.info("👈 Select a ticker from the sidebar or use Ctrl+K to search.")
        return

    # ── Context header ────────────────────────────────────────────────────
    _render_context_header(ticker)
    st.divider()

    # ── Main + RAG layout ─────────────────────────────────────────────────
    show_rag = st.session_state.get("rag_panel_open", False)
    if show_rag:
        col_main, col_rag = st.columns([3, 1])
    else:
        col_main = st.container()
        col_rag = None

    with col_main:
        _render_memo(ticker)
        st.divider()
        _render_trials(ticker)
        st.divider()
        _render_partnerships(ticker)
        _render_news(ticker)
        _render_options(ticker)

    # RAG panel toggle button
    rag_label = "Hide RAG (Ctrl+\\)" if show_rag else "Ask the RAG (R / Ctrl+\\)"
    if st.button(rag_label, key="_p2_rag_toggle"):
        st.session_state.rag_panel_open = not show_rag
        st.rerun()

    if col_rag is not None:
        with col_rag:
            render_rag_panel(ticker)
