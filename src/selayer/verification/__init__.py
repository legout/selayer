"""Public interface for immutable verification report types."""

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

__all__ = [
    "CatalogValidationResult",
    "CompatibilityCheck",
    "PhysicalCheck",
    "StaticCheck",
    "VerificationCheck",
    "VerificationDiagnostic",
    "VerificationOutcome",
    "VerificationReport",
]
