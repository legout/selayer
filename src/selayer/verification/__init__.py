"""Public interface for immutable verification report types."""

from __future__ import annotations

from selayer.model import SemanticLayer

from .audit import verify_physical
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
    "verify_physical",
    "verify_static",
]


def verify(layer: SemanticLayer, check: VerificationCheck) -> VerificationReport:
    """Run one verification ``check`` against ``layer`` and return its report.

    Dispatches on the exact check type: :class:`StaticCheck` runs
    :func:`verify_static`, :class:`CompatibilityCheck` runs
    :func:`verify_compatibility`, and :class:`PhysicalCheck` runs
    :func:`verify_physical`.  Any other object raises :class:`TypeError`.
    """
    if type(check) is StaticCheck:
        return verify_static(layer)
    if type(check) is CompatibilityCheck:
        return verify_compatibility(layer, check)
    if type(check) is PhysicalCheck:
        return verify_physical(layer, check)
    raise TypeError("unsupported verification check")
