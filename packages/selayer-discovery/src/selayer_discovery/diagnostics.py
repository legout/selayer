"""Safe diagnostic exceptions and rendering for discovery operations.

Every discovery failure surfaces as a :class:`DiscoveryError` that renders
only a stable allowlisted ``code``, a constant generic message, optional safe
descriptive text, and validated safe identifiers. Raw causes, secrets, DSNs,
tokens, source rows, and evidence bodies are accepted only as an opaque
``context`` object stored privately and *never* rendered.

This mirrors the secrecy discipline of :mod:`selayer.sources.errors`:

* the caller-supplied ``code`` is validated against a known-code allowlist and
  coerced to a fallback otherwise (and only an *exact* builtin ``str`` is
  accepted, so a hostile ``str`` subclass cannot smuggle a leaky ``__repr__``);
* ``safe_ids`` are validated against a stable-identifier shape and coerced to a
  placeholder otherwise;
* ``safe_detail`` is accepted only as an *exact* builtin ``str`` and is
  length-bounded;
* :meth:`DiscoveryError.__str__`, :meth:`DiscoveryError.__repr__`,
  :meth:`DiscoveryError.to_dict`, and :func:`format_diagnostic` never reference
  the private context, so no secret-bearing cause can surface in diagnostic
  output, JSON, stdout, or stderr.

Errors are constructed and raised *outside* active ``except`` scopes so
``__cause__`` and ``__context__`` remain ``None``; the raw cause, when needed
for internal logging, is passed explicitly via ``context=`` and never chained.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

__all__ = [
    "DiscoveryError",
    "UnsupportedArtifactError",
    "format_diagnostic",
]

_FALLBACK_CODE: Final[str] = "discovery.internal"
_FALLBACK_MESSAGE: Final[str] = "a discovery error occurred"

# Constant, generic messages keyed by stable error code. Only these constant
# strings are ever stored as ``message``; the caller may not override them, so
# no caller- or driver-derived detail can surface via ``message``.
_CODE_MESSAGES: Final[dict[str, str]] = {
    "discovery.artifact.invalid": "the artifact failed validation",
    "discovery.canonical.unsupported": "the artifact contains an unsupported value",
    "discovery.canonical.too_deep": "the artifact exceeds the maximum nesting depth",
    "discovery.canonical.too_many_items": (
        "the artifact exceeds the maximum collection size"
    ),
    _FALLBACK_CODE: _FALLBACK_MESSAGE,
}

# Known discovery error codes — an allowlist, not a permissive regex. Only a
# code in this set is retained; anything else (e.g. a SQL fragment or a secret
# smuggled into a code string) is coerced to the fallback so arbitrary caller
# or driver text can never surface as a code.
_KNOWN_CODES: Final[frozenset[str]] = frozenset(_CODE_MESSAGES)

# Stable identifier shape for safe ids: lowercase letter first, then lowercase
# letters, digits, underscores, dots, or hyphens. This admits catalog-shaped
# names, qualified semantic ids (``dimension.foo``), session/group slugs, and
# lowercase UUIDs, while rejecting credential URIs, free text, and secrets.
_SAFE_ID_RE: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_.-]*\Z")
_SAFE_ID_PLACEHOLDER: Final[str] = "<id>"
_SAFE_DETAIL_MAX_LENGTH: Final[int] = 256


def _safe_code(code: object) -> str:
    """Return ``code`` only if it is a known error code, else the fallback.

    Only an *exact* builtin ``str`` is accepted (``type(code) is str``), not
    ``isinstance``: a hostile ``str`` subclass passes ``isinstance(str)`` yet
    can carry a custom ``__repr__`` that leaks a secret when rendered.
    """

    return code if type(code) is str and code in _KNOWN_CODES else _FALLBACK_CODE


def _safe_detail(detail: object) -> str | None:
    """Return ``detail`` only if it is a bounded plain string, else ``None``."""

    if type(detail) is not str:
        return None
    if len(detail) > _SAFE_DETAIL_MAX_LENGTH:
        return None
    return detail


def _safe_ids(ids: object) -> tuple[str, ...]:
    """Return validated stable identifiers, coercing unsafe values to a placeholder.

    A bare string is treated as no ids (a string is not an id collection); any
    other non-iterable is likewise empty. Each id is retained only when it is
    an exact builtin ``str`` matching the stable-identifier shape.
    """

    if isinstance(ids, str):
        return ()
    if not isinstance(ids, Iterable):
        return ()
    result: list[str] = []
    for item in ids:
        result.append(
            item
            if type(item) is str and _SAFE_ID_RE.match(item) is not None
            else _SAFE_ID_PLACEHOLDER
        )
    return tuple(result)


def _message_for_code(code: str) -> str:
    """Return the constant generic message for ``code`` (fallback if unknown)."""

    return _CODE_MESSAGES.get(code, _FALLBACK_MESSAGE)


class DiscoveryError(Exception):
    """Sanitized discovery diagnostic exception.

    Only safe, derived identifiers are rendered:

    * ``code`` — a stable, allowlisted symbolic code.
    * ``message`` — a constant generic message looked up from ``code``.
    * ``safe_detail`` — optional bounded safe descriptive text.
    * ``safe_ids`` — validated stable identifiers.

    The optional ``context`` is stored privately (``_context``) and is never
    rendered by ``__str__``, ``__repr__``, ``to_dict`` or
    :func:`format_diagnostic`. It may carry secrets, DSNs, source rows, or
    evidence bodies for internal use only.

    Attributes:
        code: stable, allowlisted symbolic error code.
        message: constant, sanitized human-readable detail.
        safe_detail: optional bounded safe descriptive text, or ``None``.
        safe_ids: tuple of validated stable identifiers.
    """

    def __init__(
        self,
        code: object,
        *,
        safe_detail: object = None,
        safe_ids: object = (),
        context: object = None,
    ) -> None:
        self.code = _safe_code(code)
        self.message = _message_for_code(self.code)
        self.safe_detail = _safe_detail(safe_detail)
        self.safe_ids = _safe_ids(safe_ids)
        # Stored privately and never rendered. It may carry secrets, DSNs,
        # source rows, or evidence bodies for internal logging only.
        self._context: object = context
        super().__init__(self.message)

    def __str__(self) -> str:
        parts: list[str] = [self.code]
        if self.safe_detail is not None:
            parts.append(self.safe_detail)
        if self.safe_ids:
            parts.append("ids=" + ",".join(self.safe_ids))
        return " ".join(parts)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"message={self.message!r}, "
            f"safe_detail={self.safe_detail!r}, safe_ids={self.safe_ids!r})"
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping containing only safe fields."""

        result: dict[str, object] = {
            "code": self.code,
            "message": self.message,
        }
        if self.safe_detail is not None:
            result["safe_detail"] = self.safe_detail
        if self.safe_ids:
            result["safe_ids"] = list(self.safe_ids)
        return result


class UnsupportedArtifactError(DiscoveryError):
    """Raised when a value cannot be canonicalized into a semantic payload."""


def format_diagnostic(error: DiscoveryError) -> str:
    """Render a single-line safe diagnostic suitable for stdout or stderr."""

    return str(error)
