"""Marimo notebook for selayer_chat — Tier 1: session memory + result cache + replayable history.

Run with:
    marimo edit apps/marimo_app.py
    # or as a static webapp:
    marimo run apps/marimo_app.py
"""

from __future__ import annotations


import marimo

__generated_with = "0.23.14"
app = marimo.App(width="medium")


# ---------------------------------------------------------------------------
# Notes on the BACKEND holder
#
# `mo.ui.chat(model=…)` creates the widget ONCE; the callback is invoked
# per user message. If the model callback depended on `mo.state`, the chat
# widget would be torn down + recreated on every state change — losing
# the conversation. So the backend reference is *not* in `mo.state`.
#
# Instead it's defined in a dedicated cell (`BACKEND_HOLDER_CELL`) and
# flows through the reactive graph as a regular return-tuple binding.
# In-place mutation (`BACKEND["instance"] = new_backend`) does NOT change
# the dict's identity, so marimo's param-equality check doesn't fire —
# the chat cell stays alive across loads. The closure inside the chat's
# `model_fn` captures `BACKEND` and reads `.instance` at call-time.
#
# The reactive UI bits (status text, "loaded" flag) flow through
# `mo.state` so the sidebar updates on click.
# ---------------------------------------------------------------------------


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _():
    import os
    from pathlib import Path

    import plotly.express as px
    import polars as pl

    from selayer_chat import AnalyticalBackend, LLMConfig

    from apps._common import chart_kind_options, format_summary, pick_chart

    return (
        AnalyticalBackend,
        LLMConfig,
        Path,
        chart_kind_options,
        format_summary,
        mo,
        pick_chart,
        pl,
        px,
    )


@app.cell
def _():
    # Module-level-style holder, defined inside a cell so marimo's
    # runtime exposes it as a regular reactive binding. Downstream cells
    # take `BACKEND` as a parameter; in-place mutation
    # (`BACKEND["instance"] = ...`) preserves dict identity, so marimo's
    # param-equality check does NOT re-run cells — which is exactly what
    # we need for the long-lived chat widget.
    BACKEND = {"instance": None}
    return (BACKEND,)


@app.cell
def _(mo):
    # Reactive state drives only the sidebar UI bits (status text, the
    # "loaded" flag). The actual backend reference lives in `BACKEND`.
    state, set_state = mo.state(
        {
            "loaded": False,
            "schema_name": "",
            "status": "_(click Load / Reload)_",
        }
    )
    return set_state, state


@app.cell
def _(chart_kind_options, mo):
    selayer_path = mo.ui.text(
        value="ecommerce_semantic_layer.yaml", label="selayer YAML path"
    )
    api_key = mo.ui.text(kind="password", label="API key", value=os.environ.get("OPENAI_API_KEY", ""))
    base_url = mo.ui.text(value=os.environ.get("OPENAI_BASE_URL", "https://api.siemens.com/llm/v1"), label="base URL")
    model = mo.ui.text(value=os.environ.get("OPENAI_MODEL", "qwen-3.6-27b"), label="Model")
    chart_kind = mo.ui.dropdown(
        options=chart_kind_options(), value="auto", label="Chart"
    )
    return api_key, base_url, chart_kind, model, selayer_path


@app.cell
def _(
    AnalyticalBackend,
    LLMConfig,
    Path,
    BACKEND,
    api_key,
    base_url,
    mo,
    model,
    selayer_path,
    set_state,
):
    # Real load button with `on_change` handler — wired here so it can
    # mutate `BACKEND` directly (via closure) and also push a status
    # string into reactive `state` so the sidebar updates.
    def _on_load(_=None):
        try:
            if not Path(selayer_path.value).exists():
                set_state(
                    lambda s: {
                        **s,
                        "status": (f"❌ selayer file not found: {selayer_path.value}"),
                    }
                )
                return
            if not api_key.value:
                set_state(lambda s: {**s, "status": "❌ API key required"})
                return
            new_backend = AnalyticalBackend(
                selayer_paths=[selayer_path.value],
                llm=LLMConfig(
                    api_key=api_key.value,
                    base_url=base_url.value,
                    model=model.value,
                ),
            )
            BACKEND["instance"] = new_backend
            set_state(
                lambda s: {
                    **s,
                    "loaded": True,
                    "schema_name": new_backend.schema.layer_name,
                    "status": (
                        f"✅ Loaded **{new_backend.schema.layer_name}** — "
                        f"{len(new_backend.schema.tables)} tables"
                    ),
                }
            )
        except Exception as exc:
            err_msg = f"❌ {type(exc).__name__}: {exc}"
            set_state(lambda s: {**s, "loaded": False, "status": err_msg})

    # Replace the placeholder with the real button once we have a working handler.
    # (Marimo requires the button to be the cell's last expression; we
    # discard the placeholder so only the real one ships.)
    load_btn = mo.ui.button(
        label="Load / Reload",
        kind="success",
        on_change=_on_load,
    )
    return (load_btn,)


@app.cell
def _(state):
    _cur = state()
    status_md = _cur["status"]
    return (status_md,)


@app.cell
def _(BACKEND, mo, state):
    """Schema explorer — depends on state['loaded'] for reactivity, but
    reads the actual schema from `BACKEND['instance']`."""
    _cur = state()
    if not _cur["loaded"] or BACKEND["instance"] is None:
        schema_view = mo.md("_(load a backend to see the schema)_")
    else:
        s = BACKEND["instance"].schema
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
        schema_view = mo.md("\n".join(lines))
    return (schema_view,)


@app.cell
def _(BACKEND, chart_kind, mo, pick_chart, pl, px):
    """Chat model callback — invoked once per user message.

    PURE FUNCTION over the module-level `BACKEND` holder; takes NO
    Marimo `state` ref so the chat widget stays stable across status
    changes. Returns either a string (text-only reply) or a marimo
    element (`mo.vstack` of summary + SQL block + table + chart).
    """

    def model_fn(messages, config):
        backend = BACKEND["instance"]
        if backend is None:
            return mo.md(
                "⚠️ _No backend loaded. Click **Load / Reload** in "
                "the sidebar to wire up an LLM._"
            )
        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        if last_user is None:
            return ""
        question = last_user.content
        if not str(question).strip():
            return mo.md("_(empty question)_")

        result = backend.ask(str(question))
        if not getattr(result, "ok", False):
            return mo.md(f"❌ **{result.error or 'unknown error'}**")

        blocks = []
        blocks.append(mo.md(f"**{format_summary(result)}**"))
        if result.sql:
            blocks.append(
                mo.md(
                    f"```sql\n{result.sql}\n```\n"
                    f"_model: {result.model} · {result.duration_ms:.0f} ms · "
                    f"{result.rows} rows · tokens: {result.tokens_used or '?'}_"
                )
            )
        df = getattr(result, "df", None)
        if df is not None and df.height > 0:
            blocks.append(mo.ui.table(df.to_pandas()))
            choice = pick_chart(df, override=chart_kind.value)
            if choice.kind == "bar" and choice.y:
                blocks.append(px.bar(df, x=choice.x, y=choice.y[0]))
            elif choice.kind == "line" and choice.y:
                blocks.append(px.line(df, x=choice.x, y=choice.y))
            elif choice.kind == "scatter" and choice.y:
                blocks.append(px.scatter(df, x=choice.x, y=choice.y[0]))
            elif choice.kind == "area" and choice.y:
                blocks.append(px.area(df, x=choice.x, y=choice.y))
        return mo.vstack(blocks) if blocks else mo.md("_(no output)_")

    return (model_fn,)


@app.cell
def _(mo, model_fn):
    # Chat widget — created ONCE. Pre-baked prompts give one-click starters
    # matching the few-shot examples in `selayer_chat.prompts.FEW_SHOT`.
    # Cell has no `state` dependency, so the widget stays alive across
    # status / schema updates.
    chat = mo.ui.chat(
        model_fn,
        prompts=[
            "Revenue and order count by country × quarter matrix",
            "Top 10 products by units sold",
            "Order completion rate last quarter",
        ],
        show_configuration_controls=False,
    )
    return (chat,)


@app.cell
def _(
    api_key,
    base_url,
    chart_kind,
    load_btn,
    mo,
    model,
    schema_view,
    selayer_path,
    status_md,
):
    # Sidebar contents (must be the last expression in the cell).
    mo.sidebar(
        [
            selayer_path,
            api_key,
            base_url,
            model,
            chart_kind,
            mo.hstack([load_btn]),
            mo.md("---"),
            mo.md("**Status**"),
            status_md,
            mo.md("---"),
            mo.md("**Schema**"),
            schema_view,
        ]
    )
    return


@app.cell
def _(chat, mo):
    # Main pane: just the chat widget.
    mo.vstack(
        [
            mo.md(
                "# selayer_chat  \n_Ask questions about your data; "
                "the LLM writes DuckDB SQL._"
            ),
            chat,
        ]
    )
    return


if __name__ == "__main__":
    app.run()
