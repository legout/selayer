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
"""

from __future__ import annotations

import re
from typing import Any

import duckdb

from selayer.model import SemanticLayer
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
