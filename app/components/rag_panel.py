"""
Ask the RAG — ChromaDB + Ollama chat panel (Page 2 right float).

Searches the configured ChromaDB collection for relevant context,
then sends it + the user's question to llama3.1:8b via OllamaLLMTool.

UI spec: docs/ui-ux/UI_CONCEPT.md v3.5p Section 2.8
"""

from __future__ import annotations

import os

import streamlit as st

from app.queries import search_rag


def _call_ollama(prompt: str) -> str:
    """
    Call llama3.1:8b via OllamaLLMTool and return the response string.
    Falls back to an error string if Ollama is unreachable.
    """
    try:
        from src.tools.ollama_tool import OllamaLLMTool

        tool = OllamaLLMTool()
        return tool._run(
            prompt=prompt,
            model="llama3.1:8b",
            temperature=0.3,
        )
    except Exception as exc:
        import logging

        logging.exception("Ollama call failed")
        return "⚠️ I couldn't connect to the local AI model (Ollama). Please ensure the Ollama service is running and try your question again."


def render_rag_panel(ticker: str) -> None:
    """Render the persistent RAG chat panel pre-contextualized to ticker."""

    st.markdown(f"### Ask the RAG — {ticker}")
    st.caption("Sources: sec_filings · agent_memos · trial_protocols")

    # Collection selector
    collection = st.selectbox(
        "Collection",
        ["agent_memos", "sec_filings", "trial_protocols"],
        index=None,
        placeholder="Choose a collection...",
        key=f"_rag_collection_{ticker}",
    )

    # Chat history in session state (per ticker)
    history_key = f"_rag_history_{ticker}"
    if history_key not in st.session_state:
        st.session_state[history_key] = []

    # Display chat history
    for msg in st.session_state[history_key]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if not collection:
        st.info(
            "👆 Please select a knowledge base collection to start asking questions."
        )

    # Input
    user_input = st.chat_input(
        f"Ask about {ticker}...",
        key=f"_rag_input_{ticker}",
        disabled=not collection,
    )

    if user_input:
        # Add user message
        st.session_state[history_key].append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        # Retrieve context
        with st.spinner("Searching knowledge base..."):
            chunks = search_rag(
                query=user_input,
                collection=collection,
                top_k=5,
                ticker_filter=ticker,
            )

        # Build prompt
        if chunks:
            context_text = "\n\n---\n\n".join(
                f"[Source: {c['metadata'].get('source_id', 'unknown')}]\n{c['document']}"
                for c in chunks
            )
            prompt = (
                f"You are a biotech investment research assistant. "
                f"Use only the following context to answer the question about {ticker}.\n\n"
                f"CONTEXT:\n{context_text}\n\n"
                f"QUESTION: {user_input}\n\n"
                f"Answer concisely with inline citations to the source sections."
            )
        else:
            prompt = (
                f"You are a biotech investment research assistant. "
                f"No relevant context was found in the knowledge base for '{user_input}' "
                f"about {ticker}. State that clearly and offer what general knowledge you have."
            )

        # Get LLM response
        with st.spinner("Generating response..."):
            response = _call_ollama(prompt)

        # Display response
        with st.chat_message("assistant"):
            st.markdown(response)

        # Show source chunks in expander
        if chunks:
            with st.expander(f"Sources ({len(chunks)} chunks)", expanded=False):
                for i, chunk in enumerate(chunks):
                    dist = chunk.get("distance")
                    dist_str = f" · dist={dist:.3f}" if dist is not None else ""
                    meta = chunk.get("metadata", {})
                    source_id = meta.get("source_id", f"chunk_{i+1}")
                    st.caption(f"**{source_id}**{dist_str}")
                    st.text(
                        chunk["document"][:400] + "..."
                        if len(chunk["document"]) > 400
                        else chunk["document"]
                    )

        # Append to history
        st.session_state[history_key].append({"role": "assistant", "content": response})

    # Clear button
    if st.session_state[history_key]:
        if st.button(
            "Clear chat",
            key=f"_rag_clear_{ticker}",
            help="Clear chat history",
            use_container_width=True,
        ):
            st.session_state[history_key] = []
            st.rerun()
