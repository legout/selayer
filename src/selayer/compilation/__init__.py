"""Compilation of validated query plans to execution-engine SQL."""

from selayer.compilation.duckdb import (
    CompiledQuery,
    compile_duckdb,
    compile_metric_expression,
    compile_row_expression,
    quote_identifier,
)

__all__ = [
    "CompiledQuery",
    "compile_duckdb",
    "compile_metric_expression",
    "compile_row_expression",
    "quote_identifier",
]
