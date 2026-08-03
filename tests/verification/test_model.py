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


def test_report_rejects_non_one_schema_version() -> None:
    outcome = VerificationOutcome(
        "catalog.static", "passed", "declaration", "catalog", {}, ()
    )
    # Constructing with anything other than the exact schema version 1 must fail,
    # so a report can never hold or emit schema_version == 2.
    for bad in (0, 2, -1, 3):
        with pytest.raises(ValueError):
            VerificationReport(
                bad,  # type: ignore[arg-type]
                "shopfloor",
                "static",
                True,
                (outcome,),
                (),
            )
    report = VerificationReport(1, "shopfloor", "static", True, (outcome,), ())
    assert report.schema_version == 1
    assert report.to_dict()["schema_version"] == 1


def test_outcome_deep_freezes_nested_mutable_evidence() -> None:
    nested = {"min": 1, "max": 2}
    rows = [10, 20]
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={"summary": nested, "rows": rows},  # type: ignore[arg-type]
        diagnostics=(),
    )
    # Mutating the caller's original structures must not leak into the outcome.
    nested["min"] = 999
    rows.append(30)

    assert outcome.evidence["summary"] == MappingProxyType({"min": 1, "max": 2})
    assert outcome.evidence["rows"] == (10, 20)
    # Nested mappings and sequences are themselves immutable.
    with pytest.raises(TypeError):
        outcome.evidence["summary"]["min"] = 5  # type: ignore[index]
    with pytest.raises(TypeError):
        outcome.evidence["rows"][0] = 99  # type: ignore[index]


def test_report_to_dict_emits_plain_nested_evidence() -> None:
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={"summary": {"min": 1, "max": 2}, "rows": [10, 20]},  # type: ignore[arg-type]
        diagnostics=(),
    )
    report = VerificationReport(1, "shopfloor", "physical", True, (outcome,), ())

    outcomes = report.to_dict()["outcomes"]
    assert isinstance(outcomes, list)
    first = outcomes[0]
    assert isinstance(first, dict)
    evidence = first["evidence"]
    assert isinstance(evidence, dict)
    assert evidence == {"summary": {"min": 1, "max": 2}, "rows": [10, 20]}
    # Serialised evidence is plain JSON-friendly structure, not frozen proxies.
    assert isinstance(evidence["summary"], dict)
    assert isinstance(evidence["rows"], list)
