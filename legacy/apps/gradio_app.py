"""Gradio UI for selayer_chat — uses gr.ChatInterface + additional_outputs.

Run with:
    python apps/gradio_app.py

Tier 1 changes:
    * gr.State holds {backend, history, cache} — survives per-session.
    * Cache short-circuits repeat questions.
    * gr.ChatInterface (default type="messages" in Gradio 6) renders the
      chat history natively with optional embedded widgets per message.
    * additional_outputs= side-panel components always show the LATEST
      dataframe + chart (cache makes scrolling back to re-render instant).
"""

from __future__ import annotations

import os
from pathlib import Path

import gradio as gr
import plotly.express as px
import polars as pl

from selayer_chat import AnalyticalBackend, LLMConfig

from apps._common import (
    chart_kind_options,
    format_summary,
    history_cap,
    pick_chart,
)


def _initial_state() -> dict:
    return {"backend": None, "history": [], "cache": {}}


def _make_backend(
    selayer_path: str, api_key: str, base_url: str, model: str
) -> AnalyticalBackend:
    if not Path(selayer_path).exists():
        raise FileNotFoundError(f"selayer file not found: {selayer_path}")
    if not api_key:
        raise ValueError("API key required")
    return AnalyticalBackend(
        selayer_paths=[selayer_path],
        llm=LLMConfig(api_key=api_key, base_url=base_url, model=model),
    )


def _schema_md(backend: AnalyticalBackend | None) -> str:
    if backend is None:
        return "_(backend not loaded)_"
    s = backend.schema
    lines = [f"## {s.layer_name}", s.layer_description, ""]
    for kind, label in [
        ("measure", "Measures"),
        ("dimension", "Dimensions"),
        ("metric", "Metrics"),
    ]:
        block = [f for f in s.fields if f.kind == kind]
        if not block:
            continue
        lines.append(f"### {label}")
        for f in block:
            lines.append(f"- `{f.name}` — {f.description}")
        lines.append("")
    return "\n".join(lines)


def _fig(df: pl.DataFrame, choice):
    if choice.kind == "none" or not choice.y:
        return None
    if choice.kind == "bar":
        return px.bar(df, x=choice.x, y=choice.y[0])
    if choice.kind == "line":
        return px.line(df, x=choice.x, y=choice.y)
    if choice.kind == "scatter":
        return px.scatter(df, x=choice.x, y=choice.y[0])
    if choice.kind == "area":
        return px.area(df, x=choice.x, y=choice.y)
    return None


# ---------------------------------------------------------------------------
# Chat callback — signature: (message, history, *additional_inputs) → reply + additional_outputs
# ---------------------------------------------------------------------------


def chat_fn(message, history, state, chart_kind):
    # NOTE: Gradio's ChatInterface wraps our return like this:
    #     response, *additional_outputs = fn_return
    #     internal_return = (response, history, *additional_outputs)
    # So fn must return: (chat_reply, *additional_outputs_values).
    # With additional_outputs=[df_out, plot_out] we return 3 values total.
    # `state` is updated via in-place dict mutation (gr.State holds a reference).
    backend: AnalyticalBackend | None = state.get("backend")
    cache: dict = state.setdefault("cache", {})
    history_log: list = state.setdefault("history", [])

    if backend is None:
        return "_(backend not loaded — click **Load / Reload**)_", pl.DataFrame(), None

    if not message or not message.strip():
        return "(empty question)", pl.DataFrame(), None

    cache_key = (backend.id, message)
    if cache_key in cache:
        result = cache[cache_key]
        cached_flag = "  ↩︎ cached"
    else:
        result = backend.ask(message)
        cache[cache_key] = result
        cached_flag = ""

    summary = format_summary(result) + cached_flag

    # Update history log (text-only; gr.ChatInterface manages its own message list)
    history_log.append({"role": "user", "content": message})
    history_log.append({"role": "assistant", "content": summary, "result": result})
    cap = history_cap()
    if len(history_log) > cap:
        history_log = history_log[-cap:]
    state["history"] = history_log
    # gr.State sees the mutation via reference — no need to return state.

    # Side-panel content (always the latest result)
    df_out = result.df if (result.ok and result.df is not None) else pl.DataFrame()
    fig = None
    if result.ok and result.df is not None:
        choice = pick_chart(result.df, chart_kind)
        fig = _fig(result.df, choice)

    return summary, df_out, fig


def load_fn(selayer_path, api_key, base_url, model, state):
    try:
        backend = _make_backend(selayer_path, api_key, base_url, model)
    except Exception as e:
        state["backend"] = None
        state["history"] = []
        state["cache"] = {}
        return (
            f"❌ {type(e).__name__}: {e}",
            state,
            "_(backend not loaded)_",
            pl.DataFrame(),
            None,
        )
    state["backend"] = backend
    state["history"] = []
    state["cache"] = {}
    return (
        f"✅ Loaded **{backend.schema.layer_name}** — {len(backend.schema.tables)} tables",
        state,
        _schema_md(backend),
        pl.DataFrame(),
        None,
    )


def clear_fn(state):
    state["history"] = []
    state["cache"] = {}
    return state, pl.DataFrame(), None


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


with gr.Blocks(title="selayer_chat") as demo:
    state = gr.State(_initial_state())
    chart_kind_state = gr.State("auto")  # bound to chart_kind dropdown

    gr.Markdown(
        "# selayer_chat\nAsk questions about your data; the LLM writes DuckDB SQL."
    )

    with gr.Row():
        with gr.Column(scale=1):
            selayer_path = gr.Textbox(
                label="selayer YAML path", value="ecommerce_semantic_layer.yaml"
            )
            api_key = gr.Textbox(label="API key", type="password", value=os.environ.get("OPENAI_API_KEY", ""))
            base_url = gr.Textbox(label="base URL", value=os.environ.get("OPENAI_BASE_URL", "https://api.siemens.com/llm/v1"))
            model = gr.Textbox(label="Model", value=os.environ.get("OPENAI_MODEL", "qwen-3.6-27b"))
            chart_kind = gr.Dropdown(
                choices=chart_kind_options(),
                value="auto",
                label="Chart",
            )
            chart_kind.change(lambda v: v, inputs=chart_kind, outputs=chart_kind_state)
            load = gr.Button("Load / Reload", variant="primary")
            clear = gr.Button("🧹 Clear session")
            status = gr.Markdown()
            schema_md = gr.Markdown()
        with gr.Column(scale=3):
            df_out = gr.Dataframe(label="Result (latest)", interactive=False)
            plot_out = gr.Plot(label="Chart (latest)")
            chat = gr.ChatInterface(
                fn=chat_fn,
                additional_inputs=[state, chart_kind_state],
                additional_outputs=[df_out, plot_out],
                title="Conversation",
            )

    load.click(
        load_fn,
        [selayer_path, api_key, base_url, model, state],
        [status, state, schema_md, df_out, plot_out],
    )
    clear.click(clear_fn, [state], [state, df_out, plot_out])


if __name__ == "__main__":
    demo.launch()
