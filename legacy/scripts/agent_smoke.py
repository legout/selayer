#!/usr/bin/env python3
"""Offline end-to-end smoke for the PydanticAI agent path (NO network).

Proves the full wiring without hitting the Siemens endpoint:
    * `build_agent` constructs an `Agent`
    * the DuckDB ops the tools wrap (`list_columns`, `_validate_sql`) work
      against the real ecommerce views
    * `AnalyticalBackend.ask()` runs the agent, applies the safety check, and
      executes the returned SQL — via a `TestModel` that returns canned SQL

Run:  uv run scripts/agent_smoke.py
Exit: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys

from pydantic_ai.models.test import TestModel

from selayer_chat import AnalyticalBackend, LLMConfig
from selayer_chat import engine as engine_mod
from selayer_chat.agent import _validate_sql, build_agent

# The "model" returns this single statement; ask() must safety-check + execute it.
CANNED_SQL = "SELECT COUNT(*) AS n FROM orders"


def build_test_agent(cfg, schema):
    return build_agent(
        cfg,
        schema,
        model=TestModel(custom_output_args={"sql": CANNED_SQL}),
    )


def main() -> int:
    print("building backend over ecommerce layer (offline agent swap)…")
    backend = AnalyticalBackend(
        selayer_paths=["ecommerce_semantic_layer.yaml"],
        llm=LLMConfig(api_key="sk-test"),
    )
    # Swap the real (network) agent for an offline TestModel returning canned SQL.
    backend._agent = build_test_agent(backend._llm_cfg, backend.schema)

    # --- 1) schema introspection + validation against real DuckDB views ----
    print("\n# engine ops the tools wrap")
    print("  table_names      ->", backend._table_names)
    cols = engine_mod.list_columns(backend._conn, "orders")
    print("  orders columns   ->", [(c["name"], c["type"]) for c in cols[:4]], "…")
    print("  validate(good)   ->", _validate_sql(backend._conn, CANNED_SQL))
    print(
        "  validate(bad)    ->",
        _validate_sql(backend._conn, "SELECT * FROM no_such_table"),
    )

    # --- 2) ask() end-to-end (agent -> safety -> execute) ------------------
    print("\n# ask()")
    result = backend.ask("total orders")
    print("  ok     :", result.ok)
    print("  sql    :", result.sql)
    print("  rows   :", result.rows)
    print("  tokens :", result.tokens_used)
    print("  model  :", result.model)
    print("  error  :", result.error)
    if result.df is not None:
        print("  df     :", result.df.to_dicts()[:1])

    backend.close()

    if not result.ok or result.sql != CANNED_SQL:
        print("\n❌ smoke FAILED")
        return 1
    print("\n✅ smoke PASSED — agent path wired correctly (offline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
