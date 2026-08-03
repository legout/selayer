# src/selayer/verification/model.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from selayer.model import SemanticLayer
from selayer.planning.types import QueryRequest
from selayer.sources.profiles import ArrowProviderResolver, RuntimeProfileResolver

Severity = Literal["error", "warning", "info"]
OutcomeStatus = Literal["passed", "failed", "skipped", "unavailable"]
EvidenceScope = Literal["declaration", "full_scan", "planner"]
CheckKind = Literal["static", "physical", "compatibility", "okf"]
EvidenceValue = bool | int | float | str | None


@dataclass(frozen=True, slots=True)
class StaticCheck:
    pass


@dataclass(frozen=True, slots=True)
class PhysicalCheck:
    profiles: RuntimeProfileResolver | None = field(default=None, repr=False)
    arrow_providers: ArrowProviderResolver | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CompatibilityCheck:
    metrics: tuple[str, ...] | None = None
    dimensions: tuple[str, ...] | None = None
    query_cases: tuple[QueryRequest, ...] = ()

    def __post_init__(self) -> None:
        if self.metrics is not None:
            object.__setattr__(self, "metrics", tuple(self.metrics))
        if self.dimensions is not None:
            object.__setattr__(self, "dimensions", tuple(self.dimensions))
        object.__setattr__(self, "query_cases", tuple(self.query_cases))


VerificationCheck = StaticCheck | PhysicalCheck | CompatibilityCheck


@dataclass(frozen=True, slots=True, order=True)
class VerificationDiagnostic:
    code: str
    severity: Severity
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    check_id: str
    status: OutcomeStatus
    scope: EvidenceScope
    path: str
    evidence: Mapping[str, EvidenceValue]
    diagnostics: tuple[VerificationDiagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(self, "diagnostics", tuple(sorted(self.diagnostics)))


@dataclass(frozen=True, slots=True)
class VerificationReport:
    schema_version: Literal[1]
    subject: str
    check_kind: CheckKind
    complete: bool
    outcomes: tuple[VerificationOutcome, ...]
    diagnostics: tuple[VerificationDiagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "outcomes",
            tuple(sorted(self.outcomes, key=lambda item: (item.path, item.check_id))),
        )
        object.__setattr__(self, "diagnostics", tuple(sorted(self.diagnostics)))

    @property
    def passed(self) -> bool:
        return (
            self.complete
            and all(outcome.status == "passed" for outcome in self.outcomes)
            and not any(item.severity == "error" for item in self.diagnostics)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "subject": self.subject,
            "check_kind": self.check_kind,
            "complete": self.complete,
            "passed": self.passed,
            "outcomes": [
                {
                    "check_id": item.check_id,
                    "status": item.status,
                    "scope": item.scope,
                    "path": item.path,
                    "evidence": dict(item.evidence),
                    "diagnostics": [
                        {
                            "code": diagnostic.code,
                            "severity": diagnostic.severity,
                            "path": diagnostic.path,
                            "message": diagnostic.message,
                        }
                        for diagnostic in item.diagnostics
                    ],
                }
                for item in self.outcomes
            ],
            "diagnostics": [
                {
                    "code": item.code,
                    "severity": item.severity,
                    "path": item.path,
                    "message": item.message,
                }
                for item in self.diagnostics
            ],
        }


@dataclass(frozen=True, slots=True)
class CatalogValidationResult:
    layer: SemanticLayer | None
    report: VerificationReport
