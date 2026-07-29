"""Opaque runtime profiles and arrow-provider resolution.

A :class:`RuntimeProfile` is an opaque, defensively-copied bag of named values
(credential profiles, region tokens, ...) addressed by a stable profile name.
Values are never exposed in bulk: the only accessor is :meth:`value`, which
returns a single value for internal adapter use.  The stored mapping is a
``MappingProxyType`` over a private copy, so mutations to the caller's original
mapping after construction can never reach the profile, and the mapping is
excluded from the repr (``repr=False``) so secret values never surface.

Resolvers turn stable names into profiles (or arrow-object provider factories):

* :class:`MappingProfileResolver` — concrete resolver backed by a defensively
  copied profile map; raises :class:`SourceProfileError` for unknown names.
* :class:`RuntimeProfileResolver` — the structural protocol every profile
  resolver satisfies.
* :class:`ArrowProviderResolver` — structural protocol mapping a handle name to
  a zero-argument provider factory.  Providers — not one-time objects — are the
  reloadable unit: each call to the returned factory yields a fresh
  :data:`ArrowObject`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

import pyarrow as pa
import pyarrow.dataset as padataset

from selayer.sources.errors import SourceDependencyError, SourceProfileError

__all__ = [
    "ArrowObject",
    "ArrowProviderResolver",
    "MappingArrowProviderResolver",
    "MappingProfileResolver",
    "RuntimeProfile",
    "RuntimeProfileResolver",
]


# The supported union of reloadable Arrow objects.  Adapters resolve a *provider*
# (a zero-argument factory) returning one of these rather than a one-time
# object, so the underlying relation can be re-opened on reload.
type ArrowObject = (
    padataset.Dataset | padataset.Scanner | pa.Table | pa.RecordBatchReader
)


# Catalog source-name shape — the *exact* convention the catalog enforces for
# declared source names (lowercase snake_case).  Only a profile name that
# matches this shape renders; anything else (a hostile ``str`` subclass whose
# own ``__repr__`` leaks a secret, a non-string, a credential-bearing name) is
# redacted to ``"<redacted>"`` in the repr.
_PROFILE_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]*\Z")


def _safe_profile_name(name: object) -> str:
    """Render a profile name, redacting non-conformant / hostile values.

    Only an *exact* builtin ``str`` that matches the catalog source-name shape
    is rendered; a hostile ``str`` subclass (whose custom ``__repr__" could
    leak a secret when rendered), a non-string, or a non-conformant name is
    redacted to ``"<redacted>"``.  ``type(name) is str`` is used rather than
    ``isinstance`` so a subclass cannot satisfy the guard — this is a *local*
    sanitizer (no import from ``config``/``base``) to avoid any import cycle.
    """

    return name if type(name) is str and _PROFILE_NAME_RE.match(name) else "<redacted>"


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """Opaque, defensively-copied named value bag.

    The caller's mapping is snapshotted into a private ``MappingProxyType`` and
    excluded from the repr, so:

    * mutating the original mapping after construction has no effect, and
    * no secret value surfaces in diagnostics.

    The entire mapping is never exposed; :meth:`value` returns a single named
    value for internal adapter use.
    """

    name: str
    _values: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        # Defensive copy: snapshot the caller's mapping into a private dict,
        # then expose it immutably through MappingProxyType.  Subsequent
        # mutations to the original mapping cannot reach the profile.
        object.__setattr__(self, "_values", MappingProxyType(dict(self._values)))

    def value(self, name: str) -> object:
        """Return a single named value for internal adapter use."""
        return self._values[name]

    def __repr__(self) -> str:
        # Only the safe profile name renders; the ``_values`` mapping is never
        # exposed (it carries credential values).  The name is routed through a
        # conservative exact-builtin-str helper so a hostile ``str`` subclass
        # whose own ``__repr__" leaks a secret can never be rendered.
        return f"RuntimeProfile(name={_safe_profile_name(self.name)!r})"


class MappingProfileResolver:
    """Concrete resolver over a defensively-copied profile map.

    Each entry is snapshotted into an immutable :class:`RuntimeProfile` and the
    outer map is exposed immutably, so mutations to the caller's map after
    construction cannot reach the resolver.  Unknown names raise a sanitized
    :class:`SourceProfileError` outside any ``except`` scope.
    """

    __slots__ = ("_profiles",)

    def __init__(self, profiles: Mapping[str, Mapping[str, object]]) -> None:
        copied: dict[str, RuntimeProfile] = {
            name: RuntimeProfile(name, values) for name, values in profiles.items()
        }
        self._profiles: Mapping[str, RuntimeProfile] = MappingProxyType(copied)

    def resolve(self, name: str, *, source_id: str) -> RuntimeProfile:
        if name not in self._profiles:
            # Raised outside any ``except`` scope so __cause__ and __context__
            # remain None.  No untrusted name is interpolated into the error:
            # SourceError discards the caller-supplied message and stores only
            # the constant generic text for ``"missing_profile"``; ``source_id``
            # is validated (else coerced to ``"<source>"``) by SourceError.
            raise SourceProfileError(
                source_id,
                "missing_profile",
                "runtime profile is not configured",
            )
        return self._profiles[name]


@runtime_checkable
class RuntimeProfileResolver(Protocol):
    """Structural protocol: map a profile name to a :class:`RuntimeProfile`."""

    def resolve(self, name: str, *, source_id: str) -> RuntimeProfile: ...


@runtime_checkable
class ArrowProviderResolver(Protocol):
    """Structural protocol: map a handle name to a fresh-object provider.

    The returned factory yields a new :data:`ArrowObject` on each call;
    providers, not one-time objects, are the reloadable unit.
    """

    def resolve(self, handle: str, *, source_id: str) -> Callable[[], ArrowObject]: ...


class MappingArrowProviderResolver:
    """Concrete resolver over a defensively-copied arrow-provider map.

    Each handle maps to a zero-argument provider factory that yields a fresh
    :data:`ArrowObject`.  The input map is defensively copied into an
    immutable ``MappingProxyType`` so mutations to the caller's map after
    construction cannot reach the resolver.  Unknown handles raise a sanitized
    :class:`SourceDependencyError` (code ``"missing_arrow_provider"``)
    outside any ``except`` scope.
    """

    __slots__ = ("_providers",)

    def __init__(self, providers: Mapping[str, Callable[[], ArrowObject]]) -> None:
        self._providers: Mapping[str, Callable[[], ArrowObject]] = MappingProxyType(
            dict(providers)
        )

    def resolve(self, handle: str, *, source_id: str) -> Callable[[], ArrowObject]:
        if handle not in self._providers:
            # Raised outside any ``except`` scope so __cause__ and __context__
            # remain None.  No untrusted handle name is interpolated: SourceError
            # discards the caller-supplied message and stores only the constant
            # generic text for ``"missing_arrow_provider"``.
            raise SourceDependencyError(
                source_id,
                "missing_arrow_provider",
                "arrow provider handle is not configured",
            )
        return self._providers[handle]
