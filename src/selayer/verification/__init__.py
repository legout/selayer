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
from .static import validate_catalog

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
]
