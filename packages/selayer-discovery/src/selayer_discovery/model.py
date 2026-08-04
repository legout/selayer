"""Shared artifact model: enums, schema version, validators, and base type.

This module is intentionally behavior-light. It owns the closed-set enums, the
artifact schema-version constant, the bounded text/collection validators, the
normalized named-approver identity helper, and the thin versioned
:class:`Artifact` base type that every machine-readable discovery record
subclasses.

Behavior-heavy session, evidence, proposal, approval, and apply logic lives in
later modules. ``model.py`` only defines the shared vocabulary and the
size/shape guards that keep artifacts deterministic and safe to hash. The
canonicalization bounds (:data:`MAX_COLLECTION_ITEMS`, :data:`MAX_NESTING_DEPTH`)
live here so there is one source of truth for artifact limits shared by the
validators and :mod:`selayer_discovery.canonical`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NewType

from selayer_discovery.diagnostics import DiscoveryError

__all__ = [
    "MAX_COLLECTION_ITEMS",
    "MAX_NESTING_DEPTH",
    "MAX_TEXT_LENGTH",
    "SCHEMA_VERSION",
    "Artifact",
    "ArtifactId",
    "EvidenceClass",
    "GateDisposition",
    "GroupStatus",
    "bounded_mapping",
    "bounded_sequence",
    "bounded_text",
    "normalize_actor_identity",
    "validate_artifact_id",
]

#: Canonical artifact schema version produced by this package.
SCHEMA_VERSION: Final[int] = 1

#: Maximum length (in characters) of a single bounded text field.
MAX_TEXT_LENGTH: Final[int] = 16 * 1024

#: Maximum number of items in a single artifact collection (list or mapping).
MAX_COLLECTION_ITEMS: Final[int] = 100_000

#: Maximum container nesting depth accepted by the canonicalizer.
MAX_NESTING_DEPTH: Final[int] = 64

#: Nominal type for bounded, machine-generated artifact identifiers.
ArtifactId = NewType("ArtifactId", str)
_ARTIFACT_ID_RE: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")


def validate_artifact_id(value: object) -> ArtifactId:
    """Validate and return a bounded deterministic artifact identifier."""
    if type(value) is not str or _ARTIFACT_ID_RE.match(value) is None:
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="artifact id is invalid",
        )
    return ArtifactId(value)


class EvidenceClass(StrEnum):
    """How a claim's evidence was produced.

    ``OBSERVED`` comes from deterministic schema, profile, audit, or planner
    results; ``ASSERTED`` comes from people, documents, external knowledge, or
    existing catalog intent; ``INFERRED`` is an agent hypothesis that never
    satisfies a proposal's evidence requirement by itself.
    """

    OBSERVED = "observed"
    ASSERTED = "asserted"
    INFERRED = "inferred"


class GroupStatus(StrEnum):
    """Lifecycle state of an atomic dependency group."""

    DRAFT = "draft"
    BLOCKED = "blocked"
    READY = "ready"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    STALE = "stale"
    APPLIED = "applied"


class GateDisposition(StrEnum):
    """Terminal disposition of an adaptive interview gate."""

    ANSWERED = "answered"
    NOT_APPLICABLE = "not_applicable"
    BLOCKED = "blocked"


@dataclass(frozen=True, kw_only=True)
class Artifact:
    """Base type for versioned discovery artifacts.

    Every machine-readable artifact carries a bounded artifact ID and the
    current schema version so a canonical fingerprint is self-describing.
    """

    artifact_id: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_artifact_id(self.artifact_id)
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise DiscoveryError(
                "discovery.artifact.invalid",
                safe_detail="artifact schema version is invalid",
            )


_ACTOR_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


def normalize_actor_identity(value: object) -> str:
    """Return a deterministic normalized named-approver identity.

    The identity is NFC-normalized, stripped, and has internal runs of
    whitespace collapsed to a single ASCII space. A blank or non-string
    identity is rejected with :class:`~selayer_discovery.diagnostics.DiscoveryError`.

    Normalization is workflow enforcement, not authentication: a local user can
    still type another person's name. Repository review and merge permissions
    remain the final organizational control.
    """

    if type(value) is not str:
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="actor identity must be text",
        )
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = _ACTOR_WHITESPACE.sub(" ", normalized)
    if not normalized:
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="actor identity must not be blank",
        )
    return normalized


def bounded_text(value: object, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    """Return ``value`` as a plain string if it is within the text bound.

    Rejects non-strings (including ``str`` subclasses) and strings whose length
    exceeds ``max_length``.
    """

    if type(value) is not str:
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="value must be text",
        )
    if len(value) > max_length:
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="value exceeds the maximum text length",
        )
    return value


def bounded_sequence(
    value: object,
    *,
    max_items: int = MAX_COLLECTION_ITEMS,
) -> list[object]:
    """Return ``value`` as a list if it is a bounded, non-string sequence.

    Strings, bytes, and mappings are rejected (they are not item collections in
    this sense); so are sets and any non-:class:`collections.abc.Sequence`.
    """

    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="value must be a sequence",
        )
    if not isinstance(value, Sequence):
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="value must be a sequence",
        )
    if len(value) > max_items:
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="sequence exceeds the maximum item count",
        )
    return list(value)


def bounded_mapping(
    value: object,
    *,
    max_items: int = MAX_COLLECTION_ITEMS,
) -> dict[str, object]:
    """Return ``value`` as a dict with string keys if it is a bounded mapping.

    Non-mappings and mappings with non-string keys are rejected.
    """

    if not isinstance(value, Mapping):
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="value must be a mapping",
        )
    if len(value) > max_items:
        raise DiscoveryError(
            "discovery.artifact.invalid",
            safe_detail="mapping exceeds the maximum item count",
        )
    result: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise DiscoveryError(
                "discovery.artifact.invalid",
                safe_detail="mapping keys must be text",
            )
        result[key] = item
    return result
