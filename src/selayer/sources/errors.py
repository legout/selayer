"""Sanitized source lifecycle errors.

Every source lifecycle failure surfaces as a :class:`SourceError` subclass
that carries a UUIDv4 ``operation_id`` (generated at the lifecycle boundary),
the affected ``source_id``, a stable symbolic ``code``, and a *constant*
sanitized ``message``.

Three guarantees are load-bearing for the secrecy contract:

* **No arbitrary caller/driver text is ever retained.**  The caller-supplied
  ``message`` is intentionally discarded — only a constant generic message
  looked up from ``code`` is stored, so no driver-derived detail can surface.
  ``code`` is validated against a *known-code allowlist* and ``source_id``
  against the catalog source-name shape, each coerced to a placeholder when
  it does not match (and each accepted only as an exact builtin ``str`` — a
  hostile ``str`` subclass with a leaky ``__repr__`` is coerced to the
  placeholder), and an explicit ``operation_id`` is honored only when it
  parses as a UUIDv4 (otherwise a fresh one is generated).
* **No retained driver exceptions.**  Driver exceptions are never stored.
  Errors must be constructed and raised *outside* active ``except`` scopes so
  ``__cause__`` and ``__context__`` remain ``None``.
* **Safe reprs.**  The repr renders only the validated ``operation_id``,
  ``source_id``, ``code``, and the constant ``message`` — all of which are safe
  identifiers or constant text, never driver material.
"""

from __future__ import annotations

import re
import uuid

__all__ = [
    "SourceConnectionError",
    "SourceDependencyError",
    "SourceError",
    "SourceProfileError",
    "SourceReloadError",
    "SourceSchemaError",
    "new_operation_id",
]


def new_operation_id() -> str:
    """Return a fresh UUIDv4 string identifying a single source lifecycle op.

    Lifecycle boundaries (resolvers, adapters) call this — or let
    :class:`SourceError` default it — so every error carries a unique
    correlation id without retaining any driver state.
    """

    return str(uuid.uuid4())


# Constant, generic messages keyed by stable error code.  The caller-supplied
# ``message`` is *never* retained; only these constant strings are stored, so
# no driver-derived detail can surface in diagnostics.  Unknown codes fall back
# to ``_FALLBACK_MESSAGE``.
_CODE_MESSAGES: dict[str, str] = {
    "missing_profile": "a required runtime profile is not configured",
    "missing_arrow_provider": "a required arrow provider handle is not configured",
    "connect_failed": "the source connection could not be established",
    "bind_failed": "the source could not be bound for the query",
    "scan_failed": "the source could not be scanned",
    "schema_mismatch": "the observed schema does not match the declared schema",
    "reload_failed": "the source could not be reloaded",
    "reload_all_failed": "one or more sources could not be reloaded together",
    "source_initialization_failed": (
        "a data source could not be initialized during registry creation"
    ),
    "unsupported_connector": (
        "the connector type is not supported by any registered adapter"
    ),
    "missing_delta_dependency": ("the deltalake package is required for delta sources"),
    "missing_iceberg_dependency": (
        "the pyiceberg package is required for iceberg sources"
    ),
    "extension_unavailable": (
        "a required DuckDB extension is not available and may not be installed"
    ),
}

# Known source error codes — an allowlist, *not* a permissive regex.  Only a
# code in this set is retained; anything else (e.g. ``TOKENONLYCODE`` or a SQL
# fragment) is coerced to ``"unknown"`` so arbitrary caller/driver text can
# never surface as a code.
_KNOWN_CODES: frozenset[str] = frozenset(_CODE_MESSAGES)

_FALLBACK_MESSAGE = "a source lifecycle error occurred"

# Source-name identifier shape — the *exact* convention the catalog enforces
# for declared source names (lowercase snake_case).  Only a ``source_id`` that
# matches this specific validated shape is retained/rendered; anything else
# (uppercase secret tokens like ``TOKENONLYSECRET``, SQL fragments, credential
# URIs) is replaced with ``"<source>"``.  This is not a permissive catch-all
# regex: it is the project's own source-name validation, so only
# catalog-shaped identifiers surface in diagnostics.
_SOURCE_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]*\Z")


def _safe_code(code: object) -> str:
    """Return ``code`` only if it is a *known* error code, else ``"unknown"``.

    Only an *exact* builtin ``str`` is accepted (``type(code) is str``, not
    ``isinstance``): a hostile ``str`` subclass passes ``isinstance(str)`` yet
    can carry a custom ``__repr__`` that leaks a secret when rendered, so it is
    coerced to ``"unknown"`` rather than retained.
    """

    return code if type(code) is str and code in _KNOWN_CODES else "unknown"


def _safe_source_id(source_id: object) -> str:
    """Return ``source_id`` only if it is a catalog-shaped name, else ``"<source>"``.

    Only an *exact* builtin ``str`` is accepted (``type(source_id) is str``,
    not ``isinstance``): a hostile ``str`` subclass passes ``isinstance(str)``
    yet can carry a custom ``__repr__`` that leaks a secret when rendered, so
    it is coerced to ``"<source>"`` rather than retained.
    """

    return (
        source_id
        if type(source_id) is str and _SOURCE_NAME_RE.match(source_id)
        else "<source>"
    )


def _validated_operation_id(operation_id: str | None) -> str:
    """Return a UUIDv4 operation id, validating any caller-supplied value.

    A supplied value is honored only when it parses as a UUIDv4 (normalized to
    its canonical lowercase form); any other value is replaced with a fresh
    UUIDv4 so no arbitrary caller/driver text can be stored as the operation id.
    """

    if operation_id is not None:
        try:
            parsed = uuid.UUID(operation_id)
        except (ValueError, AttributeError, TypeError):
            parsed = None
        if parsed is not None and parsed.version == 4:
            return str(parsed)
    return new_operation_id()


def _message_for_code(code: str) -> str:
    """Return the constant generic message for ``code`` (fallback if unknown)."""

    return _CODE_MESSAGES.get(code, _FALLBACK_MESSAGE)


class SourceError(Exception):
    """Base class for sanitized source lifecycle errors.

    Only safe, derived identifiers are stored:

    * ``operation_id`` — a UUIDv4 (validated if supplied, else generated).
    * ``source_id`` — a catalog-shaped name, else ``"<source>"``.
    * ``code`` — a known error code, else ``"unknown"``.
    * ``message`` — a *constant* generic message looked up from ``code``; the
      caller-supplied message is intentionally ignored so driver-derived detail
      can never surface.

    Driver exceptions are never retained.  Errors must be constructed and
    raised outside active ``except`` scopes so ``__cause__`` and ``__context__``
    remain ``None``.

    Attributes:
        operation_id: UUIDv4 identifying the lifecycle operation.
        source_id: stable identifier of the affected source.
        code: short symbolic error code (constant, no credentials).
        message: constant, sanitized human-readable detail.
    """

    def __init__(
        self,
        source_id: str,
        code: str,
        message: str,
        *,
        operation_id: str | None = None,
    ) -> None:
        self.operation_id = _validated_operation_id(operation_id)
        self.source_id = _safe_source_id(source_id)
        self.code = _safe_code(code)
        # The caller-supplied ``message`` is intentionally discarded: only the
        # constant generic message for ``code`` is retained, so no
        # driver-derived detail can ever surface in diagnostics.
        self.message = _message_for_code(self.code)
        super().__init__(self.message)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"operation_id={self.operation_id!r}, "
            f"source_id={self.source_id!r}, code={self.code!r}, "
            f"message={self.message!r})"
        )


class SourceDependencyError(SourceError):
    """A source dependency (profile or arrow provider) could not be resolved."""


class SourceProfileError(SourceDependencyError):
    """A named runtime profile could not be resolved for a source."""


class SourceConnectionError(SourceError):
    """A source connection could not be established or is unhealthy."""


class SourceSchemaError(SourceError):
    """A source schema could not be inspected or no longer matches."""


class SourceReloadError(SourceError):
    """A source reload failed."""
