"""Physical source-grain audit adapter for the verification report model.

:func:`verify_physical` builds a private in-memory DuckDB connection and a
registry-backed :class:`~selayer.sources.registry.SourceRegistry` from the
layer, then runs an exact full-scan grain check over every source.  For each
source it counts rows, distinct grain tuples, null grain rows, and duplicate
grain groups — without ever reading or echoing the offending values.  Only the
aggregate counts and registry-derived metadata (connector kind, generation,
safe snapshot, schema fingerprint) reach the report.

Connector metadata is read exclusively through
:meth:`SourceRegistry.status` *inside* the per-source binding context (the
registry lock is held) so generation/snapshot/schema evidence reflects the
same bound scan; a concurrent reload cannot swap the handle between the status
read and the grain scan.  Sanitized
:class:`~selayer.sources.errors.SourceError` values raised during registry
creation, per-source binding, or the bound scan are adapted to ``unavailable``
outcomes, and a raw connector/scan failure is normalized to a sanitized
``scan_failed`` error so no driver-derived location or credential text can
ever escape.  Truly-internal programmer errors (a malformed grain column)
propagate unchanged, and any unavailable source makes the report incomplete.

After the source-grain pass, every declared relationship is audited for
cardinality with the same aggregate-only, secret-safe contract: each distinct
relationship source is bound and materialized once under the registry lock
(both sides are then derived from those re-readable temp tables, so a
single-pass query-scoped source — including a self-relationship's single
source — is read exactly once), then a cardinality-specific scan counts null
keys, duplicate groups, referential orphans, and child multiplicity without
ever selecting a key value.  Each relationship yields one
``relationship.<name>.cardinality`` outcome; an unavailable bound source
makes that outcome ``unavailable`` and the report incomplete.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import duckdb

from selayer.model import Relationship, SemanticLayer
from selayer.sources.base import SourceScanRequirement, SourceStatus
from selayer.sources.errors import SourceConnectionError, SourceError
from selayer.sources.profiles import (
    ArrowProviderResolver,
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfileResolver,
)
from selayer.sources.registry import SourceRegistry
from selayer.verification.model import (
    PhysicalCheck,
    Severity,
    VerificationDiagnostic,
    VerificationOutcome,
    VerificationReport,
)

__all__ = ["verify_physical"]

#: Evidence ``path`` shared by every grain outcome.  Outcomes are ordered
#: deterministically by ``(path, check_id)``; sharing one path keeps the
#: source order driven by the sorted ``check_id`` suffix.
_PATH = "physical"

#: Stable code for the single report-level diagnostic emitted when any source
#: is unavailable, marking the report incomplete.
_UNAVAILABLE_CODE = "source.audit.unavailable"

#: Locally scoped alias for the supported evidence leaf types (mirrors the
#: runtime contract enforced by ``VerificationOutcome``).
type EvidenceValue = bool | int | float | str | None

#: A SQL-identifier shape.  Grain columns and source IDs are quoted through a
#: single helper that re-validates this shape so a programmatic layer carrying
#: a SQL fragment can never be interpolated into the audit SQL.
_SQL_IDENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _quote_identifier(name: str) -> str:
    """Return a per-segment double-quoted SQL identifier.

    The identifier is re-validated against the SQL-identifier shape and any
    embedded double quote is doubled.  A non-identifier (a SQL fragment, a
    non-string, a ``str`` subclass) raises ``ValueError`` — this is an
    unexpected programmer error (a malformed layer declaration) and is
    deliberately not adapted to an outcome.
    """

    if type(name) is not str or not _SQL_IDENT_RE.match(name):
        raise ValueError("invalid identifier for grain audit")
    return '"' + name.replace('"', '""') + '"'


def _grain_sql(source_id: str, grain: tuple[str, ...]) -> str:
    """Build the exact full-scan grain-count SQL for one source.

    Counts row count, distinct grain tuples, duplicate row count
    (``row_count - distinct_grain_count``), null grain rows, and duplicate
    grain groups — the full aggregate specified by the audit contract.
    ``struct_pack`` field aliases are generated positionally
    (``g1``, ``g2``, …) — never from source values — so a grain column whose
    name carries a secret can never surface as a field alias.

    The distinct tuple is always built with ``struct_pack``, for single- and
    multi-column grains alike, so the distinct count is null-safe and the two
    cases stay consistent.  ``count(distinct "col")`` drops SQL NULL, which
    would undercount distinct grains and overcount duplicate rows for a
    nullable single-column grain; ``struct_pack(g1 := "col")`` yields a
    non-NULL struct ``{'g1': NULL}`` when the column is NULL, so NULL counts
    as exactly one distinct grain tuple (matching the composite path, where a
    fully-NULL grain is one struct value) and ``duplicate_row_count`` is exact.
    The grouped duplicate-subquery uses a plain ``group by`` over the grain
    columns, whose standard-SQL semantics already group all NULLs together, so
    a repeated NULL grain is one duplicate group.
    """

    quoted_source = _quote_identifier(source_id)
    quoted_grain = [_quote_identifier(column) for column in grain]
    null_filter = " or ".join(f"{column} is null" for column in quoted_grain)
    pack = ", ".join(
        f"g{index} := {column}"
        for index, column in enumerate(quoted_grain, start=1)
    )
    distinct_expr = f"count(distinct struct_pack({pack}))"
    group_columns = ", ".join(quoted_grain)
    duplicate_groups = (
        "select count(*) from ("
        f"select {group_columns} from {quoted_source} "
        f"group by {group_columns} having count(*) > 1"
        ") duplicates"
    )
    # ``_grain_sql`` and the surrounding materialization interpolate only
    # identifiers that were re-validated and double-quoted through
    # ``_quote_identifier``; no source value, parameter, or fragment reaches
    # any statement, so these are validated-identifier sinks rather than
    # dynamic-SQL sinks (the same contract as the database adapter's
    # ATTACH/CREATE VIEW statements).
    return (
        "select "
        "count(*) as row_count, "
        f"{distinct_expr} as distinct_grain_count, "
        f"count(*) - {distinct_expr} as duplicate_row_count, "
        f"count(*) filter (where {null_filter}) as null_grain_rows, "
        f"({duplicate_groups}) as duplicate_grain_groups "
        f"from {quoted_source}"
    )


def _grain_counts(
    registry: SourceRegistry, source_id: str, grain: tuple[str, ...]
) -> tuple[int, int, int, int, int]:
    """Run the grain-count SQL under the registry lock.

    Returns ``(row_count, distinct_grain_count, duplicate_row_count,
    null_grain_rows, duplicate_grain_groups)``.  Called inside
    :meth:`bind_requirements` so the lock spans the whole scan and any
    query-scoped reader is bound.

    The bound source is materialized into a private temp table holding only
    the grain columns *once*, then the multi-scan grain SQL runs against that
    table.  This keeps the scan exact (every grain row is counted) while
    making it robust for single-pass query-scoped readers (PyIceberg,
    programmatic Arrow readers) that cannot be re-scanned within one query:
    the reader is pulled exactly once to populate the temp table, and the
    duplicate-group subquery re-scans the re-readable temp table instead.
    """

    quoted_source = _quote_identifier(source_id)
    quoted_grain = [_quote_identifier(column) for column in grain]
    select_columns = ", ".join(quoted_grain)
    # A leading-underscore temp-table name can never collide with a registered
    # source name (catalog names must start with a lowercase letter), and the
    # table is dropped after every source so only one ever exists at a time.
    audit_name = "__selayer_grain_audit"
    quoted_audit = _quote_identifier(audit_name)
    registry.execute(
        f"create or replace temp table {quoted_audit} as "
        f"select {select_columns} from {quoted_source}"
    )
    try:
        sql = _grain_sql(audit_name, grain)
        row = registry.execute(sql).fetchone()
    finally:
        registry.execute(f"drop table if exists {quoted_audit}")
    return (
        int(row[0]),
        int(row[1]),
        int(row[2]),
        int(row[3]),
        int(row[4]),
    )


def _status_evidence(status: SourceStatus | None) -> dict[str, EvidenceValue]:
    """Connector metadata drawn only from registry status (never a handle)."""

    evidence: dict[str, EvidenceValue] = {}
    if status is not None:
        evidence["connector"] = status.connector
        evidence["generation"] = status.generation
        evidence["snapshot"] = status.snapshot
        evidence["schema_fingerprint"] = status.schema_fingerprint
    return evidence


def _grain_outcome(
    source_id: str,
    status: SourceStatus | None,
    counts: tuple[int, int, int, int, int],
) -> VerificationOutcome:
    """A passed/failed full-scan grain outcome carrying counts and metadata."""

    (
        row_count,
        distinct_grain_count,
        duplicate_row_count,
        null_grain_rows,
        duplicate_grain_groups,
    ) = counts
    passed = null_grain_rows == 0 and duplicate_grain_groups == 0
    evidence = _status_evidence(status)
    evidence.update(
        {
            "row_count": row_count,
            "distinct_grain_count": distinct_grain_count,
            "duplicate_row_count": duplicate_row_count,
            "null_grain_rows": null_grain_rows,
            "duplicate_grain_groups": duplicate_grain_groups,
        }
    )
    return VerificationOutcome(
        check_id=f"source.{source_id}.grain",
        status="passed" if passed else "failed",
        scope="full_scan",
        path=_PATH,
        evidence=evidence,
        diagnostics=(),
    )


def _unavailable_outcome(
    source_id: str,
    error: SourceError,
    status: SourceStatus | None,
) -> VerificationOutcome:
    """An ``unavailable`` outcome carrying only the sanitized error code."""

    evidence = _status_evidence(status)
    evidence["error_code"] = error.code
    return VerificationOutcome(
        check_id=f"source.{source_id}.grain",
        status="unavailable",
        scope="full_scan",
        path=_PATH,
        evidence=evidence,
        diagnostics=(),
    )


def _validate_grain_identifiers(source_id: str, grain: tuple[str, ...]) -> None:
    """Validate the source ID and grain columns before any scan.

    A malformed identifier (a SQL fragment smuggled into a grain column or
    source ID) raises ``ValueError`` here — a clean, truly-internal programmer
    error that propagates rather than being sanitized into an availability
    outcome.  Running this validation *before* the binding/scan try-block keeps
    the scan-time error handling free to catch every connector/scan failure
    (a raw driver error) without also masking a layer-declaration bug.
    """

    _quote_identifier(source_id)
    for column in grain:
        _quote_identifier(column)


# ---------------------------------------------------------------------------
# Relationship cardinality audit
# ---------------------------------------------------------------------------

#: Fixed, leading-underscore temp-table names that materialize each
#: relationship side once so single-pass query-scoped readers are pulled
#: exactly once and the join re-scans the re-readable temp tables.  A leading
#: underscore can never collide with a registered source name, and at most one
#: relationship is audited at a time (the temp tables are dropped in a
#: ``finally`` after every relationship).
_REL_ONE = "__selayer_rel_one"
_REL_MANY = "__selayer_rel_many"
_REL_LEFT = "__selayer_rel_left"
_REL_RIGHT = "__selayer_rel_right"

#: Per-distinct-source temp-table names.  Each distinct relationship source is
#: pulled exactly once (all columns its sides need) into one of these tables;
#: the per-side single-column tables are then derived from them, never from the
#: bound (single-pass) relation directly.  A relationship has at most two
#: distinct sources, so two names suffice; like the side tables they are
#: dropped in a ``finally`` after every relationship.
_REL_SOURCES = ("__selayer_rel_source0", "__selayer_rel_source1")

#: Positional alias for the single join column in every materialized temp
#: table — generated, never derived from a source value, so a column whose
#: name carries a secret can never surface as an alias.
_REL_KEY = "k"

#: Constant diagnostic messages (never key values) for relationship outcomes.
_REL_ONE_SIDE_NULL_MSG = "the one side of the relationship has null keys"
_REL_ONE_SIDE_DUP_MSG = "the one side of the relationship has duplicate keys"
_REL_MANY_SIDE_ORPHAN_MSG = (
    "the many side has non-null keys with no one-side match"
)
_REL_SOURCE_NULL_MSG = "the source side of the relationship has null keys"
_REL_SOURCE_DUP_MSG = "the source side of the relationship has duplicate keys"
_REL_TARGET_NULL_MSG = "the target side of the relationship has null keys"
_REL_TARGET_DUP_MSG = "the target side of the relationship has duplicate keys"
_REL_SOURCE_UNMATCHED_MSG = (
    "the source side has non-null keys with no target match"
)
_REL_TARGET_UNMATCHED_MSG = (
    "the target side has non-null keys with no source match"
)
_REL_NO_SAFE_TRAVERSAL_MSG = (
    "a many-to-many relationship does not claim uniqueness or safe traversal"
)


@dataclass(frozen=True, slots=True)
class _RelationshipSides:
    one_source: str | None
    one_column: str | None
    many_source: str | None
    many_column: str | None


def _relationship_sides(relationship: Relationship) -> _RelationshipSides:
    """Normalize a directed relationship into its one and many sides.

    For ``one_to_many`` the declared ``source`` is the one side; for
    ``many_to_one`` the declared ``target`` is the one side.  One-to-one and
    many-to-many return all-``None`` sides because their bidirectional
    semantics are handled by dedicated query helpers.
    """

    if relationship.type == "one_to_many":
        return _RelationshipSides(
            relationship.source,
            relationship.source_column,
            relationship.target,
            relationship.target_column,
        )
    if relationship.type == "many_to_one":
        return _RelationshipSides(
            relationship.target,
            relationship.target_column,
            relationship.source,
            relationship.source_column,
        )
    return _RelationshipSides(None, None, None, None)


def _validate_relationship_identifiers(relationship: Relationship) -> None:
    """Validate every relationship endpoint identifier before any scan.

    Mirrors :func:`_validate_grain_identifiers`: a malformed identifier (a SQL
    fragment smuggled into an endpoint) raises ``ValueError`` — a clean,
    truly-internal programmer error that propagates rather than being
    sanitized into an availability outcome.  All four endpoints are validated
    because every cardinality interpolates all of them.
    """

    for identifier in (
        relationship.source,
        relationship.target,
        relationship.source_column,
        relationship.target_column,
    ):
        _quote_identifier(identifier)


def _relationship_diagnostic(
    code: str, severity: Severity, message: str
) -> VerificationDiagnostic:
    """Build a relationship diagnostic on the shared physical path."""

    return VerificationDiagnostic(code, severity, _PATH, message)


@contextmanager
def _materialize_relationship(
    registry: SourceRegistry,
    temp_specs: tuple[tuple[str, str, str], ...],
) -> Iterator[None]:
    """Bind each distinct relationship source once and materialize it once.

    Each ``temp_specs`` entry is ``(temp_name, source_id, column)``.  The
    distinct sources are bound together under one
    :meth:`~SourceRegistry.bind_requirements` call (the registry lock spans the
    whole audit), and each distinct source is pulled *exactly once* into a
    private temp table holding every column its sides need.  The per-side
    single-column temp tables are then derived from those re-readable source
    temp tables — never from the bound relation directly.

    Single-pass query-scoped readers (PyIceberg scans, programmatic Arrow
    readers) cannot be re-scanned within one query, so a self-relationship
    (``source == target``) would otherwise consume its reader on the first
    side's materialization and scan empty for the second, false-unmatching a
    valid one-to-one or zeroing the other cardinality's informational counts.
    Deriving both sides from one materialized temp table reads the source once
    and keeps the two sides consistent.  Every temp table is dropped in a
    ``finally`` so only one relationship's tables ever exist.
    """

    # Distinct sources and the columns each needs, preserving first-seen
    # order so the source-temp-table names are deterministic.
    needed: dict[str, list[str]] = {}
    for _temp_name, source_id, column in temp_specs:
        needed.setdefault(source_id, []).append(column)
    requirements = {
        source_id: SourceScanRequirement(
            columns=tuple(dict.fromkeys(columns))
        )
        for source_id, columns in needed.items()
    }
    # One stable temp-table name per distinct source, in first-seen order.  A
    # relationship has at most two distinct sources, so two names suffice.
    source_temp = {
        source_id: _REL_SOURCES[index]
        for index, source_id in enumerate(needed)
    }
    with registry.bind_requirements(requirements):
        created: list[str] = []
        try:
            # Pull each distinct source exactly once into a temp table holding
            # all the columns its sides need, under their re-validated names.
            for source_id, columns in needed.items():
                distinct_columns = tuple(dict.fromkeys(columns))
                select_columns = ", ".join(
                    _quote_identifier(column) for column in distinct_columns
                )
                registry.execute(
                    "create or replace temp table "
                    f"{_quote_identifier(source_temp[source_id])} as "
                    f"select {select_columns} "
                    f"from {_quote_identifier(source_id)}"
                )
                created.append(source_temp[source_id])
            # Derive each side's single-column temp table from the re-readable
            # source temp table, never from the bound (single-pass) relation.
            for temp_name, source_id, column in temp_specs:
                registry.execute(
                    "create or replace temp table "
                    f"{_quote_identifier(temp_name)} as "
                    f"select {_quote_identifier(column)} as "
                    f"{_quote_identifier(_REL_KEY)} "
                    f"from {_quote_identifier(source_temp[source_id])}"
                )
                created.append(temp_name)
            yield
        finally:
            for temp_name in created:
                registry.execute(
                    "drop table if exists "
                    f"{_quote_identifier(temp_name)}"
                )


def _directed_relationship_counts(
    registry: SourceRegistry,
) -> tuple[int, int, int, int, int, int]:
    """Run the aggregate-only one-to-many / many-to-one cardinality scan.

    Counts one-side null rows, one-side duplicate groups, many-side null
    rows, non-null many-side orphans (``not exists``), zero-child one-side
    rows (``not exists``), and the maximum children per one-side parent
    (``left join`` against *distinct* one-side keys + ``group by``).  The
    one side is de-duplicated before the join so duplicate one-side parent
    rows do not multiply the per-parent child count.  No key value is ever
    selected.  The argument to ``execute`` is a single string literal whose
    only interpolated components are re-validated, double-quoted identifiers
    (the same validated-identifier contract as the grain SQL), so it is not a
    dynamic-SQL sink.
    """

    one = _quote_identifier(_REL_ONE)
    many = _quote_identifier(_REL_MANY)
    key = _quote_identifier(_REL_KEY)
    row = registry.execute(
        "select "
        f"(select count(*) from {one} where {key} is null) "
        "as one_side_null_rows, "
        f"(select count(*) from ("
        f"select {key} from {one} "
        f"group by {key} having count(*) > 1"
        ")) as one_side_duplicate_groups, "
        f"(select count(*) from {many} where {key} is null) "
        "as many_side_null_rows, "
        f"(select count(*) from {many} m "
        f"where m.{key} is not null and not exists ("
        f"select 1 from {one} o where o.{key} = m.{key}"
        ")) as orphan_non_null_rows, "
        f"(select count(*) from {one} o where not exists ("
        f"select 1 from {many} m where m.{key} = o.{key}"
        ")) as zero_child_one_side_rows, "
        f"(select coalesce(max(child_count), 0) from ("
        f"select count(m.{key}) as child_count "
        f"from (select distinct {key} from {one}) o "
        f"left join {many} m on m.{key} = o.{key} "
        f"group by o.{key}"
        ")) as maximum_child_multiplicity"
    ).fetchone()
    return (
        int(row[0]),
        int(row[1]),
        int(row[2]),
        int(row[3]),
        int(row[4]),
        int(row[5]),
    )


def _one_to_one_counts(
    registry: SourceRegistry,
) -> tuple[int, int, int, int, int, int]:
    """Run the aggregate-only one-to-one cardinality scan.

    Counts null and duplicate groups on each side plus unmatched non-null
    keys in both directions (``not exists``).  No key value is ever selected;
    the argument to ``execute`` is a single string literal of re-validated,
    double-quoted identifiers (same contract as the grain SQL).
    """

    left = _quote_identifier(_REL_LEFT)
    right = _quote_identifier(_REL_RIGHT)
    key = _quote_identifier(_REL_KEY)
    row = registry.execute(
        "select "
        f"(select count(*) from {left} where {key} is null) "
        "as source_null_rows, "
        f"(select count(*) from ("
        f"select {key} from {left} "
        f"group by {key} having count(*) > 1"
        ")) as source_duplicate_groups, "
        f"(select count(*) from {right} where {key} is null) "
        "as target_null_rows, "
        f"(select count(*) from ("
        f"select {key} from {right} "
        f"group by {key} having count(*) > 1"
        ")) as target_duplicate_groups, "
        f"(select count(*) from {left} l "
        f"where l.{key} is not null and not exists ("
        f"select 1 from {right} r where r.{key} = l.{key}"
        ")) as source_unmatched_non_null_rows, "
        f"(select count(*) from {right} r "
        f"where r.{key} is not null and not exists ("
        f"select 1 from {left} l where l.{key} = r.{key}"
        ")) as target_unmatched_non_null_rows"
    ).fetchone()
    return (
        int(row[0]),
        int(row[1]),
        int(row[2]),
        int(row[3]),
        int(row[4]),
        int(row[5]),
    )


def _many_to_many_counts(
    registry: SourceRegistry,
) -> tuple[int, int]:
    """Run the aggregate-only many-to-many unmatched-key scan.

    Counts non-null source keys with no target match and non-null target
    keys with no source match (``not exists`` in both directions).
    Many-to-many does not claim uniqueness, so no duplicate-group counts are
    computed.  No key value is ever selected; the argument to ``execute`` is
    a single string literal of re-validated, double-quoted identifiers (same
    contract as the grain SQL).
    """

    left = _quote_identifier(_REL_LEFT)
    right = _quote_identifier(_REL_RIGHT)
    key = _quote_identifier(_REL_KEY)
    row = registry.execute(
        "select "
        f"(select count(*) from {left} l "
        f"where l.{key} is not null and not exists ("
        f"select 1 from {right} r where r.{key} = l.{key}"
        ")) as source_unmatched_non_null_rows, "
        f"(select count(*) from {right} r "
        f"where r.{key} is not null and not exists ("
        f"select 1 from {left} l where l.{key} = r.{key}"
        ")) as target_unmatched_non_null_rows"
    ).fetchone()
    return (int(row[0]), int(row[1]))


def _relationship_check_id(relationship: Relationship) -> str:
    return f"relationship.{relationship.name}.cardinality"


def _directed_relationship_outcome(
    relationship: Relationship,
    counts: tuple[int, int, int, int, int, int],
) -> VerificationOutcome:
    """Build a one-to-many / many-to-one outcome from the six counts.

    Fails on one-side nulls, one-side duplicates, or non-null many-side
    orphans.  Nullable many-side keys and zero-child one-side rows stay
    informational (reported in evidence, never a failure).
    """

    (
        one_null,
        one_dup,
        many_null,
        orphan,
        zero_child,
        max_multiplicity,
    ) = counts
    diagnostics: list[VerificationDiagnostic] = []
    if one_null:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.one_side_null",
                "error",
                _REL_ONE_SIDE_NULL_MSG,
            )
        )
    if one_dup:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.one_side_duplicates",
                "error",
                _REL_ONE_SIDE_DUP_MSG,
            )
        )
    if orphan:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.many_side_orphans",
                "error",
                _REL_MANY_SIDE_ORPHAN_MSG,
            )
        )
    failed = bool(diagnostics)
    evidence: dict[str, EvidenceValue] = {
        "one_side_null_rows": one_null,
        "one_side_duplicate_groups": one_dup,
        "many_side_null_rows": many_null,
        "orphan_non_null_rows": orphan,
        "zero_child_one_side_rows": zero_child,
        "maximum_child_multiplicity": max_multiplicity,
    }
    return VerificationOutcome(
        check_id=_relationship_check_id(relationship),
        status="failed" if failed else "passed",
        scope="full_scan",
        path=_PATH,
        evidence=evidence,
        diagnostics=tuple(diagnostics),
    )


def _one_to_one_outcome(
    relationship: Relationship,
    counts: tuple[int, int, int, int, int, int],
) -> VerificationOutcome:
    """Build a one-to-one outcome, failing on any null, duplicate, or gap."""

    (
        source_null,
        source_dup,
        target_null,
        target_dup,
        source_unmatched,
        target_unmatched,
    ) = counts
    diagnostics: list[VerificationDiagnostic] = []
    if source_null:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.source_null",
                "error",
                _REL_SOURCE_NULL_MSG,
            )
        )
    if source_dup:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.source_duplicates",
                "error",
                _REL_SOURCE_DUP_MSG,
            )
        )
    if target_null:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.target_null",
                "error",
                _REL_TARGET_NULL_MSG,
            )
        )
    if target_dup:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.target_duplicates",
                "error",
                _REL_TARGET_DUP_MSG,
            )
        )
    if source_unmatched:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.source_unmatched",
                "error",
                _REL_SOURCE_UNMATCHED_MSG,
            )
        )
    if target_unmatched:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.target_unmatched",
                "error",
                _REL_TARGET_UNMATCHED_MSG,
            )
        )
    failed = bool(diagnostics)
    evidence: dict[str, EvidenceValue] = {
        "source_null_rows": source_null,
        "source_duplicate_groups": source_dup,
        "target_null_rows": target_null,
        "target_duplicate_groups": target_dup,
        "source_unmatched_non_null_rows": source_unmatched,
        "target_unmatched_non_null_rows": target_unmatched,
    }
    return VerificationOutcome(
        check_id=_relationship_check_id(relationship),
        status="failed" if failed else "passed",
        scope="full_scan",
        path=_PATH,
        evidence=evidence,
        diagnostics=tuple(diagnostics),
    )


def _many_to_many_outcome(
    relationship: Relationship, counts: tuple[int, int]
) -> VerificationOutcome:
    """Build a many-to-many outcome with an always-on no-traversal diagnostic.

    Many-to-many never claims uniqueness or safe traversal: a stable
    informational diagnostic is always attached.  The outcome fails only when
    either side has unmatched non-null keys.
    """

    source_unmatched, target_unmatched = counts
    diagnostics: list[VerificationDiagnostic] = [
        _relationship_diagnostic(
            "relationship.many_to_many_no_safe_traversal",
            "info",
            _REL_NO_SAFE_TRAVERSAL_MSG,
        )
    ]
    failed = False
    if source_unmatched:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.source_unmatched",
                "error",
                _REL_SOURCE_UNMATCHED_MSG,
            )
        )
        failed = True
    if target_unmatched:
        diagnostics.append(
            _relationship_diagnostic(
                "relationship.target_unmatched",
                "error",
                _REL_TARGET_UNMATCHED_MSG,
            )
        )
        failed = True
    evidence: dict[str, EvidenceValue] = {
        "source_unmatched_non_null_rows": source_unmatched,
        "target_unmatched_non_null_rows": target_unmatched,
    }
    return VerificationOutcome(
        check_id=_relationship_check_id(relationship),
        status="failed" if failed else "passed",
        scope="full_scan",
        path=_PATH,
        evidence=evidence,
        diagnostics=tuple(diagnostics),
    )


def _relationship_unavailable_outcome(
    relationship: Relationship, error: SourceError
) -> VerificationOutcome:
    """An ``unavailable`` relationship outcome carrying only the error code."""

    return VerificationOutcome(
        check_id=_relationship_check_id(relationship),
        status="unavailable",
        scope="full_scan",
        path=_PATH,
        evidence={"error_code": error.code},
        diagnostics=(),
    )


def _directed_specs(
    relationship: Relationship,
) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    sides = _relationship_sides(relationship)
    # ``_relationship_sides`` returns concrete sides for the directed types;
    # the ``None`` fields are unreachable here.
    assert sides.one_source is not None
    assert sides.one_column is not None
    assert sides.many_source is not None
    assert sides.many_column is not None
    return (
        (_REL_ONE, sides.one_source, sides.one_column),
        (_REL_MANY, sides.many_source, sides.many_column),
    )


def _bidirectional_specs(
    relationship: Relationship,
) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    return (
        (_REL_LEFT, relationship.source, relationship.source_column),
        (_REL_RIGHT, relationship.target, relationship.target_column),
    )


def _audit_relationship(
    registry: SourceRegistry, relationship: Relationship
) -> VerificationOutcome:
    """Audit one declared relationship's cardinality exactly.

    Both sides are bound and materialized under the registry lock, then a
    cardinality-specific aggregate-only SQL runs.  No key value is ever
    selected.  A sanitized :class:`~selayer.sources.errors.SourceError` from
    binding or a raw ``duckdb.Error`` from the materialization/scan is adapted
    to an ``unavailable`` outcome (raw failures normalize to ``scan_failed``)
    so no driver-derived detail can escape; a malformed endpoint identifier
    propagates unchanged as a programmer error.
    """

    _validate_relationship_identifiers(relationship)
    try:
        if relationship.type in ("one_to_many", "many_to_one"):
            specs = _directed_specs(relationship)
            with _materialize_relationship(registry, specs):
                counts = _directed_relationship_counts(registry)
            return _directed_relationship_outcome(relationship, counts)
        if relationship.type == "one_to_one":
            specs = _bidirectional_specs(relationship)
            with _materialize_relationship(registry, specs):
                counts = _one_to_one_counts(registry)
            return _one_to_one_outcome(relationship, counts)
        specs = _bidirectional_specs(relationship)
        with _materialize_relationship(registry, specs):
            counts = _many_to_many_counts(registry)
        return _many_to_many_outcome(relationship, counts)
    except (SourceError, duckdb.Error) as error:
        sanitized = (
            error
            if isinstance(error, SourceError)
            else SourceConnectionError(
                relationship.name,
                "scan_failed",
                "the relationship scan failed",
            )
        )
        return _relationship_unavailable_outcome(relationship, sanitized)


def _report(
    layer: SemanticLayer, outcomes: list[VerificationOutcome]
) -> VerificationReport:
    # Any required source that could not be audited makes the report
    # incomplete and is flagged by a single stable diagnostic so consumers can
    # treat the report as "not fully verified".  Failed outcomes (a data
    # quality issue the scan did observe) keep the report complete.
    unavailable = any(
        outcome.status == "unavailable" for outcome in outcomes
    )
    diagnostics: tuple[VerificationDiagnostic, ...] = ()
    if unavailable:
        diagnostics = (
            VerificationDiagnostic(
                code=_UNAVAILABLE_CODE,
                severity="error",
                path=_PATH,
                message="one or more required sources were unavailable for verification",
            ),
        )
    return VerificationReport(
        schema_version=1,
        subject=layer.name,
        check_kind="physical",
        complete=not unavailable,
        outcomes=tuple(outcomes),
        diagnostics=diagnostics,
    )


def verify_physical(layer: SemanticLayer, check: PhysicalCheck) -> VerificationReport:
    """Run an exact full-scan source-grain audit and return its report.

    A private in-memory DuckDB connection and a registry are created for the
    audit and closed in a ``finally``.  Every source is audited in sorted ID
    order: its declared grain becomes a single-source
    :class:`~selayer.sources.base.SourceScanRequirement`, the registry binds it
    under its lock, and the grain-count SQL scans the bound relation.

    A sanitized :class:`~selayer.sources.errors.SourceError` from registry
    creation is adapted to an ``unavailable`` outcome for every source; a
    per-source binding failure or a raw connector/scan failure from the bound
    scan is adapted to an ``unavailable`` outcome (binding failures keep their
    sanitized code; raw scan failures are normalized to ``scan_failed``) so no
    driver-derived detail can ever escape.  Truly-internal programmer errors
    (a malformed grain column) propagate unchanged.

    After the source-grain pass, every declared relationship is audited in
    sorted name order via :func:`_audit_relationship`, producing one
    ``relationship.<name>.cardinality`` outcome per relationship with the same
    secret-safe, lock-spanning contract; any unavailable bound source makes
    that outcome ``unavailable`` and the report incomplete.
    """

    profiles: RuntimeProfileResolver = (
        check.profiles if check.profiles is not None else MappingProfileResolver({})
    )
    arrow_providers: ArrowProviderResolver = (
        check.arrow_providers
        if check.arrow_providers is not None
        else MappingArrowProviderResolver({})
    )
    source_ids = sorted(layer.data_sources)
    outcomes: list[VerificationOutcome] = []
    connection: Any = duckdb.connect(":memory:")
    registry: SourceRegistry | None = None
    try:
        try:
            registry = SourceRegistry.create(
                layer, connection, profiles, arrow_providers
            )
        except SourceError as error:
            # ``create`` already closed the connection; every source is
            # unavailable.  No status is available because nothing registered.
            for source_id in source_ids:
                outcomes.append(_unavailable_outcome(source_id, error, None))
            for relationship_id in sorted(layer.relationships):
                outcomes.append(
                    _relationship_unavailable_outcome(
                        layer.relationships[relationship_id], error
                    )
                )
            return _report(layer, outcomes)
        try:
            for source_id in source_ids:
                grain = layer.data_sources[source_id].grain
                # Validate grain identifiers *before* binding so a malformed
                # layer declaration raises a clean programmer error instead of
                # being sanitized into an availability outcome.  This keeps the
                # scan-time handling below free to catch every connector/scan
                # failure without masking a layer-declaration bug.
                _validate_grain_identifiers(source_id, grain)
                status: SourceStatus | None = None
                try:
                    with registry.bind_requirements(
                        {source_id: SourceScanRequirement(columns=grain)}
                    ):
                        # Read connector metadata *inside* the binding context
                        # (the registry lock is held) so generation/snapshot/
                        # schema_fingerprint reflect the same bound scan the
                        # grain SQL runs over; a concurrent reload cannot swap
                        # the handle between the status read and the scan.
                        status = registry.status(source_id)
                        counts = _grain_counts(registry, source_id, grain)
                except (SourceError, duckdb.Error) as error:
                    # Global "no raw error wins": a sanitized ``SourceError``
                    # from binding or status, or any raw connector/scan
                    # ``duckdb.Error`` from the bound scan, is adapted to a
                    # sanitized ``unavailable`` outcome so no driver-derived
                    # location, handle, or credential text can ever escape.
                    # The error is constructed (not raised) here, so
                    # ``__cause__``/``__context__`` remain ``None``.
                    sanitized = (
                        error
                        if isinstance(error, SourceError)
                        else SourceConnectionError(
                            source_id, "scan_failed", "the source scan failed"
                        )
                    )
                    outcomes.append(
                        _unavailable_outcome(source_id, sanitized, status)
                    )
                else:
                    outcomes.append(_grain_outcome(source_id, status, counts))
            for relationship_id in sorted(layer.relationships):
                outcomes.append(
                    _audit_relationship(
                        registry, layer.relationships[relationship_id]
                    )
                )
        finally:
            registry.close()
    finally:
        # ``registry.close`` (and the create-failure path) already closed the
        # connection; guard the redundant close so a double-close never raises.
        try:
            connection.close()
        except Exception:  # noqa: BLE001, S110
            pass
    return _report(layer, outcomes)
