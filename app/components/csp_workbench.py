"""
CSP Workbench — options expiration overlay drawer (Page 1 bottom).

Shows options expiration cycles overlaid on the catalyst timeline to
validate Volatility Agent rule: catalyst must be OUTSIDE the expiration window.

UI spec: docs/ui-ux/UI_CONCEPT.md v3.5p Section 1.6
"""

from __future__ import annotations

import html
from datetime import date

import streamlit as st


def render_csp_workbench(catalyst_data: list[dict]) -> None:
    """
    Render the CSP Workbench: ticker selector, options chain, catalyst conflict check.

    Args:
        catalyst_data: catalyst list from Page 1 (already filtered)
    """
    from app.queries import (
        get_all_active_tickers,
        get_latest_volatility,
        get_options_for_ticker,
    )

    tickers = get_all_active_tickers()
    if not tickers:
        st.caption("No active tickers available.")
        return

    col1, col2 = st.columns([1, 3])
    with col1:
        ticker = st.selectbox(
            "Select ticker",
            tickers,
            index=None,
            placeholder="Choose a ticker...",
            key="_csp_ticker",
        )

    if not ticker:
        st.info(
            "👆 Please select a ticker to view its options expiration overlap analysis."
        )
        return

    with col2:
        # Volatility agent recommendation
        vol = get_latest_volatility(ticker)
        if vol:
            status_color = {
                "APPROVED": "#22c55e",
                "REJECTED": "#ef4444",
                "CAUTION": "#f97316",
            }.get(vol.status or "", "#64748b")

            c1, c2, c3, c4 = st.columns(4)
            c1.markdown(
                f"**Status:** <span style='color:{status_color}'>{html.escape(str(vol.status or '—'))}</span>",
                unsafe_allow_html=True,
            )
            c2.metric(
                "Strike", f"${vol.selected_strike:.2f}" if vol.selected_strike else "—"
            )
            c3.metric(
                "Annualized",
                (
                    f"{vol.annualized_return_pct * 100:.1f}%"
                    if vol.annualized_return_pct
                    else "—"
                ),
            )
            c4.metric(
                "IV%",
                f"{vol.iv_pct * 100:.1f}%" if vol.iv_pct else "—",
            )

            if vol.next_catalyst_within_window:
                st.warning(
                    f"CATALYST CONFLICT: {vol.next_catalyst_date} falls within the "
                    "expiration window — Volatility Agent REJECTED this CSP setup.",
                    icon="⚠️",
                )
            elif vol.risk_warnings:
                try:
                    import json

                    warnings = json.loads(vol.risk_warnings)
                    for w in warnings[:3]:
                        st.caption(f"⚠ {w}")
                except Exception:
                    st.caption(vol.risk_warnings)
        else:
            st.caption(
                f"No Volatility Agent output for {ticker}. Run Crew 3 to populate."
            )

    # Options chain table
    st.markdown("**Available PUT options (< 45 DTE)**")
    options = get_options_for_ticker(ticker, option_type="PUT")

    # Filter to < 45 DTE
    today = date.today()
    from datetime import timedelta

    cutoff = today + timedelta(days=45)
    options = [o for o in options if o.expiration <= cutoff]

    if not options:
        st.caption("No PUT options within 45 DTE. Check fetch_options ingestion.")
        return

    # Catalyst conflict map
    ticker_catalysts = {
        str(c["event_date"]): c for c in catalyst_data if c["ticker"] == ticker
    }

    try:
        import pandas as pd

        # Pre-parse dates to avoid O(N*M) string allocations inside the loop
        parsed_catalyst_dates = [
            date.fromisoformat(cat_date) for cat_date in ticker_catalysts
        ]

        rows = []
        for o in options[:30]:
            dte = (o.expiration - today).days
            conflict = any(
                today <= cat_date <= o.expiration for cat_date in parsed_catalyst_dates
            )
            rows.append(
                {
                    "Expiration": str(o.expiration),
                    "DTE": dte,
                    "Strike": f"${o.strike:.2f}",
                    "IV %": f"{o.iv * 100:.1f}%" if o.iv else "—",
                    "OI": o.oi or 0,
                    "Bid": f"${o.bid:.2f}" if o.bid else "—",
                    "Catalyst Conflict": "YES" if conflict else "—",
                }
            )

        df = pd.DataFrame(rows)
        # Highlight catalyst conflicts
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Catalyst Conflict": st.column_config.TextColumn(
                    "Catalyst Conflict",
                    help="Catalyst falls within this expiration window",
                ),
            },
        )
    except ImportError:
        for o in options[:10]:
            dte = (o.expiration - today).days
            st.caption(
                f"{o.expiration} | DTE={dte} | Strike=${o.strike:.2f} "
                f"| IV={o.iv * 100:.1f}%"
                if o.iv
                else f"{o.expiration}"
            )
