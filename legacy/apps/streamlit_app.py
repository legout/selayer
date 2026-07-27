"""Streamlit UI for selayer_chat — Tier 1: session memory + result cache + replayable history.

Run with:
    streamlit run apps/streamlit_app.py
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from selayer_chat import AnalyticalBackend, LLMConfig

from apps._common import (
    chart_kind_options,
    format_summary,
    history_cap,
    pick_chart,
    plotly_figure,
)

st.set_page_config(page_title="selayer_chat", page_icon="💬", layout="wide")
st.title("selayer_chat")
st.caption("Ask questions about your data; the LLM generates DuckDB SQL.")


# ---------------------------------------------------------------------------
# Render helpers — defined first so the main block can call them freely
# ---------------------------------------------------------------------------


def _render_assistant(msg: dict, chart_kind: str) -> None:
    """Render one assistant history entry, with df + chart + SQL if it has a result."""
    result = msg.get("result")
    if result is None:
        st.markdown(msg["content"])
        return
    if not getattr(result, "ok", False):
        st.error(msg["content"])
        return
    with st.expander("Generated SQL"):
        st.code(result.sql, language="sql")
        st.caption(
            f"model: {result.model}  •  "
            f"{result.duration_ms:.0f} ms  •  "
            f"{result.rows} rows  •  "
            f"tokens: {result.tokens_used or '?'}"
        )
    if result.df is not None:
        st.dataframe(result.df, use_container_width=True)
        choice = pick_chart(result.df, override=chart_kind)
        fig = plotly_figure(result.df, choice)
        if fig is not None:
            st.plotly_chart(fig, use_container_width=True)
        st.download_button(
            "Download CSV",
            result.df.write_csv().encode("utf-8"),
            file_name="result.csv",
            mime="text/csv",
            key=f"dl_{id(result)}",
        )


# ---------------------------------------------------------------------------
# Session-scoped state
# ---------------------------------------------------------------------------


def _init_state() -> None:
    if "history" not in st.session_state:
        # list of {"role": "user"|"assistant", "content": str, "result": QueryResult | None}
        st.session_state.history = []
    if "cache" not in st.session_state:
        # {(backend.id, question): QueryResult} — survives reruns, dies with the tab
        st.session_state.cache = {}


def _cached_ask(backend: AnalyticalBackend, question: str) -> tuple[object, bool]:
    """Return (result, was_cached). Cached results skip the LLM round-trip."""
    key = (backend.id, question)
    cache = st.session_state.cache
    if key in cache:
        return cache[key], True
    result = backend.ask(question)
    cache[key] = result
    return result, False


def _push_history(user_content: str, assistant_content: str, result) -> None:
    st.session_state.history.append({"role": "user", "content": user_content})
    st.session_state.history.append(
        {"role": "assistant", "content": assistant_content, "result": result}
    )
    cap = history_cap()
    if len(st.session_state.history) > cap:
        st.session_state.history = st.session_state.history[-cap:]


# ---------------------------------------------------------------------------
# Cached resource
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner="Loading selayer file(s)…")
def _build_backend(selayer_path: str, _api_key: str, _model: str, _base_url: str):
    return AnalyticalBackend(
        selayer_paths=[selayer_path],
        llm=LLMConfig(api_key=_api_key, model=_model, base_url=_base_url),
    )


# ---------------------------------------------------------------------------
# Sidebar: configuration
# ---------------------------------------------------------------------------


with st.sidebar:
    st.header("Configuration")

    selayer_path = st.text_input(
        "selayer YAML path",
        value="ecommerce_semantic_layer.yaml",
        help="Path to a selayer YAML file (relative to CWD or absolute).",
    )
    api_key = st.text_input(
        "API key", type="password", value=os.environ.get("OPENAI_API_KEY", "")
    )
    base_url = st.text_input(
        "OpenAI-compatible base URL",
        value=os.environ.get("OPENAI_BASE_URL", "https://api.siemens.com/llm/v1"),
        help="Any endpoint that follows the OpenAI chat-completions API.",
    )
    model = st.text_input("Model", value=os.environ.get("OPENAI_MODEL", "qwen-3.6-27b"))

    chart_kind = st.selectbox("Chart", chart_kind_options(), index=0)

    if Path(selayer_path).exists():
        st.success(f"File found: {Path(selayer_path).name}")
    else:
        st.error(f"File not found: {selayer_path}")

    if not api_key:
        st.warning("Enter an API key to enable queries.")

    if st.button("🧹 Clear session history", use_container_width=True):
        st.session_state.history = []
        st.session_state.cache = {}
        st.rerun()


# ---------------------------------------------------------------------------
# Main: schema + chat
# ---------------------------------------------------------------------------


_init_state()

if selayer_path and Path(selayer_path).exists() and api_key:
    try:
        backend = _build_backend(selayer_path, api_key, model, base_url)
    except Exception as e:
        st.error(f"Failed to load backend: {e}")
        st.stop()

    with st.expander(f"📐 Schema: {backend.schema.layer_name}", expanded=False):
        st.write(backend.schema.layer_description)
        st.divider()

        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown("**Measures**")
            for f in backend.schema.fields:
                if f.kind == "measure":
                    st.markdown(f"- `{f.name}` — {f.description}")
        with col2:
            st.markdown("**Dimensions**")
            for f in backend.schema.fields:
                if f.kind == "dimension":
                    st.markdown(f"- `{f.name}` — {f.description}")
        with col3:
            st.markdown("**Metrics**")
            for f in backend.schema.fields:
                if f.kind == "metric":
                    st.markdown(f"- `{f.name}` — {f.description}")

        st.divider()
        st.markdown("**Tables**")
        for t in backend.schema.tables:
            cols = ", ".join(c["name"] for c in t.columns)
            st.markdown(f"- `{t.name}` ({len(t.columns)} cols) — `{cols}`")

    # ---- replayable history -----------------------------------------------

    for msg in st.session_state.history:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                _render_assistant(msg, chart_kind)

    # ---- ask ------------------------------------------------------------

    question = st.chat_input("Ask a question about your data…")
    if question:
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Asking the LLM…"):
                result, was_cached = _cached_ask(backend, question)
            summary = format_summary(result) + ("  ↩︎ cached" if was_cached else "")
            st.markdown(summary)
            # Render the new result inline using the same renderer that history uses
            temp_msg = {"role": "assistant", "content": summary, "result": result}
            _render_assistant(temp_msg, chart_kind)

        _push_history(question, summary, result)

    if not st.session_state.history:
        st.info(
            "Try: *Revenue and order count by country × quarter matrix* — "
            "or pick one from the schema panel above."
        )

else:
    st.info("Configure the selayer YAML path and API key in the sidebar to begin.")
