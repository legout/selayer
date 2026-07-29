"""Immutable adapter lifecycle contracts.

This module defines the immutable value objects and the private
:class:`SourceAdapter` protocol that Task 4 adapters implement.  Every value
object is frozen and slotted.

Secrecy invariants (load-bearing):

* **No credentials, handles, or schemas in reprs.**  ``SourceHandle.resource``,
  ``schema``, and ``cleanup`` are excluded from the repr; ``SourceStatus`` and
  ``ReloadResult`` carry only IDs, connector kind, generation, a stable
  fingerprint, a safe snapshot/version, and health — never the resource object
  or schema.  Snapshots are routed through the centralized URI-userinfo
  sanitizer so embedded credentials can never surface.
* **No raw SQL.**  :class:`SourceScanRequirement` carries ordered physical
  columns and structured :class:`SourceFilter` objects with a closed set of
  symbolic operators — never SQL text.
* **Cleanup callbacks are repr-hidden.**  ``SourceHandle.cleanup`` and
  ``QueryBinding.cleanup`` never appear in diagnostics.
* **Context cleanup.**  :class:`QueryBinding` is a context manager that
  invokes its cleanup callback exactly once (idempotent) on exit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol, Self, runtime_checkable

from selayer.sources.catalog import ParsedSource
from selayer.sources.config import _format_repr
from selayer.sources.profiles import (
    ArrowProviderResolver,
    RuntimeProfileResolver,
)
from selayer.sources.schema import TableSchema, schema_fingerprint

__all__ = [
    "QueryBinding",
    "ReloadResult",
    "SourceAdapter",
    "SourceFilter",
    "SourceFilterOperator",
    "SourceHandle",
    "SourceHealth",
    "SourceScanRequirement",
    "SourceStatus",
]


# ---------------------------------------------------------------------------
# Health state
# ---------------------------------------------------------------------------


class SourceHealth(StrEnum):
    """Coarse health state of a prepared source."""

    READY = "ready"
    STALE = "stale"
    UNHEALTHY = "unhealthy"


# ---------------------------------------------------------------------------
# Scan requirements (structured, no raw SQL)
# ---------------------------------------------------------------------------


# A closed set of symbolic filter operators.  Because the type is a Literal,
# no arbitrary SQL string can be stored in a SourceFilter — enforcing the
# "no raw SQL" invariant at the type level.
type SourceFilterOperator = Literal[
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "in",
    "is_null",
    "is_not_null",
]


@dataclass(frozen=True, slots=True)
class SourceFilter:
    """Structured source-local scan filter.

    ``operator`` is a closed symbolic token and ``value`` is a literal (or
    collection literal for ``in``).  No field may carry SQL text.
    """

    column: str
    operator: SourceFilterOperator
    value: object

    def __repr__(self) -> str:
        return _format_repr(
            "SourceFilter",
            [
                ("column", self.column),
                ("operator", self.operator),
                ("value", self.value),
            ],
        )


@dataclass(frozen=True, slots=True)
class SourceScanRequirement:
    """Ordered physical columns and structured source-local filters.

    Contains no raw SQL: filters are symbolic :class:`SourceFilter` objects.
    """

    columns: tuple[str, ...]
    filters: tuple[SourceFilter, ...] = ()

    def __post_init__(self) -> None:
        # Coerce any iterable (e.g. a mutable list) to an immutable tuple so
        # the frozen requirement cannot be mutated by later caller changes.
        if not isinstance(self.columns, tuple):
            object.__setattr__(self, "columns", tuple(self.columns))
        if not isinstance(self.filters, tuple):
            object.__setattr__(self, "filters", tuple(self.filters))

    def __repr__(self) -> str:
        return _format_repr(
            "SourceScanRequirement",
            [("columns", self.columns), ("filters", self.filters)],
        )


# ---------------------------------------------------------------------------
# Handles, status, and reload results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceHandle:
    """Immutable handle to a prepared source resource.

    ``resource``, ``schema``, and ``cleanup`` are excluded from the repr
    (``repr=False``) and additionally the repr renders only the safe
    identifier/snapshot fields, so no resource object, schema, or cleanup
    callback can surface in diagnostics.
    """

    source_id: str
    connector: str
    resource: object = field(repr=False)
    schema: TableSchema = field(repr=False)
    snapshot: str | None = None
    query_scoped: bool = False
    cleanup: Callable[[], None] | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return _format_repr(
            "SourceHandle",
            [
                ("source_id", self.source_id),
                ("connector", self.connector),
                ("snapshot", self.snapshot),
                ("query_scoped", self.query_scoped),
            ],
        )


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Immutable source health snapshot.

    Carries only IDs, connector kind, generation, a stable schema fingerprint,
    a safe snapshot/version, and health — never the resource object or schema.
    """

    source_id: str
    connector: str
    generation: int
    schema_fingerprint: str
    snapshot: str | None
    health: SourceHealth

    @classmethod
    def from_handle(
        cls,
        handle: SourceHandle,
        generation: int,
        *,
        health: SourceHealth = SourceHealth.READY,
    ) -> SourceStatus:
        return cls(
            source_id=handle.source_id,
            connector=handle.connector,
            generation=generation,
            schema_fingerprint=schema_fingerprint(handle.schema),
            snapshot=handle.snapshot,
            health=health,
        )

    def __repr__(self) -> str:
        return _format_repr(
            "SourceStatus",
            [
                ("source_id", self.source_id),
                ("connector", self.connector),
                ("generation", self.generation),
                ("schema_fingerprint", self.schema_fingerprint),
                ("snapshot", self.snapshot),
                ("health", self.health.value),
            ],
        )


@dataclass(frozen=True, slots=True)
class ReloadResult:
    """Immutable record of a completed (or attempted) source reload."""

    source_id: str
    old_generation: int
    new_generation: int
    schema_fingerprint: str
    snapshot: str | None

    def __repr__(self) -> str:
        return _format_repr(
            "ReloadResult",
            [
                ("source_id", self.source_id),
                ("old_generation", self.old_generation),
                ("new_generation", self.new_generation),
                ("schema_fingerprint", self.schema_fingerprint),
                ("snapshot", self.snapshot),
            ],
        )


# ---------------------------------------------------------------------------
# Query binding (context-managed cleanup)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueryBinding:
    """Immutable, context-managed registration cleanup record.

    Entering the binding returns ``self``; exiting invokes the cleanup callback
    exactly once (idempotent), deregistering the bound query.  The cleanup
    callback is excluded from the repr.
    """

    source_id: str
    stable_name: str
    cleanup: Callable[[], None] = field(repr=False)

    def __post_init__(self) -> None:
        # Wrap the caller's cleanup in an idempotent closure stored back into
        # the ``cleanup`` slot, so a context-manager exit (even if repeated)
        # runs the underlying deregistration at most once.
        target = self.cleanup
        done = [False]

        def _once() -> None:
            if not done[0]:
                done[0] = True
                target()

        object.__setattr__(self, "cleanup", _once)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()

    def __repr__(self) -> str:
        return _format_repr(
            "QueryBinding",
            [("source_id", self.source_id), ("stable_name", self.stable_name)],
        )


# ---------------------------------------------------------------------------
# Private adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SourceAdapter(Protocol):
    """Internal lifecycle contract every source adapter implements.

    This protocol is private to the sources package: it is the exact surface
    Task 4 adapters satisfy.  ``connection`` is intentionally typed ``object``
    so this module stays decoupled from any specific execution engine.
    """

    def prepare(
        self,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        """Open the source resource and return an opaque handle."""
        ...

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        """Return the currently observed schema for a prepared handle."""
        ...

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        """Register the source under a stable name on the execution connection."""
        ...

    def bind_query(
        self, handle: SourceHandle, requirement: SourceScanRequirement
    ) -> QueryBinding | None:
        """Bind a scan requirement to the source, returning a cleanup record.

        Returning ``None`` signals the adapter cannot push the requirement
        down and the caller must fall back to a full scan.
        """
        ...

    def close(self, handle: SourceHandle) -> None:
        """Release all resources held by the handle."""
        ...
