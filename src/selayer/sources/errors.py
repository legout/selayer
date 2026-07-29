"""Sanitized source lifecycle errors.

Every source lifecycle failure surfaces as a :class:`SourceError` subclass
that carries a UUIDv4 ``operation_id`` (generated at the lifecycle boundary),
the affected ``source_id``, a stable symbolic ``code``, and a constant
sanitized ``message``.

Two guarantees are load-bearing for the secrecy contract:

* **No retained driver exceptions.**  Driver exceptions are never stored.
  Errors must be constructed and raised *outside* active ``except`` scopes so
  ``__cause__`` and ``__context__`` remain ``None``; the only stored text is
  the constant ``message`` — never driver-derived strings that could carry
  credentials.
* **Safe reprs.**  The repr renders only ``operation_id``, ``source_id``,
  ``code``, and the constant ``message`` — all of which are safe identifiers or
  constant text, never driver material.
"""

from __future__ import annotations

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


class SourceError(Exception):
    """Base class for sanitized source lifecycle errors.

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
        self.operation_id = (
            operation_id if operation_id is not None else new_operation_id()
        )
        self.source_id = source_id
        self.code = code
        self.message = message
        super().__init__(message)

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
