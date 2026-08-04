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

from collections.abc import Mapping
from pathlib import Path

from selayer.catalog import CatalogValidationError, collect_model_issues
from selayer.model import SemanticLayer, SemanticObject, SemanticStatus
from selayer.verification.model import (
    CatalogValidationResult,
    VerificationDiagnostic,
    VerificationOutcome,
    VerificationReport,
)

#: Check identifier emitted by every catalog static outcome.
_CHECK_ID = "catalog.static"

#: Check identifier emitted by the deprecation-graph outcome. Only present when
#: at least one catalog object is deprecated so clean catalogs keep a single
#: byte-identical ``catalog.static`` outcome.
_DEPRECATION_CHECK_ID = "catalog.deprecation"


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
    outcome = VerificationOutcome(_CHECK_ID, "passed", "declaration", "catalog", {}, ())
    return CatalogValidationResult(
        layer,
        # Delegate to ``verify_static`` so the success path is deprecation-
        # aware while a clean catalog (no deprecated objects) keeps the exact
        # same single-outcome report.
        verify_static(layer),
    )


def _reaches_cycle(
    objects: Mapping[str, SemanticObject], start: str
) -> bool:
    """Return True when the replacement chain starting at ``start`` returns to it.

    Follows ``replaced_by`` edges along resolvable, same-kind, non-self targets.
    The chain is bounded by the finite object set; a target already visited (or
    one that breaks the chain) terminates the walk without a cycle for
    ``start``.
    """
    visited: set[str] = set()
    current = start
    while True:
        target = objects[current].replaced_by
        if not target or target not in objects:
            return False
        if target.split(".", 1)[0] != current.split(".", 1)[0]:
            return False
        if target == start:
            return True
        if target == current:
            return False
        if target in visited:
            return False
        visited.add(target)
        current = target


def _deprecation_diagnostics(
    layer: SemanticLayer,
) -> tuple[VerificationDiagnostic, ...]:
    """Validate the replacement graph for every deprecated catalog object.

    For each deprecated object emit one non-blocking ``notice`` diagnostic
    (severity ``info``) plus, when its ``replaced_by`` is missing,
    unresolvable, of a different semantic kind, a self-reference, or part of a
    replacement cycle, exactly one blocking ``error`` diagnostic. The four
    failure codes (``replacement_missing``, ``replacement_kind``,
    ``self_replacement``, ``cycle``) are mutually exclusive per object, checked
    in that order so a kind mismatch is reported before any cycle walk.

    Diagnostics are returned unsorted; the outcome and report sort them.
    """
    objects = layer.semantic_objects()
    diagnostics: list[VerificationDiagnostic] = []
    deprecated_ids = sorted(
        semantic_id
        for semantic_id, value in objects.items()
        if getattr(value, "status", None) == SemanticStatus.DEPRECATED
    )
    for semantic_id in deprecated_ids:
        value = objects[semantic_id]
        diagnostics.append(
            VerificationDiagnostic(
                "catalog.deprecation.notice",
                "info",
                semantic_id,
                f"{semantic_id} is deprecated",
            )
        )
        replaced_by = getattr(value, "replaced_by", None)
        if not replaced_by or replaced_by not in objects:
            diagnostics.append(
                VerificationDiagnostic(
                    "catalog.deprecation.replacement_missing",
                    "error",
                    semantic_id,
                    f"{semantic_id} declares no resolvable replacement",
                )
            )
            continue
        source_kind = semantic_id.split(".", 1)[0]
        target_kind = replaced_by.split(".", 1)[0]
        if target_kind != source_kind:
            diagnostics.append(
                VerificationDiagnostic(
                    "catalog.deprecation.replacement_kind",
                    "error",
                    semantic_id,
                    f"{semantic_id} replacement '{replaced_by}' is a different "
                    f"semantic kind",
                )
            )
            continue
        if replaced_by == semantic_id:
            diagnostics.append(
                VerificationDiagnostic(
                    "catalog.deprecation.self_replacement",
                    "error",
                    semantic_id,
                    f"{semantic_id} replaces itself",
                )
            )
            continue
        if _reaches_cycle(objects, semantic_id):
            diagnostics.append(
                VerificationDiagnostic(
                    "catalog.deprecation.cycle",
                    "error",
                    semantic_id,
                    f"{semantic_id} participates in a replacement cycle",
                )
            )
    return tuple(diagnostics)


def verify_static(layer: SemanticLayer) -> VerificationReport:
    """Run declaration-rule validation on a typed layer.

    Maps :func:`collect_model_issues` onto an immutable verification report:
    a clean layer yields a passed ``catalog.static`` outcome, while every
    catalog issue becomes an error diagnostic carrying the catalog's stable
    code. The typed model validators and the raw YAML loader share the same
    rule implementations, so a loaded catalog and an equivalent programmatic
    layer produce identical diagnostics.

    When one or more catalog objects are deprecated, a separate
    ``catalog.deprecation`` outcome validates the replacement graph (missing,
    cross-kind, self, and cyclic replacements) and emits one non-blocking
    notice per deprecated object. A clean catalog with no deprecations keeps
    a single byte-identical ``catalog.static`` outcome.
    """
    issues = collect_model_issues(layer)
    static_diagnostics = tuple(
        VerificationDiagnostic(issue.code, "error", issue.path, issue.message)
        for issue in issues
    )
    static_status = "passed" if not static_diagnostics else "failed"
    static_outcome = VerificationOutcome(
        _CHECK_ID, static_status, "declaration", "catalog", {}, static_diagnostics
    )

    deprecation_diagnostics = _deprecation_diagnostics(layer)
    outcomes: list[VerificationOutcome] = [static_outcome]
    if deprecation_diagnostics:
        deprecated_count = sum(
            1
            for value in layer.semantic_objects().values()
            if getattr(value, "status", None) == SemanticStatus.DEPRECATED
        )
        deprecation_errors = [
            diagnostic for diagnostic in deprecation_diagnostics
            if diagnostic.severity == "error"
        ]
        outcomes.append(
            VerificationOutcome(
                _DEPRECATION_CHECK_ID,
                "passed" if not deprecation_errors else "failed",
                "declaration",
                "catalog",
                {"deprecated_count": deprecated_count},
                deprecation_diagnostics,
            )
        )

    all_diagnostics = static_diagnostics + deprecation_diagnostics
    return VerificationReport(
        1, layer.name, "static", True, tuple(outcomes), all_diagnostics
    )


__all__ = ["validate_catalog", "verify_static"]
