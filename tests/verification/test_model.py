from types import MappingProxyType

import pytest
from selayer.verification.model import (
    VerificationDiagnostic,
    VerificationOutcome,
    VerificationReport,
)


def test_verification_report_freezes_nested_evidence() -> None:
    evidence = {"row_count": 3}
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence=evidence,
        diagnostics=(),
    )
    evidence["row_count"] = 99
    assert outcome.evidence == MappingProxyType({"row_count": 3})
    with pytest.raises(TypeError):
        outcome.evidence["row_count"] = 4  # type: ignore[index]


def test_report_passed_requires_complete_success() -> None:
    passed = VerificationOutcome(
        "catalog.static", "passed", "declaration", "catalog", {}, ()
    )
    unavailable = VerificationOutcome(
        "source.orders", "unavailable", "full_scan", "data_sources.orders", {}, ()
    )
    assert VerificationReport(1, "shopfloor", "static", True, (passed,), ()).passed
    assert not VerificationReport(
        1, "shopfloor", "physical", False, (passed, unavailable), ()
    ).passed


def test_report_error_diagnostic_prevents_pass() -> None:
    diagnostic = VerificationDiagnostic(
        "catalog.grain.duplicate_column",
        "error",
        "data_sources.orders.grain",
        "grain columns must be unique",
    )
    outcome = VerificationOutcome(
        "catalog.static", "failed", "declaration", "catalog", {}, (diagnostic,)
    )
    report = VerificationReport(1, "shopfloor", "static", True, (outcome,), (diagnostic,))
    assert not report.passed
    assert report.to_dict()["schema_version"] == 1
