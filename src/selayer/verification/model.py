# src/selayer/verification/model.py
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from selayer.model import SemanticLayer
from selayer.planning.types import QueryRequest
from selayer.sources.profiles import ArrowProviderResolver, RuntimeProfileResolver

Severity = Literal["error", "warning", "info"]
OutcomeStatus = Literal["passed", "failed", "skipped", "unavailable"]
EvidenceScope = Literal["declaration", "full_scan", "planner"]
CheckKind = Literal["static", "physical", "compatibility", "okf"]
EvidenceValue = bool | int | float | str | None

#: Runtime schema version produced and accepted by ``VerificationReport``.
_SCHEMA_VERSION = 1


def _canonical_sort_key(value: Any) -> tuple[Any, ...]:
    """Total, deterministic sort key for unordered-evidence members.

    ``set``/``frozenset`` members are only *partially* ordered (the ``<``
    operator is subset containment), so ``sorted(values)`` never raises but
    silently yields a result that depends on the iteration order of the set,
    which is PYTHONHASHSEED-dependent whenever members hash on ``str``/
    ``bytes``. Falling back to ``repr`` is no better, since the ``repr`` of a
    set or frozenset is itself hash-seed dependent.

    Instead this returns a rank-tagged tuple per value so comparison is always
    total: the leading element is an ``int`` rank in every branch, so two keys
    only ever compare element-wise within the *same* rank (hence the same
    value kind) and never raise ``TypeError``. The key is recursive, so the
    same logical value maps to the same key regardless of hash seed.
    """
    if value is None:
        return (0,)
    # ``bool`` is a subclass of ``int``: test it first so True/False do not
    # fall through to the integer branch.
    if isinstance(value, bool):
        return (1, value)
    if isinstance(value, int):
        return (2, value)
    if isinstance(value, float):
        return (3, value)
    if isinstance(value, str):
        return (4, value)
    if isinstance(value, (list, tuple)):
        return (5, tuple(_canonical_sort_key(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return (6, tuple(sorted(_canonical_sort_key(item) for item in value)))
    # Any other (necessarily hashable, non-collection) scalar: deterministic
    # last resort. This never receives a set/frozenset, so its ``repr`` is not
    # hash-seed dependent the way a collection repr is.
    return (7, type(value).__qualname__, repr(value))


def _freeze_evidence(value: object) -> object:
    """Recursively freeze evidence into immutable, deterministic structures.

    Mappings become ``MappingProxyType`` whose keys are reordered by
    :func:`_canonical_sort_key`, so serialised output is hash-seed independent
    even when the source mapping was built from an unordered input; mapping
    values are preserved and recursively frozen. Ordered sequences
    (``list``/``tuple``) become ``tuple`` while preserving the caller's order
    exactly — they are *not* reordered. Unordered collections
    (``set``/``frozenset``) become a ``tuple`` whose members are ordered by
    :func:`_canonical_sort_key`, so the result is deterministic and hash-seed
    independent. Scalars are returned unchanged. Non-finite floats are rejected
    up front by :func:`_validate_evidence_json_safe`.
    """
    if isinstance(value, Mapping):
        ordered_items = sorted(
            value.items(), key=lambda kv: _canonical_sort_key(kv[0])
        )
        return MappingProxyType(
            {key: _freeze_evidence(item) for key, item in ordered_items}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_evidence(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(
            _freeze_evidence(item)
            for item in sorted(value, key=_canonical_sort_key)
        )
    return value


def _evidence_to_plain(value: object) -> object:
    """Convert frozen evidence back into plain ``dict``/``list`` trees.

    Frozen sets are already sorted ``tuple`` (see :func:`_freeze_evidence`), so
    the restored ``list`` ordering stays deterministic and JSON-serialisable.
    """
    if isinstance(value, Mapping):
        return {key: _evidence_to_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_evidence_to_plain(item) for item in value]
    return value


def _validate_evidence_json_safe(value: object) -> None:
    """Reject accepted evidence values that strict JSON cannot emit.

    ``json.dumps(..., allow_nan=False)`` raises on ``nan``/``inf``/``-inf``, so
    a non-finite float is rejected here, at construction time. Every other
    accepted scalar (``bool``/``int``/finite ``float``/``str``/``None``) is
    strict-JSON-safe and is retained. Collections are validated recursively,
    including mapping keys.
    """
    # ``bool`` is a subclass of ``int``; this single check covers
    # ``None``/``bool``/``int``/``str``.
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"evidence contains non-finite float: {value!r}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_evidence_json_safe(key)
            _validate_evidence_json_safe(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _validate_evidence_json_safe(item)
        return


def _validate_schema_version(schema_version: object) -> None:
    """Enforce the exact runtime schema version.

    The ``Literal[1]`` annotation only constrains statically typed callers;
    this guard additionally rejects numeric look-alikes (``True`` as ``bool``,
    ``1.0`` as ``float``) by requiring an exact ``int`` type and the value ``1``.
    """
    if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be {_SCHEMA_VERSION} (int), "
            f"got {schema_version!r}"
        )


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
        _validate_evidence_json_safe(self.evidence)
        object.__setattr__(self, "evidence", _freeze_evidence(self.evidence))
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
        _validate_schema_version(self.schema_version)
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
                    "evidence": _evidence_to_plain(item.evidence),
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
