from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

Severity = Literal["error", "warning"]
TrustTier = Literal["unverified", "machine_confirmed", "human_reviewed"]
Freshness = Literal["current", "stale", "unspecified"]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class OkfIssue:
    path: str
    message: str
    severity: Severity = "error"


class OkfValidationError(ValueError):
    def __init__(self, issues: tuple[OkfIssue, ...]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in issues))


@dataclass(frozen=True, slots=True)
class OkfSection:
    title: str
    content: str


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
    "Freshness",
    "OkfConcept",
    "OkfIssue",
    "OkfSection",
    "OkfValidationError",
    "Severity",
    "TrustTier",
]
