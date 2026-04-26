"""
Biotech-Analyzer v3.5 — Catalyst Workspace

Two-page Streamlit app:
  Page 1 — Catalyst Timeline: Portfolio HUD, Gantt chart, Agent Signals Feed, CSP Workbench
  Page 2 — Analyst Workspace: per-ticker deep dive with memo, trials, RAG chat

Keyboard shortcuts injected via JS:
  Ctrl+K     — Omni-search
  Esc        — Back to Page 1
  Ctrl+\\    — Toggle RAG panel
  Shift+?    — Keyboard help overlay
  T          — Jump to Gantt timeline
  F          — Open filter panel
  O          — Quick onboard (new ticker)
  N          — News feed
  R          — RAG panel (Page 2)
  M          — Memo section (Page 2)
  C          — Options chain (Page 2)
  P          — Partnerships (Page 2)
  W          — News feed (Page 2)

UI spec: docs/ui-ux/UI_CONCEPT.md v3.5p
"""

import os
import sys
from pathlib import Path

import streamlit as st

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pages.page1_timeline import render_page1
from app.pages.page2_workspace import render_page2

# ---------------------------------------------------------------------------
# Dark-mode glassmorphism CSS
# ---------------------------------------------------------------------------

_DARK_CSS = """
<style>
/* Base Theme */
body, .stApp {
    background: linear-gradient(135deg, #050b14 0%, #0a0e1a 100%);
    color: #f1f5f9;
    font-family: 'Inter', 'Segoe UI', sans-serif;
    font-size: 14px;
}

/* Glassmorphic Cards */
.glass-card {
    background: rgba(255, 255, 255, 0.03);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 16px;
    padding: 1.25rem;
    margin-bottom: 1rem;
    box-shadow: 0 4px 30px rgba(0, 0, 0, 0.2);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.glass-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.4);
    border: 1px solid rgba(255, 255, 255, 0.15);
}

/* Sidebar */
[data-testid="stSidebar"] {
    background-color: rgba(10, 14, 26, 0.95);
    backdrop-filter: blur(20px);
    border-right: 1px solid rgba(255, 255, 255, 0.06);
}

/* Metrics (HUD) */
[data-testid="stMetric"] {
    background: rgba(255, 255, 255, 0.03);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
    padding: 1rem;
    box-shadow: 0 4px 20px rgba(0,0,0,0.15);
}
[data-testid="stMetricValue"] {
    font-size: 1.8rem !important;
    font-weight: 700 !important;
    color: #e2e8f0;
}
[data-testid="stMetricLabel"] {
    font-size: 0.85rem !important;
    font-weight: 600 !important;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

/* Badges */
.badge-ma      { background: rgba(146, 64, 14, 0.2); color: #fcd34d; border: 1px solid rgba(252, 211, 77, 0.3); border-radius: 6px; padding: 3px 8px; font-size: 11px; font-weight: 700; }
.badge-fda     { background: rgba(30, 58, 95, 0.3); color: #93c5fd; border: 1px solid rgba(147, 197, 253, 0.3); border-radius: 6px; padding: 3px 8px; font-size: 11px; font-weight: 700; }
.badge-conf    { background: rgba(26, 46, 26, 0.4); color: #86efac; border: 1px solid rgba(134, 239, 172, 0.3); border-radius: 6px; padding: 3px 8px; font-size: 11px; font-weight: 700; }
.badge-partner { background: rgba(45, 30, 74, 0.4); color: #c4b5fd; border: 1px solid rgba(196, 181, 253, 0.3); border-radius: 6px; padding: 3px 8px; font-size: 11px; font-weight: 700; }
.badge-analyst { background: rgba(28, 28, 28, 0.5); color: #cbd5e1; border: 1px solid rgba(203, 213, 225, 0.3); border-radius: 6px; padding: 3px 8px; font-size: 11px; font-weight: 700; }

/* Neon Highlights */
.neg-ev-badge {
    background: rgba(6, 78, 59, 0.3); 
    color: #34d399;
    border: 1px solid #10b981; 
    border-radius: 6px;
    padding: 4px 10px; 
    font-size: 12px; 
    font-weight: 700;
    box-shadow: 0 0 10px rgba(16, 185, 129, 0.3);
}

.rumor-alert-banner {
    background: rgba(146, 64, 14, 0.15);
    border-left: 4px solid #f59e0b;
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 1rem;
    color: #fde68a;
    box-shadow: 0 4px 15px rgba(245, 158, 11, 0.1);
}

/* Inputs & Buttons */
.stTextInput input, .stSelectbox select, .stMultiSelect div[data-baseweb="select"] {
    background-color: rgba(31, 41, 55, 0.6) !important; 
    color: #f8fafc !important;
    border: 1px solid rgba(255,255,255,0.12) !important; 
    border-radius: 8px !important;
    backdrop-filter: blur(8px);
}
.stTextInput input:focus, .stSelectbox select:focus {
    border-color: #3b82f6 !important;
    box-shadow: 0 0 0 1px #3b82f6 !important;
}
.stTextInput input:focus-visible, .stSelectbox select:focus-visible, .stMultiSelect div[data-baseweb="select"]:focus-visible {
    outline: 2px solid #3b82f6 !important;
    outline-offset: 2px !important;
}

div[data-testid="stButton"] button {
    background: rgba(59,130,246,0.1) !important; 
    color: #60a5fa !important;
    border: 1px solid rgba(59,130,246,0.3) !important; 
    border-radius: 8px !important; 
    font-weight: 600 !important;
    letter-spacing: 0.02em !important;
    transition: all 0.2s !important;
}
div[data-testid="stButton"] button:focus-visible {
    outline: 2px solid #3b82f6 !important;
    outline-offset: 2px !important;
}
div[data-testid="stButton"] button:hover {
    background: rgba(59,130,246,0.2) !important;
    border-color: #3b82f6 !important;
    color: #93c5fd !important;
    box-shadow: 0 0 12px rgba(59, 130, 246, 0.2) !important;
    transform: translateY(-1px) !important;
}
div[data-testid="stButton"] button[data-testid="baseButton-primary"] {
    background: rgba(59,130,246,0.25) !important;
    border-color: #3b82f6 !important;
    color: #fff !important;
    box-shadow: 0 0 15px rgba(59, 130, 246, 0.3) !important;
}

/* Details/Expanders */
[data-testid="stExpander"] {
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
}
[data-testid="stExpander"] summary {
    padding: 1rem !important;
    font-weight: 600;
    color: #e2e8f0;
}
[data-testid="stExpander"] summary:focus-visible {
    outline: 2px solid #3b82f6 !important;
    outline-offset: 2px !important;
    border-radius: 12px !important;
}

/* Scrollbars */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.2); }

/* Dividers */
hr {
    border-color: rgba(255, 255, 255, 0.08) !important;
    margin: 1.5rem 0 !important;
}
</style>
"""

# ---------------------------------------------------------------------------
# Keyboard shortcut JS
# ---------------------------------------------------------------------------

_KEYBOARD_JS = """
<script>
(function() {
  var _fired = false;
  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      window.parent.postMessage({type:'streamlit:keyshortcut', shortcut:'omni_search'}, '*');
    }
    if (e.key === 'Escape' && !e.ctrlKey) {
      window.parent.postMessage({type:'streamlit:keyshortcut', shortcut:'back'}, '*');
    }
    if ((e.ctrlKey || e.metaKey) && e.key === '\\\\') {
      e.preventDefault();
      window.parent.postMessage({type:'streamlit:keyshortcut', shortcut:'toggle_rag'}, '*');
    }
    if (e.key === 'o' && !e.ctrlKey && !e.metaKey) {
      window.parent.postMessage({type:'streamlit:keyshortcut', shortcut:'onboard'}, '*');
    }
    if (e.shiftKey && e.key === '?') {
      window.parent.postMessage({type:'streamlit:keyshortcut', shortcut:'help'}, '*');
    }
  });
})();
</script>
"""


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------


def _init_state():
    # Sync selected_ticker with URL query parameter ?ticker=...
    params = st.query_params
    url_ticker = params.get("ticker", None)

    defaults = {
        "active_page": 1,
        "selected_ticker": url_ticker,
        "omni_search_open": False,
        "onboard_modal_open": False,
        "rag_panel_open": False,
        "help_open": False,
        "filter_signal_type": [],
        "filter_event_type": [],
        "filter_company_type": "All",
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

    # If the ticker in state doesn't match URL, update URL
    if (
        st.session_state.get("selected_ticker")
        and st.query_params.get("ticker") != st.session_state.selected_ticker
    ):
        st.query_params["ticker"] = st.session_state.selected_ticker


# ---------------------------------------------------------------------------
# Omni-search modal
# ---------------------------------------------------------------------------


def _render_omni_search():
    from app.queries import get_all_active_tickers

    if not st.session_state.get("omni_search_open"):
        return

    with st.container():
        st.markdown("### Search Ticker (Ctrl+K)")
        query = st.text_input(
            "Ticker symbol", key="_omni_input", placeholder="MRNA, BIIB, CRSP..."
        )
        tickers = get_all_active_tickers()
        matches = [
            t for t in tickers if query and t.startswith(query.upper())
        ] or tickers[:20]

        if matches:
            choice = st.selectbox("Select", matches, index=None, key="_omni_select")
            if st.button(
                "Open",
                key="_omni_go",
                disabled=not choice,
                help=(
                    "Select a ticker to open" if not choice else "Open selected ticker"
                ),
                use_container_width=True,
            ):
                st.session_state.selected_ticker = choice
                st.session_state.active_page = 2
                st.session_state.omni_search_open = False
                st.rerun()

        if st.button(
            "Cancel",
            key="_omni_cancel",
            help="Close omni-search modal",
            use_container_width=True,
        ):
            st.session_state.omni_search_open = False
            st.rerun()


# ---------------------------------------------------------------------------
# Quick Onboard modal
# ---------------------------------------------------------------------------


def _render_onboard_modal():
    if not st.session_state.get("onboard_modal_open"):
        return

    with st.container():
        st.markdown("### Quick Onboard Ticker (O)")
        new_ticker = (
            st.text_input(
                "Ticker symbol", key="_onboard_ticker", placeholder="e.g. CRSP"
            )
            .upper()
            .strip()
        )
        col1, col2 = st.columns(2)
        with col1:
            if (
                st.button(
                    "Onboard",
                    key="_onboard_go",
                    disabled=not new_ticker,
                    help=(
                        "Enter a ticker symbol to onboard"
                        if not new_ticker
                        else "Onboard this ticker"
                    ),
                    use_container_width=True,
                )
                and new_ticker
            ):
                with st.spinner(f"Onboarding {new_ticker}..."):
                    try:
                        from scripts.onboard_company import onboard

                        status = onboard(new_ticker)
                        st.toast(f"{new_ticker}: {status}", icon="✅")
                        st.session_state.selected_ticker = new_ticker
                        st.session_state.active_page = 2
                    except Exception as exc:
                        import logging

                        logging.exception("Onboarding failed for %s", new_ticker)
                        st.toast(
                            f"Failed to onboard {new_ticker}. Please verify the ticker symbol and try again.",
                            icon="❌",
                        )
                st.session_state.onboard_modal_open = False
                st.rerun()
        with col2:
            if st.button(
                "Cancel",
                key="_onboard_cancel",
                help="Close onboard modal",
                use_container_width=True,
            ):
                st.session_state.onboard_modal_open = False
                st.rerun()


# ---------------------------------------------------------------------------
# Help overlay
# ---------------------------------------------------------------------------


def _render_help():
    if not st.session_state.get("help_open"):
        return
    with st.expander("Keyboard Shortcuts (Shift+?)", expanded=True):
        st.markdown("""
| Shortcut | Action |
|---|---|
| `Ctrl+K` | Omni-search |
| `Esc` | Back to Timeline |
| `Ctrl+\\` | Toggle RAG panel |
| `Shift+?` | This help |
| `O` | Quick onboard |
| `T` | Jump to Gantt |
| `F` | Open filters |
| `N` | News feed |
| `R` | RAG panel (Page 2) |
| `M` | Memo (Page 2) |
| `C` | Options chain (Page 2) |
| `P` | Partnerships (Page 2) |
| `W` | News (Page 2) |
        """)
        if st.button(
            "Close",
            key="_help_close",
            help="Close keyboard shortcuts help",
            use_container_width=True,
        ):
            st.session_state.help_open = False
            st.rerun()


# ---------------------------------------------------------------------------
# Navigation bar
# ---------------------------------------------------------------------------


def _render_nav():
    c1, c2, c3, c4, c5, c6 = st.columns([1.5, 1, 1, 1, 1, 1])
    with c1:
        st.markdown(
            "<h4 style='margin-top: 5px; color: #f1f5f9;'>Biotech-Analyzer</h4>",
            unsafe_allow_html=True,
        )
    with c2:
        if st.button(
            "Catalyst Timeline",
            key="_nav_p1",
            type="primary" if st.session_state.active_page == 1 else "secondary",
            use_container_width=True,
        ):
            st.session_state.active_page = 1
            st.session_state.omni_search_open = False
            st.session_state.onboard_modal_open = False
            st.session_state.help_open = False
            st.rerun()
    with c3:
        label = (
            f"Analyst: {st.session_state.selected_ticker}"
            if st.session_state.selected_ticker
            else "Workspace"
        )
        if st.button(
            label,
            key="_nav_p2",
            type="primary" if st.session_state.active_page == 2 else "secondary",
            use_container_width=True,
        ):
            st.session_state.active_page = 2
            st.session_state.omni_search_open = False
            st.session_state.onboard_modal_open = False
            st.session_state.help_open = False
            st.rerun()
    with c4:
        if st.button(
            "Search",
            key="_nav_search",
            help="Omni-search (Ctrl+K)",
            use_container_width=True,
        ):
            st.session_state.omni_search_open = True
            st.session_state.onboard_modal_open = False
            st.session_state.help_open = False
            st.rerun()
    with c5:
        if st.button(
            "Onboard",
            key="_nav_onboard",
            help="Quick Onboard (O)",
            use_container_width=True,
        ):
            st.session_state.onboard_modal_open = True
            st.session_state.omni_search_open = False
            st.session_state.help_open = False
            st.rerun()
    with c6:
        if st.button(
            "Help",
            key="_nav_help",
            help="Show keyboard shortcuts (Shift+?)",
            use_container_width=True,
        ):
            st.session_state.help_open = not st.session_state.get("help_open", False)
            st.session_state.omni_search_open = False
            st.session_state.onboard_modal_open = False
            st.rerun()

    st.divider()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    st.set_page_config(
        page_title="Biotech-Analyzer v3.5",
        page_icon="🧬",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    st.markdown(_DARK_CSS, unsafe_allow_html=True)
    st.components.v1.html(_KEYBOARD_JS, height=0)

    _init_state()
    _render_nav()

    # Overlays
    _render_omni_search()
    _render_onboard_modal()
    _render_help()

    # Route
    if st.session_state.active_page == 1:
        render_page1()
    else:
        render_page2(st.session_state.get("selected_ticker"))


if __name__ == "__main__":
    main()
