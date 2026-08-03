"""Static catalog validation adapter for the verification report model.

:func:`validate_catalog` adapts the existing schema-version-1 catalog loader
(:func:`selayer.catalog.load`) into the immutable verification report model.
A successful load returns the parsed layer alongside a passed ``catalog.static``
outcome; a catalog-domain failure (any :class:`CatalogValidationError` raised by
the loader, including malformed YAML and structural issues that the loader maps
to that error) is converted into a failed outcome whose diagnostics carry the
catalog's stable issue codes.

The adapter deliberately catches only catalog-domain errors: programmer errors
(such as ``AssertionError`` from the loader's unreachable branches) and
non-catalog I/O errors propagate unchanged so they are not silently swallowed.
"""

from __future__ import annotations

from pathlib import Path

from selayer.catalog import CatalogValidationError
from selayer.model import SemanticLayer
from selayer.verification.model import (
    CatalogValidationResult,
    VerificationDiagnostic,
    VerificationOutcome,
    VerificationReport,
)

#: Check identifier emitted by every catalog static outcome.
_CHECK_ID = "catalog.static"


def validate_catalog(path: str | Path) -> CatalogValidationResult:
    """Validate a catalog at ``path`` and return a coded verification report.

    Returns a :class:`CatalogValidationResult` whose ``layer`` is the loaded
    :class:`SemanticLayer` (or ``None`` when validation failed) and whose
    ``report`` is an immutable static verification report. Catalog-domain
    failures raised by the loader become a single failed ``catalog.static``
    outcome with one diagnostic per catalog issue.
    """
    subject = str(Path(path))
    try:
        layer = SemanticLayer.load(path)
    except CatalogValidationError as error:
        diagnostics = tuple(
            VerificationDiagnostic(issue.code, "error", issue.path, issue.message)
            for issue in error.issues
        )
        outcome = VerificationOutcome(
            _CHECK_ID, "failed", "declaration", "catalog", {}, diagnostics
        )
        return CatalogValidationResult(
            None,
            VerificationReport(1, subject, "static", True, (outcome,), diagnostics),
        )
    outcome = VerificationOutcome(
        _CHECK_ID, "passed", "declaration", "catalog", {}, ()
    )
    return CatalogValidationResult(
        layer,
        VerificationReport(1, layer.name, "static", True, (outcome,), ()),
    )


__all__ = ["validate_catalog"]
