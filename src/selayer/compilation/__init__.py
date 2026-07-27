"""SQL compilation for supported query engines."""

from selayer.compilation.duckdb import CompiledQuery, compile_duckdb

__all__ = ["CompiledQuery", "compile_duckdb"]
