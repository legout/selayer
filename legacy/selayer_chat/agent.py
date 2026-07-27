"""PydanticAI agent: NL → DuckDB SQL with structured output + schema tools.

This is the agentic engine behind `AnalyticalBackend.ask()`. It replaces the
old "free-text completion + regex extraction + one self-fix retry" flow with:

    * structured output via `SQLResult` (no more `extraction.py` regex for ask)
    * DuckDB-backed tools so the model can inspect the schema and self-check
      its SQL (`list_tables`, `describe_table`, `validate_sql`) before returning
    * PydanticAI's own output-validation retries

Design notes
------------
* The Siemens endpoint serves `qwen-3.6-27b`, a REASONING model. Thinking is
  disabled via `extra_body={"chat_template_kwargs": {"enable_thinking": ...}}`
  — the mechanism proven by `scripts/probe_endpoint.py` — NOT via PydanticAI's
  `thinking=False` (which sends the untested `reasoning_effort` knob).
* `safety.is_safe_sql()` still runs on the final SQL in the backend; structured
  output is not a substitute for defense in depth.
* The low-level `LLMClient` port + `OpenAIClient` are retained for the sync
  token-stream path (`AnalyticalBackend.stream()`), which still uses them.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from . import engine as engine_mod
from .prompts import build_system_prompt
from .types import LLMConfig, Schema

# qwen-native cue that suppresses <think>…</think>. Kept as a belt-and-
# suspenders alongside the `extra_body` chat-template kwarg; appended to the
# user turn by `AnalyticalBackend.ask()`.
NO_THINK = " /no_think"

# Extra instructions layered on top of the shared system prompt. Kept separate
# so the raw-SQL `stream()` path (which still wants "ONLY the SQL") is
# unaffected — the structured-output path overrides that anyway.
AGENT_ADDENDUM = (
    "You have tools. Call `describe_table(table_name)` to learn exact column "
    "names/types, and `validate_sql(sql)` (a dry-run EXPLAIN against DuckDB) "
    "to self-check your statement before returning it. Prefer validating when "
    "unsure. Return the final SQL only through the structured result."
)


class SQLResult(BaseModel):
    """Structured NL→SQL result. Replaces free-text + regex extraction."""

    sql: str
    explanation: str | None = None


@dataclass
class AgentDeps:
    """Runtime state injected into every tool via `RunContext[AgentDeps]`."""

    conn: duckdb.DuckDBPyConnection
    schema: Schema
    table_names: list[str]


# ---------------------------------------------------------------------------
# Tools — thin typed wrappers over engine.py (no business logic here)
# ---------------------------------------------------------------------------


def list_tables(ctx: RunContext[AgentDeps]) -> list[str]:
    """Names of all registered data tables/views available for querying."""
    return list(ctx.deps.table_names)


def describe_table(
    ctx: RunContext[AgentDeps], table_name: str
) -> list[dict[str, str]] | str:
    """Columns of a table as ``[{"name": ..., "type": ...}, ...]``.

    Call this to learn exact column names and DuckDB types before writing SQL.
    Returns an error string if the table does not exist (never raises).
    """
    try:
        return engine_mod.list_columns(ctx.deps.conn, table_name)
    except duckdb.Error as e:
        return f"{type(e).__name__}: {e}"


def _validate_sql(conn: duckdb.DuckDBPyConnection, sql: str) -> str:
    """Dry-run (EXPLAIN) a statement against DuckDB WITHOUT executing it."""
    try:
        conn.execute(f"EXPLAIN {sql}")
    except duckdb.Error as e:
        return f"{type(e).__name__}: {e}"
    return "OK"


def validate_sql(ctx: RunContext[AgentDeps], sql: str) -> str:
    """Dry-run (EXPLAIN) a SQL statement against DuckDB WITHOUT executing it.

    Returns ``"OK"`` if the statement parses and plans, otherwise the DuckDB
    error string. Use this to self-check before returning the final SQL.
    """
    return _validate_sql(ctx.deps.conn, sql)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_agent(
    cfg: LLMConfig,
    schema: Schema,
    *,
    model: Model | None = None,
) -> Agent[AgentDeps, SQLResult]:
    """Construct the NL→SQL agent.

    Parameters
    ----------
    cfg : LLMConfig
        Endpoint + model + token settings. ``cfg.enable_thinking`` controls the
        qwen reasoning toggle (default off — see module docstring).
    schema : Schema
        Semantic layer rendered into the system prompt.
    model : optional
        Pre-built model; defaults to an ``OpenAIChatModel`` over an
        ``OpenAIProvider(base_url, api_key)`` built from ``cfg``. Tests inject a
        ``TestModel``/``FunctionModel`` here to run fully offline.
    """
    if model is None:
        model = OpenAIChatModel(
            cfg.model,
            provider=OpenAIProvider(base_url=cfg.base_url, api_key=cfg.api_key),
        )

    settings = ModelSettings(
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        extra_body={"chat_template_kwargs": {"enable_thinking": cfg.enable_thinking}},
    )

    return Agent(
        model,
        deps_type=AgentDeps,
        output_type=SQLResult,
        system_prompt=[build_system_prompt(schema), AGENT_ADDENDUM],
        model_settings=settings,
        retries=2,
        tools=[list_tables, describe_table, validate_sql],
    )
