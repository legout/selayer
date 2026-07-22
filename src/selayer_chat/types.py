"""Data carriers for selayer_chat.

Pure dataclasses, no business logic. These are the public types the UI
adapters consume; everything else is implementation detail.

LLMConfig resolution order for each field:
    1. The value passed at construction (e.g. LLMConfig(api_key="sk-..."))
    2. The corresponding env var (OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
       — OPENAI_MODEL_NAME is also accepted as a model alias)
    3. The hardcoded fallback (DEFAULT_BASE_URL / DEFAULT_MODEL)
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import polars as pl

# Environment-variable names consulted by LLMConfig.__post_init__.
ENV_API_KEY = "OPENAI_API_KEY"
ENV_BASE_URL = "OPENAI_BASE_URL"
ENV_MODEL = "OPENAI_MODEL"
ENV_MODEL_ALIAS = "OPENAI_MODEL_NAME"  # common alias in LiteLLM/vLLM/etc.

# Hard fallback defaults -- used only when neither explicit nor env is set.
DEFAULT_BASE_URL = "https://api.siemens.com/llm/v1"
DEFAULT_MODEL = "qwen-3.6-27b"


@dataclass
class LLMConfig:
    """Configuration for any OpenAI-compatible chat-completions endpoint.

    Empty-string defaults are filled in by __post_init__ from env, then
    from the hardcoded DEFAULT_* values, in that order.
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 1000
    timeout_s: float = 60.0
    # qwen-3.6-27b is a reasoning model: thinking is OFF by default. Leaving it
    # on empties the visible answer (see scripts/probe_endpoint.py). Flip to
    # True only if you want <think> traces.
    enable_thinking: bool = False

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = os.environ.get(ENV_API_KEY, "")
        if not self.base_url:
            self.base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)
        if not self.model:
            # OPENAI_MODEL is the primary var; OPENAI_MODEL_NAME is a common
            # alias used by LiteLLM/vLLM/other OpenAI-compatible tools.
            self.model = os.environ.get(ENV_MODEL) or os.environ.get(
                ENV_MODEL_ALIAS, DEFAULT_MODEL
            )


@dataclass
class FieldSpec:
    """A single field exposed to the LLM: a measure, dimension, or metric."""

    name: str
    description: str
    kind: Literal["measure", "dimension", "metric"] = "measure"
    data_type: str = ""
    sql_hint: str = ""
    hierarchies: list[str] = field(default_factory=list)


@dataclass
class TableSpec:
    """A raw data-source table, exposed so the LLM can write raw SQL too."""

    name: str
    description: str = ""
    columns: list[dict] = field(default_factory=list)


@dataclass
class Schema:
    """Information a UI needs to render a schema explorer panel."""

    layer_name: str
    layer_description: str
    fields: list[FieldSpec] = field(default_factory=list)
    tables: list[TableSpec] = field(default_factory=list)


@dataclass
class QueryResult:
    """Result of asking a question, including the SQL the LLM produced."""

    question: str
    sql: str
    df: pl.DataFrame | None = None
    error: str | None = None
    duration_ms: float = 0.0
    model: str = ""
    attempted_sql: list[str] = field(default_factory=list)
    tokens_used: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.df is not None

    @property
    def rows(self) -> int:
        return 0 if self.df is None else self.df.height


@dataclass
class StreamEvent:
    """One step of an answer being produced."""

    kind: Literal["token", "sql", "rows", "done", "error"]
    content: Any = None
    sql: str = ""
    df: pl.DataFrame | None = None
    error: str | None = None


Stream = Iterator[StreamEvent]
