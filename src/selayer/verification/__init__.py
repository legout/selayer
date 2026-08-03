"""Public interface for immutable verification report types."""

from __future__ import annotations

from selayer.model import SemanticLayer

from .compatibility import verify_compatibility
from .model import (
    CatalogValidationResult,
    CompatibilityCheck,
    PhysicalCheck,
    StaticCheck,
    VerificationCheck,
    VerificationDiagnostic,
    VerificationOutcome,
    VerificationReport,
)
from .static import validate_catalog, verify_static

__all__ = [
    "CatalogValidationResult",
    "CompatibilityCheck",
    "PhysicalCheck",
    "StaticCheck",
    "VerificationCheck",
    "VerificationDiagnostic",
    "VerificationOutcome",
    "VerificationReport",
    "validate_catalog",
    "verify",
    "verify_compatibility",
    "verify_static",
]


def verify(layer: SemanticLayer, check: VerificationCheck) -> VerificationReport:
    """Run one verification ``check`` against ``layer`` and return its report.

    Dispatches on the exact check type: :class:`StaticCheck` runs
    :func:`verify_static`, and :class:`CompatibilityCheck` runs
    :func:`verify_compatibility`. Physical verification is added in a later
    task; any other object raises :class:`TypeError`.
    """
    if type(check) is StaticCheck:
        return verify_static(layer)
    if type(check) is CompatibilityCheck:
        return verify_compatibility(layer, check)
    raise TypeError("unsupported verification check")
