"""Panel UI for selayer_chat — uses pn.chat.ChatInterface + pn.state.cache.

Run with:
    panel serve apps/panel_app.py --show --allow-websocket-origin='*'

Architecture:
    * All state lives in pn.state.cache["selayer_state"] — survives the
      Panel serialization round-trip (stash on widgets.param does NOT).
    * pn.chat.ChatInterface owns the chat UI; the callback returns a
      pn.Column containing the markdown summary + SQL + Tabulator + Plotly.
    * Configuration widgets sit above the chat in a simple pn.Column layout
      (no template, no sidebar/main — keeps the document small and the
      JS minimal, which avoids template-related browser issues).
    * `.servable()` MUST be at module scope — `panel serve` imports the
      module and never executes the `if __name__ == "__main__":` guard.
"""

from __future__ import annotations

import os
from pathlib import Path

import panel as pn
import plotly.express as px
import polars as pl

from selayer_chat import AnalyticalBackend, LLMConfig, QueryResult

from apps._common import (
    chart_kind_options,
    format_summary,
    history_cap,
    pick_chart,
)

# Load both the plotly and tabulator extensions up-front. Tabulator is used
# inside the chat callback's pn.Column response (dataframes). Plotly is
# used for the chart pane. Both must be loaded BEFORE any widget that needs
# them is instantiated.
pn.extension("plotly", "tabulator")


# ---------------------------------------------------------------------------
# Per-session state via pn.state.cache
# ---------------------------------------------------------------------------


def _state() -> dict:
    cache = pn.state.cache
    if "selayer_state" not in cache:
        cache["selayer_state"] = {"backend": None, "history": [], "cache": {}}
    return cache["selayer_state"]


def _cached_ask(question: str) -> tuple[QueryResult | None, bool]:
    s = _state()
    backend = s.get("backend")
    if backend is None:
        return None, False
    key = (backend.id, question)
    if key in s["cache"]:
        return s["cache"][key], True
    result = backend.ask(question)
    s["cache"][key] = result
    return result, False


# ---------------------------------------------------------------------------
# Configuration widgets
# ---------------------------------------------------------------------------

selayer_path = pn.widgets.TextInput(
    name="selayer YAML path", value="ecommerce_semantic_layer.yaml"
)
api_key = pn.widgets.PasswordInput(name="API key", value=os.environ.get("OPENAI_API_KEY", ""))
base_url = pn.widgets.TextInput(name="base URL", value=os.environ.get("OPENAI_BASE_URL", "https://api.siemens.com/llm/v1"))
model = pn.widgets.TextInput(name="Model", value=os.environ.get("OPENAI_MODEL", "qwen-3.6-27b"))
chart_kind = pn.widgets.Select(name="Chart", options=chart_kind_options(), value="auto")
load_btn = pn.widgets.Button(name="Load / Reload", button_type="primary")
clear_btn = pn.widgets.Button(name="🧹 Clear session")
status = pn.pane.Markdown("")


def _do_load(_=None):
    if not Path(selayer_path.value).exists():
        status.object = f"❌ selayer file not found: {selayer_path.value}"
        return
    if not api_key.value:
        status.object = "❌ API key required"
        return
    try:
        backend = AnalyticalBackend(
            selayer_paths=[selayer_path.value],
            llm=LLMConfig(
                api_key=api_key.value, base_url=base_url.value, model=model.value
            ),
        )
    except Exception as e:
        status.object = f"❌ {type(e).__name__}: {e}"
        return
    s = _state()
    s["backend"] = backend
    s["history"] = []
    s["cache"] = {}
    status.object = (
        f"✅ Loaded **{backend.schema.layer_name}** — "
        f"{len(backend.schema.tables)} tables, "
        f"{sum(1 for f in backend.schema.fields if f.kind == 'measure')} measures, "
        f"{sum(1 for f in backend.schema.fields if f.kind == 'dimension')} dimensions"
    )


def _do_clear(_=None):
    s = _state()
    s["history"] = []
    s["cache"] = {}
    status.object = "_(session cleared — type to start fresh)_"


load_btn.on_click(_do_load)
clear_btn.on_click(_do_clear)


# ---------------------------------------------------------------------------
# Chat callback — returns a single Panel layout that ChatInterface renders
# as one assistant bubble.
# ---------------------------------------------------------------------------


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


def _build_response(question: str):
    result, was_cached = _cached_ask(question)
    s = _state()

    if result is None:
        return pn.pane.Markdown(
            "_(backend not loaded — fill the fields above and click **Load / Reload**)_"
        )

    if not result.ok:
        return pn.pane.Markdown(f"❌ {result.error}")

    summary = format_summary(result) + ("  ↩︎ cached" if was_cached else "")
    blocks: list = [pn.pane.Markdown(f"**🤖** {summary}")]
    if result.sql:
        blocks.append(
            pn.pane.Markdown(
                f"```sql\n{result.sql}\n```\n_model: {result.model} · "
                f"{result.duration_ms:.0f} ms · {result.rows} rows · "
                f"tokens: {result.tokens_used or '?'}_"
            )
        )
    df = result.df
    if df is not None:
        blocks.append(
            pn.widgets.Tabulator(
                df.to_pandas(),
                disabled=True,
                height=300,
                show_index=False,
                sizing_mode="stretch_width",
            )
        )
        choice = pick_chart(df, chart_kind.value)
        fig = _fig(df, choice)
        if fig is not None:
            blocks.append(pn.pane.Plotly(fig, sizing_mode="stretch_width"))

    # Update history (cap at history_cap())
    history = s["history"]
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": summary, "result": result})
    cap = history_cap()
    if len(history) > cap:
        history = history[-cap:]
    s["history"] = history

    return pn.Column(*blocks, sizing_mode="stretch_width")


# ---------------------------------------------------------------------------
# Layout — plain pn.Column, no template
# ---------------------------------------------------------------------------

chat = pn.chat.ChatInterface(
    callback=_build_response,
    widgets=pn.widgets.TextInput(
        placeholder="Ask a question about your data…", sizing_mode="stretch_width"
    ),
    user="you",
    callback_user="selayer",
    show_send=True,
    show_rerun=False,
    show_clear=False,
    sizing_mode="stretch_width",
)

root = pn.Column(
    "# selayer_chat",
    "Ask questions about your data; the LLM writes DuckDB SQL.",
    pn.Row(selayer_path, api_key, sizing_mode="stretch_width"),
    pn.Row(base_url, model, sizing_mode="stretch_width"),
    pn.Row(chart_kind, load_btn, clear_btn, sizing_mode="stretch_width"),
    status,
    chat,
    sizing_mode="stretch_width",
)

# NB: .servable() MUST run at import time — `panel serve` imports this
# module and never executes the `__main__` guard.
root.servable()

if __name__ == "__main__":
    import bokeh.command.bootstrap as bk_boot

    bk_boot.main(["bokeh", "serve", "--show", __file__])
