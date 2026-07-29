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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

import pyarrow as pa
import pyarrow.dataset as padataset

from selayer.sources.errors import SourceProfileError

__all__ = [
    "ArrowObject",
    "ArrowProviderResolver",
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
            # remain None.  ``name`` originates from a validated connector
            # profile field (``[a-z][a-z0-9_]*``) and ``source_id`` from a
            # validated source name, so neither can carry credentials.
            raise SourceProfileError(
                source_id,
                "missing_profile",
                f"runtime profile {name!r} is not configured for source {source_id!r}",
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
