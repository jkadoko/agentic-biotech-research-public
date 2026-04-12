"""
Plotly Gantt chart component for the Catalyst Timeline.

Event node colors:
  Red    — market_impact_score ≥ 8 (PDUFA, Phase 3 top-line)
  Orange — score 6–7 (AdComm, interim analysis)
  Blue   — score 4–5 (conference)
  Green  — RSS_NEWS sourced signals

UI spec: docs/ui-ux/UI_CONCEPT.md v3.5p Section 1.2
"""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st


def render_gantt(catalyst_data: list[dict]) -> None:
    """
    Render an interactive Plotly Gantt (timeline) chart from catalyst_data.

    Args:
        catalyst_data: list of dicts with keys:
            ticker, event_type, event_date (date), event_name,
            drug_name, market_impact_score, source_type, color (hex)
    """
    if not catalyst_data:
        st.caption("No catalysts to display.")
        return

    try:
        import pandas as pd
        import plotly.express as px
    except ImportError:
        st.warning(
            "plotly and pandas are required for the Gantt chart. Run: pip install plotly pandas"
        )
        _render_table_fallback(catalyst_data)
        return

    # Build dataframe — plotly timeline needs Start + Finish columns
    rows = []
    for c in catalyst_data:
        event_date = c["event_date"]
        if isinstance(event_date, str):
            event_date = date.fromisoformat(event_date)

        # Each event is a 1-day wide bar
        rows.append(
            {
                "Ticker": c["ticker"],
                "Event": f"{c['ticker']}: {c['event_name']}",
                "Drug": c.get("drug_name") or "",
                "Type": c["event_type"],
                "Start": str(event_date),
                "Finish": str(event_date + timedelta(days=3)),  # minimum bar width
                "Impact": c.get("market_impact_score") or 0,
                "Color": c.get("color", "#3b82f6"),
                "Tooltip": (
                    f"<b>{c['ticker']}</b><br>"
                    f"{c['event_name']}<br>"
                    f"Drug: {c.get('drug_name') or 'N/A'}<br>"
                    f"Impact: {c.get('market_impact_score') or '—'}/10<br>"
                    f"Date: {event_date}"
                ),
            }
        )

    df = pd.DataFrame(rows)
    df = df.sort_values("Start")

    fig = px.timeline(
        df,
        x_start="Start",
        x_end="Finish",
        y="Ticker",
        color="Color",
        hover_name="Event",
        hover_data={
            "Tooltip": True,
            "Start": False,
            "Finish": False,
            "Color": False,
            "Ticker": False,
            "Impact": True,
        },
        template="plotly_dark",
        title="",
        # ⚡ Bolt: Fast mapping using unique() instead of slow df.iterrows()
        color_discrete_map={c: c for c in df["Color"].unique()},
    )

    # Style
    fig.update_layout(
        paper_bgcolor="#0a0e1a",
        plot_bgcolor="#111827",
        font_color="#e2e8f0",
        xaxis=dict(
            showgrid=True,
            gridcolor="rgba(255,255,255,0.05)",
            tickfont=dict(size=11),
        ),
        yaxis=dict(
            showgrid=False,
            tickfont=dict(size=11),
            categoryorder="total ascending",
        ),
        showlegend=False,
        height=max(300, min(800, len(df["Ticker"].unique()) * 30 + 100)),
        margin=dict(l=80, r=20, t=20, b=40),
    )

    # Today line
    fig.add_vline(
        x=str(date.today()),
        line_dash="dash",
        line_color="rgba(255,255,255,0.3)",
        annotation_text="Today",
        annotation_font_color="#94a3b8",
    )

    # Click handler hint
    st.plotly_chart(fig, use_container_width=True, key="gantt_chart")
    st.caption(
        "Red = PDUFA/Phase3 · Orange = AdComm/Interim · Blue = Conference · Green = RSS Signal"
    )


def _render_table_fallback(catalyst_data: list[dict]) -> None:
    """Plain table fallback when plotly is not available."""
    st.markdown("**Upcoming Catalysts**")
    for c in sorted(catalyst_data, key=lambda x: str(x.get("event_date", ""))):
        d = c.get("event_date")
        score = c.get("market_impact_score")
        st.markdown(
            f"- **{c['ticker']}** | {c['event_type']} | " f"{d} | Impact: {score}/10"
        )
