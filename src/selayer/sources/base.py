"""Immutable adapter lifecycle contracts.

This module defines the immutable value objects and the private
:class:`SourceAdapter` protocol that Task 4 adapters implement.  Every value
object is frozen and slotted.

Secrecy invariants (load-bearing):

* **No credentials, handles, or schemas in reprs.**  ``SourceHandle.resource``,
  ``schema``, and ``cleanup`` are excluded from the repr; free-form strings
  (filter ``value``, ``snapshot``, ``stable_name``, ``schema_fingerprint``)
  are *always* redacted to a fixed placeholder so a token-shaped secret
  (``TOKENONLYSECRET``), a SQL fragment, or a credential-bearing URI can never
  surface — no permissive token regex is trusted.  ``source_id`` and
  ``connector`` render only when they match the catalog source-name shape.
* **No raw SQL.**  :class:`SourceScanRequirement` carries ordered physical
  columns and structured :class:`SourceFilter` objects with a closed set of
  symbolic operators — never SQL text.  An operator outside the closed
  :data:`SourceFilterOperator` set is rejected at construction.
* **Cleanup callbacks are repr-hidden.**  ``SourceHandle.cleanup`` and
  ``QueryBinding.cleanup`` never appear in diagnostics.
* **Context cleanup.**  :class:`QueryBinding` is a context manager that
  invokes its cleanup callback exactly once (idempotent) on exit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol, Self, runtime_checkable

from selayer.sources.catalog import ParsedSource
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
# Repr sanitization
# ---------------------------------------------------------------------------
#
# Every string that appears in a lifecycle value-object repr is routed through
# these helpers so that credentials, arbitrary SQL, opaque handles, resources,
# schemas, and cleanup callbacks can never surface in diagnostics:
#
# * identifier fields (``source_id``, ``connector``) must match the catalog
#   source-name shape (lowercase snake_case) and are placeholder-ed otherwise;
# * physical columns must match the SQL-identifier shape and are placeholder-ed
#   otherwise (this is the SQL-injection surface since adapters interpolate
#   columns);
# * free-form fields (filter ``value``, ``snapshot``, ``stable_name``,
#   ``schema_fingerprint``) are *always* redacted to a fixed placeholder — no
#   permissive "safe token" regex can reliably tell a credential/SQL fragment
#   from a benign string (``TOKENONLYSECRET`` is token-shaped yet secret), so
#   strings are redacted by default and only non-string scalars (ints, bools,
#   floats, ``None``) pass through.
#
# Sanitization is repr-only: the stored values are unchanged so adapters keep
# using the real identifiers/snapshots/filter literals to drive scans.

# The exact shape the catalog enforces for declared source names.  Only a
# catalog-shaped name renders; anything else (uppercase secret tokens, SQL
# fragments, credential URIs) is placeholder-ed.
_SOURCE_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]*\Z")
# Physical SQL identifiers (columns) may legitimately be mixed case.
_SQL_IDENT_RE = re.compile(r"\A[a-zA-Z_][a-zA-Z0-9_]*\Z")


def _repr_source_name(value: object) -> str:
    """Render a source_id/connector, placeholder-ing non-conformant values."""

    if isinstance(value, str) and _SOURCE_NAME_RE.match(value):
        return value
    return "<redacted>"


def _repr_column(value: object) -> str:
    """Render a physical column, *conservatively* placeholder-ing values.

    Columns are validated against the full SQL-identifier shape (mixed case
    allowed) at construction, but the repr is stricter: only the catalog
    source-name shape (lowercase) renders.  A token-shaped secret column such
    as ``TOKENONLYSECRET`` (uppercase) is a syntactically valid identifier and
    therefore accepted at construction, yet it is redacted here so it can never
    surface in diagnostics.  Legitimate lowercase columns (``id``, ``amount``)
    render unchanged.
    """

    if isinstance(value, str) and _SOURCE_NAME_RE.match(value):
        return value
    return "<redacted>"


def _repr_literal(value: object) -> object:
    """Repr-safe projection of a free-form literal/handle field.

    Every string and byte string is redacted to a fixed ``"<redacted>"``
    placeholder: a free-form string may carry a secret token
    (``TOKENONLYSECRET``), a SQL fragment, or a credential-bearing URI, and no
    token-shaped regex can reliably separate a safe string from a secret.
    Mappings (``dict``) and unordered collections (``set``/``frozenset``) are
    redacted wholesale — their keys and members may each be a secret — while
    ordered collections (``tuple``/``list``) are projected element-wise so that
    bare numeric literals remain visible.  Non-string scalars (ints, bools,
    floats, enums, ``None``) pass through unchanged.
    """

    if isinstance(value, (str, bytes)):
        return "<redacted>"
    if isinstance(value, (dict, set, frozenset)):
        return "<redacted>"
    if isinstance(value, tuple):
        return tuple(_repr_literal(item) for item in value)
    if isinstance(value, list):
        return tuple(_repr_literal(item) for item in value)
    return value


def _render(name: str, fields: list[tuple[str, object]]) -> str:
    """Render a ``Name(field=value, ...)`` repr from pre-sanitized field values."""

    body = ", ".join(f"{field}={value!r}" for field, value in fields)
    return f"{name}({body})"


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


# The closed set of valid filter operators, mirrored as a runtime frozenset so
# that an out-of-set operator can be rejected at construction (the ``Literal``
# alias is not enforced at runtime).  This is the single guard against an
# arbitrary SQL string being stored as an operator and later rendered.
_FILTER_OPERATORS: frozenset[str] = frozenset(
    {"eq", "ne", "lt", "le", "gt", "ge", "in", "is_null", "is_not_null"}
)


@dataclass(frozen=True, slots=True)
class SourceFilter:
    """Structured source-local scan filter.

    ``operator`` is a closed symbolic token and ``value`` is a literal (or
    collection literal for ``in``).  No field may carry SQL text.  An operator
    outside the closed :data:`SourceFilterOperator` set is rejected at
    construction so no arbitrary SQL can ever be stored or rendered.
    """

    column: str
    operator: SourceFilterOperator
    value: object

    def __post_init__(self) -> None:
        # ``column`` must be a string SQL identifier: a non-string or a SQL
        # fragment (e.g. ``"id; DROP TABLE users--"``) is rejected here so no
        # arbitrary SQL can ever be stored as a column and later interpolated
        # by an adapter.  The check runs against an untyped local so the type
        # checker does not narrow it away as impossible.
        column: object = self.column
        if not (isinstance(column, str) and _SQL_IDENT_RE.match(column)):
            raise ValueError("invalid SourceFilter column")
        # The ``Literal`` type is not enforced at runtime, so validate the
        # operator against the closed set here: an out-of-set value (arbitrary
        # SQL such as ``"SELECT * FROM secrets"``) raises immediately.  The
        # check runs against an untyped local so the type checker does not
        # narrow it away as impossible.
        operator: object = self.operator
        if not (isinstance(operator, str) and operator in _FILTER_OPERATORS):
            raise ValueError("invalid SourceFilter operator")

    def __repr__(self) -> str:
        return _render(
            "SourceFilter",
            [
                ("column", _repr_column(self.column)),
                ("operator", self.operator),
                ("value", _repr_literal(self.value)),
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
        # Validate every column is a string SQL identifier before coercing: a
        # non-string or a SQL fragment (e.g. ``"id; DROP TABLE users--"``) is
        # rejected so no arbitrary SQL can ever be carried as a planned column.
        # The checks run against untyped locals so the type checker does not
        # narrow them away as impossible.
        for raw_column in self.columns:
            column: object = raw_column
            if not (isinstance(column, str) and _SQL_IDENT_RE.match(column)):
                raise ValueError("invalid SourceScanRequirement column")
        # Validate every filter is an actual SourceFilter: a raw string such as
        # ``"SELECT password FROM users"`` is rejected with a clean TypeError so
        # arbitrary SQL can never be stored as a planned filter.
        for raw_filter in self.filters:
            if not isinstance(raw_filter, SourceFilter):
                raise TypeError(
                    "SourceScanRequirement filters must be SourceFilter instances"
                )
        # Coerce any iterable (e.g. a mutable list) to an immutable tuple so
        # the frozen requirement cannot be mutated by later caller changes.
        if not isinstance(self.columns, tuple):
            object.__setattr__(self, "columns", tuple(self.columns))
        if not isinstance(self.filters, tuple):
            object.__setattr__(self, "filters", tuple(self.filters))

    def __repr__(self) -> str:
        return _render(
            "SourceScanRequirement",
            [
                ("columns", tuple(_repr_column(column) for column in self.columns)),
                ("filters", self.filters),
            ],
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
        return _render(
            "SourceHandle",
            [
                ("source_id", _repr_source_name(self.source_id)),
                ("connector", _repr_source_name(self.connector)),
                ("snapshot", _repr_literal(self.snapshot)),
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
        return _render(
            "SourceStatus",
            [
                ("source_id", _repr_source_name(self.source_id)),
                ("connector", _repr_source_name(self.connector)),
                ("generation", self.generation),
                ("schema_fingerprint", _repr_literal(self.schema_fingerprint)),
                ("snapshot", _repr_literal(self.snapshot)),
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
        return _render(
            "ReloadResult",
            [
                ("source_id", _repr_source_name(self.source_id)),
                ("old_generation", self.old_generation),
                ("new_generation", self.new_generation),
                ("schema_fingerprint", _repr_literal(self.schema_fingerprint)),
                ("snapshot", _repr_literal(self.snapshot)),
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
        return _render(
            "QueryBinding",
            [
                ("source_id", _repr_source_name(self.source_id)),
                ("stable_name", _repr_literal(self.stable_name)),
            ],
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
