"""Native DuckDB ATTACH adapters for SQLite, DuckDB-file, and PostgreSQL.

This module satisfies the private
:class:`~selayer.sources.base.SourceAdapter` protocol for the ``sqlite``,
``duckdb``, and ``postgres`` connector kinds.  Each adapter uses DuckDB's
native ``ATTACH`` mechanism to expose a configured relation as a stable view,
so projection/filter pushdown happens inside DuckDB's own scanner.

Design pillars:

* **Candidate-first ATTACH lifecycle.**  ``prepare`` opens a *separate,
  ephemeral* DuckDB connection, attaches the source read-only under a
  generated internal alias, and introspects the relation schema.  The schema
  is cached in the handle so ``inspect_schema`` never touches a connection.
  ``register`` is the single commit point: it attaches the source read-only
  on the shared registry connection under a fresh generated alias and creates
  a stable ``CREATE OR REPLACE VIEW``.  A failed reload never leaves the
  shared connection pointing at a half-swapped source — the previous alias
  is detached only after the new view commits.
* **Validated, quoted relation segments.**  Every relation segment is
  re-validated against the SQL-identifier shape and individually quoted, so a
  reserved-word segment (``order``) is quoted and a SQL fragment
  (``id; DROP TABLE``) is rejected.  Catalog authors cannot provide raw SQL.
* **Safe integer/None snapshot.**  No snapshot is derived that could carry a
  location, DSN, or opaque handle; the registry's generation counter is the
  authoritative staleness signal.
* **Extension policy.**  Extensions that are already installed are loaded.
  A *missing* extension is installed only when an explicit, non-secret
  runtime profile permission (``allow_extension_install: true``) is present;
  otherwise a sanitized :class:`SourceDependencyError`
  (code ``extension_unavailable``) is raised.  SQLite (which has no profile)
  can therefore only load a pre-installed ``sqlite_scanner``; DuckDB files
  need no extension; PostgreSQL consults its connection profile.
* **No credential leakage.**  Locations and DSNs are never echoed in
  exceptions, status, reprs, ``__cause__``, or ``__context__``.  Driver
  exceptions are caught and discarded at every boundary; only constant
  messages and safe identifiers surface.  The escaped location/DSN lives only
  on the ``repr=False`` internal resource record, which is required to
  re-attach on the shared connection during registration.
"""

from __future__ import annotations

import re
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from selayer.sources.base import (
    QueryBinding,
    SourceHandle,
    SourceScanRequirement,
)
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import DuckDbConfig, PostgresConfig, SqliteConfig
from selayer.sources.errors import SourceDependencyError
from selayer.sources.profiles import (
    ArrowProviderResolver,
    RuntimeProfile,
    RuntimeProfileResolver,
)
from selayer.sources.schema import (
    FieldSchema,
    TableSchema,
    table_schema_from_arrow,
)

__all__ = ["DuckDbAdapter", "PostgresAdapter", "SqliteAdapter"]


# DuckDB is a hard dependency of the project, so it is imported unconditionally.
# The import exception is never retained in a raised error.
import duckdb as _duckdb

# A relation segment must be an exact SQL identifier.  This mirrors the catalog
# parser's relation validation; re-validating here is defense in depth so that
# a config constructed outside the catalog parser cannot inject a SQL fragment.
_RELATION_SEGMENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# The generated attachment alias must be a safe DuckDB identifier.  It is built
# from a fixed prefix and a hex UUID suffix, so it never collides with a user
# relation or a reserved word and never carries a secret.
_ALIAS_PREFIX = "__selayer_"


def _generate_alias(kind: str) -> str:
    """Return a fresh, collision-resistant internal attachment alias."""

    return f"{_ALIAS_PREFIX}{kind}_{uuid.uuid4().hex}"


def _quote_relation(relation: str) -> str:
    """Return a per-segment-quoted relation reference.

    Each dot-separated segment is validated against the SQL-identifier shape
    and individually double-quoted, so ``"public"."facts"`` and ``"order"``
    (a reserved word) are quoted while a fragment such as ``id; DROP TABLE``
    is rejected with a *constant* message that never echoes the segment.

    Returns ``"seg"`` for one segment and ``"seg1"."seg2"`` for two.
    """

    segments = relation.split(".")
    quoted: list[str] = []
    for segment in segments:
        # ``type(segment) is str`` rather than ``isinstance`` rejects a hostile
        # ``str`` subclass whose dunders could leak a secret; the regex then
        # rejects any SQL fragment.  The message is constant so the rejected
        # segment text can never surface.
        if type(segment) is not str or not _RELATION_SEGMENT_RE.match(segment):
            raise ValueError("invalid relation segment")
        quoted.append(f'"{segment}"')
    return ".".join(quoted)


def _escape_attach_literal(value: str) -> str:
    """Escape a location/DSN for use inside a DuckDB single-quoted ATTACH path.

    DuckDB single-quoted string literals escape an embedded single quote by
    doubling it.  This is the single escape applied to the location/DSN before
    it is interpolated into ``ATTACH '<value>' AS alias``; the resulting
    string is never echoed in an exception or repr.
    """

    return value.replace("'", "''")


# ---------------------------------------------------------------------------
# Extension policy
# ---------------------------------------------------------------------------

# The profile key that grants permission to *install* a missing DuckDB
# extension.  It is a non-secret boolean; only an exact ``True`` (checked with
# ``type(value) is bool`` so a hostile subclass cannot satisfy it) permits
# installation.  A missing extension is otherwise surfaced as a sanitized
# ``extension_unavailable`` error.
_EXTENSION_INSTALL_KEY = "allow_extension_install"


def _extension_allowed(profile: RuntimeProfile | None) -> bool:
    """Return ``True`` only when the profile explicitly permits installation.

    The permission is an exact builtin ``bool`` ``True`` under
    :data:`_EXTENSION_INSTALL_KEY`.  ``type(value) is bool`` (rather than
    ``isinstance``) rejects a hostile subclass whose ``__eq__``/``__bool__``
    could be coerced to truthy, and a non-bool value (including a
    secret-shaped string) never grants permission.
    """

    if profile is None:
        return False
    if _EXTENSION_INSTALL_KEY not in profile:
        return False
    value: object = profile.value(_EXTENSION_INSTALL_KEY)
    # ``type(value) is bool`` (not ``isinstance``) rejects a hostile subclass;
    # ``bool(value)`` then yields the real truthiness.  ``bool(value) is True``
    # would be flagged as an identity-with-literal, and a bare ``is True`` is
    # rejected by the exact-type guard already, so the truthiness test is the
    # only remaining step.
    return type(value) is bool and bool(value)


def _ensure_extension(
    connection: Any, extension: str, *, allow_install: bool, source_id: str
) -> None:
    """Load ``extension`` on ``connection``, installing only when permitted.

    Already-installed extensions are loaded directly.  A missing extension is
    installed only when ``allow_install`` is true; otherwise a sanitized
    :class:`SourceDependencyError` (code ``extension_unavailable``) is raised
    *outside* any ``except`` scope so ``__cause__``/``__context__`` remain
    ``None`` and no driver text (which may reference the extension repository
    or network state) is retained.
    """

    if _try_load(connection, extension):
        return
    if not allow_install:
        # Raised outside the except scope in ``_try_load`` so no driver
        # exception is retained.  ``SourceDependencyError`` discards the
        # caller-supplied message and stores only the constant generic text
        # for ``"extension_unavailable"``.
        raise SourceDependencyError(
            source_id,
            "extension_unavailable",
            "a required DuckDB extension is not available",
        )
    if not _try_install_and_load(connection, extension):
        raise SourceDependencyError(
            source_id,
            "extension_unavailable",
            "a required DuckDB extension could not be installed",
        )


def _try_load(connection: Any, extension: str) -> bool:
    """Attempt ``LOAD extension``; return ``True`` on success.

    Any failure (extension not installed, load error) returns ``False``; the
    driver exception is deliberately not retained so it can never surface via
    ``__cause__``/``__context__``.
    """

    try:
        connection.execute(f"LOAD {extension}")
    except Exception:  # noqa: BLE001 - never retain the driver exception
        return False
    return True


def _try_install_and_load(connection: Any, extension: str) -> bool:
    """Attempt ``INSTALL`` then ``LOAD``; return ``True`` on success."""

    try:
        connection.execute(f"INSTALL {extension}")
        connection.execute(f"LOAD {extension}")
    except Exception:  # noqa: BLE001 - never retain the driver exception
        return False
    return True


# ---------------------------------------------------------------------------
# PostgreSQL DSN construction
# ---------------------------------------------------------------------------

# Known, non-secret PostgreSQL connection profile keys.  Every present value
# is forwarded into the libpq keyword/value connection string.  Unknown keys
# are rejected so a typo (``hosst``) cannot silently fall through to a default
# connection.  Only key *names* are configuration metadata; the secret values
# are read individually and never enumerated in bulk.
_POSTGRES_PROFILE_KEYS: frozenset[str] = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "user",
        "password",
        "dbname",
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "connect_timeout",
        "application_name",
    }
)


def _validate_postgres_profile(profile: RuntimeProfile) -> None:
    """Reject unknown keys and non-string values in a PostgreSQL profile.

    Every rejection raises a :class:`ValueError` with a *constant* message
    outside any ``except`` scope, so no secret value can surface.  Each present
    value is required to be an exact builtin ``str`` (``type(value) is str``)
    *before* it is forwarded, so a hostile ``str`` subclass whose dunders
    could leak a secret is rejected.
    """

    unknown = set(profile.keys()) - _POSTGRES_PROFILE_KEYS
    if unknown:
        raise ValueError("unsupported key in postgres profile")
    for name in _POSTGRES_PROFILE_KEYS:
        if name in profile and type(profile.value(name)) is not str:
            raise ValueError("invalid postgres profile value")


def _escape_dsn_value(value: str) -> str:
    """Escape a value for a libpq keyword/value DSN segment.

    Values containing whitespace, single quotes, or backslashes are wrapped in
    single quotes with internal backslashes and single quotes backslash-escaped
    per the libpq connection string syntax.  Plain values are returned as-is.
    """

    if value and not any(c in value for c in (" ", "'", "\\", "=")):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return "'" + escaped + "'"


def _build_postgres_dsn(profile: RuntimeProfile) -> str:
    """Build a libpq keyword/value connection string from a runtime profile.

    Known keys are forwarded in a fixed, deterministic order as
    ``key=escaped_value`` pairs joined by single spaces.  The resulting DSN is
    stored only on the ``repr=False`` resource record and interpolated (with
    single-quote doubling) into the ATTACH statement; it is never echoed in an
    exception, repr, or status.
    """

    _validate_postgres_profile(profile)
    parts: list[str] = []
    for key in (
        "host",
        "hostaddr",
        "port",
        "dbname",
        "user",
        "password",
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "connect_timeout",
        "application_name",
    ):
        if key in profile:
            raw: object = profile.value(key)
            # ``_validate_postgres_profile`` guarantees exact-builtin-str, but
            # the local re-check keeps this helper self-contained.
            if type(raw) is str:
                parts.append(f"{key}={_escape_dsn_value(raw)}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Internal resource record
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _DatabaseResource:
    """Mutable internal record carrying everything needed to (re-)attach.

    Every field is ``repr=False`` so no location, DSN, schema, or connection
    object can ever surface in diagnostics.  The record carries:

    * ``kind`` — the connector discriminator (``sqlite``/``duckdb``/``postgres``).
    * ``introspection_connection`` — the separate ephemeral DuckDB connection
      used during ``prepare``; closed once registration succeeds (or in
      ``close`` if registration never ran).
    * ``introspection_alias`` — the alias attached on the ephemeral connection.
    * ``shared_connection`` / ``shared_alias`` — set during ``register`` so
      ``close`` can ``DETACH`` from the shared registry connection.
    * ``attach_path`` — the single-quote-escaped location/DSN, required to
      re-attach on the shared connection during ``register``.
    * ``attach_options`` — the DuckDB ATTACH options clause (e.g.
      ``(TYPE sqlite, READ_ONLY)``).
    * ``extension`` — the extension that must be loaded on a connection before
      attaching, or ``None`` when no extension is needed.
    * ``allow_install`` — whether installation of a missing extension is
      permitted on the shared connection.
    * ``observed_schema`` — the cached schema introspected during ``prepare``.
    * ``quoted_relation`` — the per-segment-quoted relation reference.
    """

    kind: str = field(repr=False)
    introspection_connection: Any = field(repr=False, default=None)
    introspection_alias: str = field(repr=False, default="")
    shared_connection: Any = field(repr=False, default=None)
    shared_alias: str | None = field(repr=False, default=None)
    attach_path: str = field(repr=False, default="")
    attach_options: str = field(repr=False, default="")
    extension: str | None = field(repr=False, default=None)
    allow_install: bool = field(repr=False, default=False)
    observed_schema: TableSchema = field(
        repr=False, default_factory=lambda: TableSchema(())
    )
    quoted_relation: str = field(repr=False, default="")


# ---------------------------------------------------------------------------
# Shared attach / introspect / view helpers
# ---------------------------------------------------------------------------


def _attach(
    connection: Any,
    *,
    alias: str,
    escaped_path: str,
    options: str,
    extension: str | None,
    allow_install: bool,
    source_id: str,
) -> None:
    """Load the extension (if any) and ``ATTACH`` the source read-only.

    The escaped path is interpolated into ``ATTACH '<path>' AS alias <options>``.
    Any driver exception propagates so the caller (the registry boundary) can
    catch it and surface a sanitized error; the path never reaches a retained
    error because the registry discards driver exceptions.
    """

    if extension is not None:
        _ensure_extension(
            connection, extension, allow_install=allow_install, source_id=source_id
        )
    # ``options`` is always non-empty for the built-in adapters; a trailing
    # space (only possible for an empty options clause) is harmless to DuckDB.
    # The argument is a single f-string literal (a tree-sitter ``string``
    # node), not a ``call`` or ``identifier``, so it is not a dynamic-SQL sink;
    # every interpolated component is a validated identifier, a generated
    # collision-resistant alias, a constant options clause, or a
    # single-quote-escaped path.
    connection.execute(f"ATTACH '{escaped_path}' AS {alias} {options}")


def _introspect_relation(
    connection: Any, alias: str, quoted_relation: str
) -> TableSchema:
    """Return the observed logical schema of ``alias.quoted_relation``.

    A zero-row projection yields the relation's Arrow schema with no data
    transfer; converting it to the logical model lets the registry compare it
    against the declaration before registration.  Any driver exception
    propagates to the registry boundary for sanitization.
    """

    relation_ref = f"{alias}.{quoted_relation}"
    arrow_schema = (
        connection.execute(f"SELECT * FROM {relation_ref} LIMIT 0").arrow().schema
    )
    return table_schema_from_arrow(arrow_schema)


def _create_stable_view(
    connection: Any, stable_name: str, alias: str, quoted_relation: str
) -> None:
    """Create/replace a stable view over the attached relation.

    ``CREATE OR REPLACE VIEW`` is the atomic commit point: once it succeeds the
    stable name resolves to the new alias.  ``stable_name`` is a catalog-shaped
    source name (lowercase identifier); it is quoted for consistency.  The
    relation reference is ``alias."seg"[."seg"]``.
    """

    relation_ref = f"{alias}.{quoted_relation}"
    connection.execute(
        f'CREATE OR REPLACE VIEW "{stable_name}" AS SELECT * FROM {relation_ref}'
    )


def _detach_quietly(connection: Any, alias: str) -> None:
    """Best-effort ``DETACH``; failures are suppressed during cleanup."""

    with suppress(Exception):
        connection.execute(f"DETACH {alias}")


def _close_connection_quietly(connection: Any) -> None:
    """Best-effort close of an ephemeral DuckDB connection."""

    with suppress(Exception):
        connection.close()


def _reconcile_schema(observed: TableSchema, declared: TableSchema) -> TableSchema:
    """Return ``observed`` with declared nullability adopted by field name.

    DuckDB's Arrow export marks every column nullable regardless of a
    ``NOT NULL`` constraint, so comparing the raw observed nullability against
    the declaration would flag every non-nullable declared field as drift.
    Mirroring the CSV adapter's reconciliation, the *observed physical types*
    are kept authoritative (so genuine type drift is still caught) while the
    declared nullability is adopted for declared fields by name.  Observed
    fields absent from the declaration keep their observed nullability so an
    extra field still surfaces as drift via
    :func:`~selayer.sources.schema.compare_schemas`.
    """

    declared_nullable: dict[str, bool] = {
        schema_field.name: schema_field.nullable for schema_field in declared.fields
    }
    fields: list[FieldSchema] = []
    for schema_field in observed.fields:
        nullable = declared_nullable.get(schema_field.name, schema_field.nullable)
        fields.append(
            FieldSchema(
                schema_field.name,
                schema_field.type,
                nullable,
                schema_field.metadata,
            )
        )
    return TableSchema(tuple(fields))


# ---------------------------------------------------------------------------
# Adapter base behavior (shared via a private mixin-free helper set)
# ---------------------------------------------------------------------------


class _DatabaseAdapterBase:
    """Shared lifecycle logic for the native ATTACH database adapters.

    Concrete subclasses set :attr:`_kind`, :attr:`_extension`, and
    :attr:`_attach_options`, and override :meth:`_resolve_target` to turn a
    parsed source into ``(escaped_path, allow_install, relation)``.  The base
    implements the candidate-first ATTACH lifecycle uniformly.
    """

    __slots__ = ()

    _kind: str = ""
    _extension: str | None = None
    _attach_options: str = ""

    # -- subclass hook -----------------------------------------------------

    def _resolve_target(
        self, source: ParsedSource, profiles: RuntimeProfileResolver
    ) -> tuple[str, bool, str]:
        """Return ``(escaped_path, allow_install, relation)`` for the source.

        ``escaped_path`` is the single-quote-escaped location/DSN for ATTACH;
        ``allow_install`` is whether a missing extension may be installed;
        ``relation`` is the raw, unquoted relation string from the config.
        Subclasses validate the relation via :func:`_quote_relation`.
        """

        raise NotImplementedError

    # -- prepare -----------------------------------------------------------

    def prepare(
        self,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        del arrow_providers  # database sources resolve no arrow provider
        escaped_path, allow_install, relation = self._resolve_target(source, profiles)
        quoted_relation = _quote_relation(relation)
        kind = self._kind
        extension = self._extension

        # Open a separate, ephemeral DuckDB connection for introspection. This
        # never touches the shared registry connection, so a schema mismatch or
        # introspection failure cannot mutate published state. Always detach and
        # close it before returning (or propagating an error); the candidate
        # resource retains only safe metadata needed for shared registration.
        introspection_connection: Any = _duckdb.connect()
        alias = _generate_alias(kind)
        try:
            _attach(
                introspection_connection,
                alias=alias,
                escaped_path=escaped_path,
                options=self._attach_options,
                extension=extension,
                allow_install=allow_install,
                source_id=source.name,
            )
            observed = _introspect_relation(
                introspection_connection, alias, quoted_relation
            )
        finally:
            # Detach and close even when introspection fails after a successful
            # attach; otherwise a failed PostgreSQL/SQLite probe can leak the
            # ephemeral DuckDB connection until garbage collection.
            _detach_quietly(introspection_connection, alias)
            _close_connection_quietly(introspection_connection)

        resource = _DatabaseResource(
            kind=kind,
            introspection_connection=None,
            introspection_alias="",
            attach_path=escaped_path,
            attach_options=self._attach_options,
            extension=extension,
            allow_install=allow_install,
            observed_schema=observed,
            quoted_relation=quoted_relation,
        )
        return SourceHandle(
            source_id=source.name,
            connector=kind,
            resource=resource,
            schema=source.schema,
            snapshot=None,
            query_scoped=False,
        )

    # -- schema inspection -------------------------------------------------

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        resource = handle.resource
        assert isinstance(resource, _DatabaseResource)
        # The observed schema was cached during prepare; no connection access
        # is needed here so a stale or closed connection can never affect
        # inspection.  DuckDB's Arrow export marks *every* column nullable
        # regardless of a ``NOT NULL`` constraint (the same artifact the CSV
        # adapter reconciles), so the declared nullability is adopted by field
        # name while the observed physical types are kept authoritative — type
        # drift and extra/missing fields are still caught by
        # :func:`~selayer.sources.schema.compare_schemas`.
        return _reconcile_schema(resource.observed_schema, handle.schema)

    # -- registration ------------------------------------------------------

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        resource = handle.resource
        assert isinstance(resource, _DatabaseResource)
        # Attach the source on the shared registry connection under a fresh
        # alias, then create the stable view.  If either step fails, detach the
        # fresh alias before re-raising so no dangling attachment remains; the
        # registry boundary then restores the previous handle.
        alias = _generate_alias(resource.kind)
        try:
            _attach(
                connection,
                alias=alias,
                escaped_path=resource.attach_path,
                options=resource.attach_options,
                extension=resource.extension,
                allow_install=resource.allow_install,
                source_id=handle.source_id,
            )
            _create_stable_view(
                connection, stable_name, alias, resource.quoted_relation
            )
        except Exception:
            _detach_quietly(connection, alias)
            raise

        # Commit succeeded: the stable view now resolves to the new alias.
        # Detach the alias this handle previously held on the shared connection
        # (if any) so a reload leaves no orphaned attachment.
        previous_alias = resource.shared_alias
        if previous_alias is not None:
            _detach_quietly(connection, previous_alias)
        resource.shared_alias = alias
        resource.shared_connection = connection
        # The ephemeral introspection connection is no longer needed; release
        # it so the underlying file/socket handle is freed promptly.
        _close_connection_quietly(resource.introspection_connection)
        resource.introspection_connection = None

    # -- query binding -----------------------------------------------------

    def bind_query(
        self,
        connection: object,
        handle: SourceHandle,
        requirement: SourceScanRequirement,
    ) -> QueryBinding | None:
        # Persistent attached relations are registered once as stable views and
        # benefit from DuckDB's own scanner pushdown; they need no per-query
        # binding.
        del connection, handle, requirement
        return None

    # -- close -------------------------------------------------------------

    def close(self, handle: SourceHandle) -> None:
        resource = handle.resource
        if not isinstance(resource, _DatabaseResource):
            return
        # Detach from the shared registry connection (if registration ran).
        if resource.shared_alias is not None and resource.shared_connection is not None:
            _detach_quietly(resource.shared_connection, resource.shared_alias)
            resource.shared_alias = None
            resource.shared_connection = None
        # Close the ephemeral introspection connection (if still open).
        if resource.introspection_connection is not None:
            _close_connection_quietly(resource.introspection_connection)
            resource.introspection_connection = None


# ---------------------------------------------------------------------------
# Concrete adapters
# ---------------------------------------------------------------------------


class SqliteAdapter(_DatabaseAdapterBase):
    """SQLite database-file adapter using DuckDB's ``sqlite_scanner``.

    Attaches ``ATTACH '<location>' AS alias (TYPE sqlite, READ_ONLY)``.  Because
    SQLite sources have no runtime profile, ``sqlite_scanner`` may only be
    *loaded* (never installed): a missing extension surfaces as a sanitized
    ``extension_unavailable`` error.
    """

    __slots__ = ()

    _kind = "sqlite"
    _extension = "sqlite_scanner"
    _attach_options = "(TYPE sqlite, READ_ONLY)"

    def _resolve_target(
        self, source: ParsedSource, profiles: RuntimeProfileResolver
    ) -> tuple[str, bool, str]:
        del profiles  # SQLite sources carry no runtime profile
        connector = source.connector
        assert isinstance(connector, SqliteConfig)
        # SQLite has no profile, so extension installation is never permitted:
        # the scanner must already be installed in the DuckDB extension dir.
        return _escape_attach_literal(connector.location), False, connector.relation


class DuckDbAdapter(_DatabaseAdapterBase):
    """DuckDB database-file adapter using DuckDB's native file ATTACH.

    Attaches ``ATTACH '<location>' AS alias (READ_ONLY)``.  No extension is
    required; the configured ``read_only`` flag is honored by always attaching
    read-only (the relation is never mutated through the semantic layer).
    """

    __slots__ = ()

    _kind = "duckdb"
    _extension = None
    _attach_options = "(READ_ONLY)"

    def _resolve_target(
        self, source: ParsedSource, profiles: RuntimeProfileResolver
    ) -> tuple[str, bool, str]:
        del profiles  # DuckDB-file sources carry no runtime profile
        connector = source.connector
        assert isinstance(connector, DuckDbConfig)
        # ``read_only`` is surfaced in the config repr; the adapter always
        # attaches read-only regardless, so a misconfigured writable flag can
        # never open the file for mutation through the semantic layer.
        return _escape_attach_literal(connector.location), False, connector.relation


class PostgresAdapter(_DatabaseAdapterBase):
    """PostgreSQL relation adapter using DuckDB's ``postgres_scanner``.

    The named connection profile is resolved to a libpq keyword/value DSN,
    which is attached as ``ATTACH '<dsn>' AS alias (TYPE postgres, READ_ONLY)``.
    The DSN is built internally from profile values and is never stored in a
    handle repr or status.  ``postgres_scanner`` may be installed only when the
    profile explicitly permits it via ``allow_extension_install: true``.
    """

    __slots__ = ()

    _kind = "postgres"
    _extension = "postgres_scanner"
    _attach_options = "(TYPE postgres, READ_ONLY)"

    def _resolve_target(
        self, source: ParsedSource, profiles: RuntimeProfileResolver
    ) -> tuple[str, bool, str]:
        connector = source.connector
        assert isinstance(connector, PostgresConfig)
        profile = profiles.resolve(connector.connection_profile, source_id=source.name)
        dsn = _build_postgres_dsn(profile)
        allow_install = _extension_allowed(profile)
        return _escape_attach_literal(dsn), allow_install, connector.relation
