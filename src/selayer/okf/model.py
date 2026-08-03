from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

Severity = Literal["error", "warning"]
TrustTier = Literal["unverified", "machine_confirmed", "human_reviewed"]
Freshness = Literal["current", "stale", "unspecified"]


class OkfMetadataError(ValueError):
    pass


def _freeze(value: Any, identity_stack: set[int] | None = None) -> Any:
    if not isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return value
    if identity_stack is None:
        identity_stack = set()
    identity = id(value)
    if identity in identity_stack:
        raise OkfMetadataError("cyclic metadata")
    identity_stack.add(identity)
    try:
        if isinstance(value, Mapping):
            return MappingProxyType(
                {key: _freeze(item, identity_stack) for key, item in value.items()}
            )
        if isinstance(value, (list, tuple)):
            return tuple(_freeze(item, identity_stack) for item in value)
        return frozenset(_freeze(item, identity_stack) for item in value)
    finally:
        identity_stack.remove(identity)


@dataclass(frozen=True, slots=True)
class OkfIssue:
    path: str
    message: str
    severity: Severity = "error"
    code: str = "okf.invalid"


class OkfValidationError(ValueError):
    def __init__(self, issues: tuple[OkfIssue, ...]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in issues))


class ContextLookupError(LookupError):
    pass


class ContextBudgetError(ValueError):
    def __init__(self, required_chars: int, max_chars: int) -> None:
        self.required_chars = required_chars
        self.max_chars = max_chars
        super().__init__(
            f"mandatory context requires {required_chars} characters; "
            f"budget is {max_chars}"
        )


@dataclass(frozen=True, slots=True)
class ContextItem:
    concept_id: str
    kind: str
    content: str
    provider: str
    semantic_refs: tuple[str, ...]
    trust: TrustTier
    freshness: Freshness
    sources: tuple[str, ...]
    attested_computation: AttestedComputation | None = None


@dataclass(frozen=True, slots=True)
class ContextResult:
    items: tuple[ContextItem, ...]
    diagnostics: tuple[OkfIssue, ...]
    total_chars: int


@dataclass(frozen=True, slots=True)
class SyncReport:
    written: tuple[str, ...]
    unchanged: tuple[str, ...]
    conflicts: tuple[str, ...]
    orphaned: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OkfSection:
    title: str
    content: str


@dataclass(frozen=True, slots=True)
class OkfParameter:
    name: str
    type: str
    required: bool


@dataclass(frozen=True, slots=True)
class AttestedComputation:
    runtime: str
    parameters: tuple[OkfParameter, ...]
    computation_path: str | None
    computation_body: str
    executor_resource: str | None
    executor_receipt: tuple[str, ...]
    attester_resource: str | None


@dataclass(frozen=True, slots=True)
class OkfConcept:
    concept_id: str
    relative_path: PurePosixPath
    frontmatter: Mapping[str, Any]
    preamble: str
    sections: tuple[OkfSection, ...]
    links: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        concept_id: str,
        relative_path: PurePosixPath,
        frontmatter: Mapping[str, Any],
        preamble: str = "",
        sections: tuple[OkfSection, ...] = (),
        links: tuple[str, ...] = (),
    ) -> OkfConcept:
        return cls(
            concept_id=concept_id,
            relative_path=relative_path,
            frontmatter=_freeze(frontmatter),
            preamble=preamble,
            sections=sections,
            links=links,
        )


__all__ = [
    "AttestedComputation",
    "ContextBudgetError",
    "ContextItem",
    "ContextLookupError",
    "ContextResult",
    "Freshness",
    "OkfConcept",
    "OkfIssue",
    "OkfMetadataError",
    "OkfParameter",
    "OkfSection",
    "OkfValidationError",
    "Severity",
    "SyncReport",
    "TrustTier",
]
