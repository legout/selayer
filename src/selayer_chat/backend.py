"""The single seam between data work and UI work.

`AnalyticalBackend` is the only thing the UI adapters know about. It hides:

    * YAML loading and validation (delegated to `selayer`)
    * DuckDB in-memory view creation
    * System prompt construction
    * Agentic NL→SQL via PydanticAI (structured `SQLResult` + DuckDB schema
      tools); the low-level `LLMClient` is retained for the token-stream path
    * Safety validation (defense in depth, even on structured output)
    * Result packaging with timing and metadata

It exposes only three things:

    .schema           — Schema for the UI sidebar
    .ask(question)    — synchronous one-shot answer
    .stream(question) — iterator of intermediate events
    .close()          — release the DuckDB connection
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb
import polars as pl
from pydantic_ai import Agent

from . import agent as agent_mod
from . import engine as engine_mod
from . import prompts as prompts_mod
from .agent import AgentDeps, SQLResult
from .extraction import extract_sql
from .llm import LLMClient, OpenAIClient
from .safety import is_safe_sql
from .types import LLMConfig, QueryResult, Schema, StreamEvent


def _run_tokens(run: object) -> int | None:
    """Best-effort total-token count from a PydanticAI run result.

    The usage object's API shifts between PydanticAI versions, so we sum any
    int attribute whose name ends in ``tokens``. Returns None if nothing is
    found — ``tokens_used`` is informational, never load-bearing.
    """
    try:
        usage = getattr(run, "usage", None)
        if callable(usage):
            usage = usage()
        total, found = 0, False
        for name in dir(usage):
            if name.startswith("_") or not name.endswith("tokens"):
                continue
            val = getattr(usage, name, 0)
            if isinstance(val, int):
                total += val
                found = True
        return total if found and total else None
    except Exception:  # noqa: BLE001
        return None


class AnalyticalBackend:
    """The NL→SQL backend for selayer semantic layers.

    Construction loads one or more selayer YAML files, registers every data
    source as a DuckDB view, and builds the prompt schema. Subsequent
    `ask()` calls are cheap.
    """

    def __init__(
        self,
        selayer_paths: list[str | Path],
        llm: LLMConfig,
        *,
        client: LLMClient | None = None,
        agent: Agent[AgentDeps, SQLResult] | None = None,
        max_retries: int = 1,
    ) -> None:
        if not selayer_paths:
            raise ValueError("selayer_paths must contain at least one path")
        if not llm.api_key:
            raise ValueError("LLMConfig.api_key is required")

        self.id = str(uuid.uuid4())
        self._llm_cfg = llm
        self._client = client or OpenAIClient(llm)
        self._max_retries = max_retries

        # Lazy imports keep `selayer` optional at module load time.
        from selayer import DataSource as _DS  # noqa: F401
        from selayer import SemanticLayer

        self._conn = engine_mod.make_connection()
        self._table_names: list[str] = []

        layers: list[SemanticLayer] = []
        for p in selayer_paths:
            layer = SemanticLayer.load(str(p))
            layers.append(layer)
            self._register_layer(layer)

        if len(layers) == 1:
            self._schema = prompts_mod.schema_from_semantic_layer(layers[0])
        else:
            schemas = [
                prompts_mod.schema_from_semantic_layer(layer) for layer in layers
            ]
            self._schema = Schema(
                layer_name=", ".join(s.layer_name for s in schemas),
                layer_description=" / ".join(s.layer_description for s in schemas),
                fields=[f for s in schemas for f in s.fields],
                tables=[t for s in schemas for t in s.tables],
            )

        for t in self._schema.tables:
            if not t.columns and t.name in self._table_names:
                t.columns = engine_mod.list_columns(self._conn, t.name)

        # Agentic engine for `ask()`. Tests inject a model/agent; the default
        # builds an OpenAIChatModel over the configured endpoint. `client`
        # remains in use by the `stream()` token path below.
        self._agent = agent or agent_mod.build_agent(self._llm_cfg, self._schema)

    # -- public surface -------------------------------------------------------

    @property
    def schema(self) -> Schema:
        return self._schema

    def ask(self, question: str) -> QueryResult:
        """Synchronous one-shot: ask the agent, get an answer (or an error).

        Runs the PydanticAI agent (structured ``SQLResult`` + DuckDB schema
        tools) through its sync entry point, then safety-checks and executes
        the returned SQL. The agent self-validates with ``validate_sql`` before
        returning, so there is no separate self-fix retry here.
        """
        t0 = time.perf_counter()
        model = self._llm_cfg.model

        try:
            deps = AgentDeps(
                conn=self._conn,
                schema=self._schema,
                table_names=list(self._table_names),
            )
            # `/no_think` keeps qwen's reasoning off at the turn level, as a
            # belt-and-suspenders alongside the extra_body chat-template kwarg.
            run = self._agent.run_sync(question + agent_mod.NO_THINK, deps=deps)
            sql = (run.output.sql or "").strip()
            tokens = _run_tokens(run)
        except Exception as e:  # noqa: BLE001 - any agent failure -> QueryResult
            return QueryResult(
                question=question,
                sql="",
                error=f"agent failed: {type(e).__name__}: {e}",
                duration_ms=(time.perf_counter() - t0) * 1000,
                model=model,
            )

        attempted = [sql] if sql else []

        if not sql:
            return QueryResult(
                question=question,
                sql="",
                error="agent returned empty SQL",
                attempted_sql=attempted,
                duration_ms=(time.perf_counter() - t0) * 1000,
                model=model,
                tokens_used=tokens,
            )

        safe, reason = is_safe_sql(sql)
        if not safe:
            return QueryResult(
                question=question,
                sql=sql,
                error=f"generated SQL failed safety check: {reason}",
                attempted_sql=attempted,
                duration_ms=(time.perf_counter() - t0) * 1000,
                model=model,
                tokens_used=tokens,
            )

        df, err = self._execute_with_retry(sql)
        return QueryResult(
            question=question,
            sql=sql,
            df=df,
            error=err,
            attempted_sql=attempted,
            duration_ms=(time.perf_counter() - t0) * 1000,
            model=model,
            tokens_used=tokens,
        )

    def stream(self, question: str) -> Iterator[StreamEvent]:
        """Iterate: tokens → sql → rows → done, OR tokens → error → done.

        For UIs that want to show the SQL forming in real time.
        """
        system = prompts_mod.build_system_prompt(self._schema)

        try:
            for ev in self._client.stream(system, question):
                yield ev
                if ev.kind == "sql":
                    sql, err = extract_sql(str(ev.content))
                    if err:
                        yield StreamEvent(kind="error", error=err)
                        yield StreamEvent(kind="done")
                        return
                    safe, reason = is_safe_sql(sql)
                    if not safe:
                        yield StreamEvent(kind="error", error=f"SQL safety: {reason}")
                        yield StreamEvent(kind="done")
                        return
                    df, sql_err = self._execute_with_retry(sql)
                    if sql_err:
                        yield StreamEvent(kind="error", error=sql_err, sql=sql)
                        yield StreamEvent(kind="done")
                        return
                    yield StreamEvent(kind="rows", df=df, sql=sql)
                    yield StreamEvent(kind="done")
                    return
        except Exception as e:
            yield StreamEvent(
                kind="error", error=f"LLM call failed: {type(e).__name__}: {e}"
            )
            yield StreamEvent(kind="done")

    def close(self) -> None:
        """Release the DuckDB connection (UI frameworks usually never call this)."""
        with contextlib.suppress(Exception):
            self._conn.close()

    # -- context manager sugar -----------------------------------------------

    def __enter__(self) -> AnalyticalBackend:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- internals ------------------------------------------------------------

    def _register_layer(self, layer) -> None:
        for name, ds in layer.data_sources.items():
            try:
                engine_mod.register_data_source(self._conn, name, ds.path)
                self._table_names.append(name)
            except Exception as e:
                raise RuntimeError(
                    f"failed to register data source '{name}' from '{ds.path}': {e}"
                ) from e

    def _execute_with_retry(self, sql: str) -> tuple[pl.DataFrame | None, str | None]:
        try:
            df = engine_mod.execute_sql(self._conn, sql)
            return df, None
        except duckdb.Error as e:
            return None, f"{type(e).__name__}: {e}"
