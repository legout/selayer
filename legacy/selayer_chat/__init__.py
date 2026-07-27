"""selayer_chat — A standalone NL→SQL backend for `selayer` semantic layers.

Public API:
    AnalyticalBackend       — the only class you need
    LLMConfig, QueryResult, Schema, StreamEvent  — data carriers

Quick start:
    >>> from selayer_chat import AnalyticalBackend, LLMConfig
    >>> backend = AnalyticalBackend(
    ...     selayer_paths=["data/ecom.yaml"],
    ...     llm=LLMConfig(api_key="sk-...", model="qwen-3.6-27b"),
    ... )
    >>> result = backend.ask("Total revenue last quarter")
    >>> result.sql
    'SELECT ...'
    >>> result.df
    shape: (1, 1)
"""

# Auto-load .env on import (if python-dotenv is available).
# Existing os.environ values are NOT overridden, so shell vars win.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)
except ImportError:
    pass


def load_env() -> bool:
    """Public re-export of the .env loader. Idempotent. Returns True
    if python-dotenv is installed (and .env was loaded), False otherwise.
    Never overrides existing os.environ values.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)
        return True
    except ImportError:
        return False
from .types import (
    LLMConfig,
    QueryResult,
    Schema,
    StreamEvent,
)
from .backend import AnalyticalBackend

__all__ = [
    "AnalyticalBackend",
    "LLMConfig",
    "QueryResult",
    "Schema",
    "StreamEvent",
    "load_env",
]
