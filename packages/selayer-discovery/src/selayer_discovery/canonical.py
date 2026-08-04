"""Strict canonical JSON and SHA-256 fingerprints for discovery artifacts.

Canonicalization is the single source of truth for artifact fingerprints. It
produces deterministic, sorted, compact UTF-8 JSON so that the same semantic
content always yields the same fingerprint regardless of mapping insertion
order.

Strictness guarantees:

* mapping keys are sorted (order independence);
* list order is preserved (lists are semantic);
* :class:`enum.Enum` (including :class:`enum.StrEnum`) and dataclass instances
  are normalized to their JSON-native forms;
* :class:`datetime.date` values are rendered as ISO-8601 strings;
* :class:`datetime.datetime` and :class:`datetime.time` are *rejected* —
  wall-clock timestamps never enter a semantic fingerprint (callers that need a
  calendar date use :class:`datetime.date`);
* non-finite floats (NaN, +inf, -inf) are rejected;
* unsupported objects (sets, bytes, arbitrary classes, non-string mapping keys)
  are rejected rather than coerced through ``str()``;
* nesting depth and per-collection item counts are bounded by
  :data:`~selayer_discovery.model.MAX_NESTING_DEPTH` and
  :data:`~selayer_discovery.model.MAX_COLLECTION_ITEMS`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum

from selayer_discovery.diagnostics import UnsupportedArtifactError
from selayer_discovery.model import MAX_COLLECTION_ITEMS, MAX_NESTING_DEPTH

__all__ = [
    "NormalizedArtifact",
    "UnsupportedArtifactError",
    "canonical_bytes",
    "fingerprint",
    "normalize_artifact",
]


type NormalizedArtifact = (
    None | bool | int | float | str
    | list["NormalizedArtifact"]
    | dict[str, "NormalizedArtifact"]
)


def _unsupported(code: str) -> UnsupportedArtifactError:
    return UnsupportedArtifactError(code)


def _normalize(value: object, depth: int) -> NormalizedArtifact:
    if depth > MAX_NESTING_DEPTH:
        raise _unsupported("discovery.canonical.too_deep")
    if value is None:
        return None
    # bool is a subclass of int; check it first so True/False are preserved.
    if type(value) is bool:
        return value
    if isinstance(value, Enum):
        return _normalize(value.value, depth + 1)
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _unsupported("discovery.canonical.unsupported")
        return value
    if type(value) is str:
        return value
    if isinstance(value, (bytes, bytearray)):
        raise _unsupported("discovery.canonical.unsupported")
    # datetime is a subclass of date; reject timestamps before accepting dates.
    if isinstance(value, (datetime, time)):
        raise _unsupported("discovery.canonical.unsupported")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        result: dict[str, NormalizedArtifact] = {}
        for field in fields(value):
            result[field.name] = _normalize(getattr(value, field.name), depth + 1)
        if len(result) > MAX_COLLECTION_ITEMS:
            raise _unsupported("discovery.canonical.too_many_items")
        return result
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise _unsupported("discovery.canonical.too_many_items")
        out: dict[str, NormalizedArtifact] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _unsupported("discovery.canonical.unsupported")
            out[key] = _normalize(item, depth + 1)
        return out
    if isinstance(value, Sequence):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise _unsupported("discovery.canonical.too_many_items")
        return [_normalize(item, depth + 1) for item in value]
    raise _unsupported("discovery.canonical.unsupported")


def normalize_artifact(value: object) -> NormalizedArtifact:
    """Return the JSON-native canonical form of ``value``.

    Raises :class:`UnsupportedArtifactError` for any value that cannot be made
    deterministic (non-finite floats, timestamps, sets, bytes, non-string
    mapping keys, arbitrary objects) or that exceeds the nesting or item bounds.
    """

    return _normalize(value, 0)


def canonical_bytes(value: object) -> bytes:
    """Return deterministic compact UTF-8 JSON bytes for ``value``.

    Keys are sorted, separators are minimal, non-ASCII is preserved as UTF-8,
    and ``NaN``/``Infinity`` tokens are refused (they are pre-rejected by
    :func:`normalize_artifact`, and ``allow_nan=False`` guards the boundary).
    """

    normalized = normalize_artifact(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def fingerprint(value: object) -> str:
    """Return the SHA-256 hex digest of the canonical form of ``value``."""

    return hashlib.sha256(canonical_bytes(value)).hexdigest()
