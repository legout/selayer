import json
import os
import subprocess
import sys
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


def test_report_rejects_bool_and_float_schema_version() -> None:
    outcome = VerificationOutcome(
        "catalog.static", "passed", "declaration", "catalog", {}, ()
    )
    # ``True`` (bool) and ``1.0`` (float) compare equal to ``1`` under Python
    # numeric equality, but only an exact ``int`` value of ``1`` is accepted.
    for bad in (True, 1.0):
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
    assert type(report.schema_version) is int


def test_outcome_freezes_nested_set_evidence() -> None:
    violations = {"a", "b", "c"}
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={"violations": violations},  # type: ignore[arg-type]
        diagnostics=(),
    )
    # The set is frozen into a deterministically ordered tuple; mutating the
    # caller's original set does not leak into the outcome.
    violations.add("d")

    frozen = outcome.evidence["violations"]
    assert isinstance(frozen, tuple)
    assert frozen == ("a", "b", "c")
    # The frozen set-backed sequence is itself immutable.
    with pytest.raises(TypeError):
        frozen[0] = "z"  # type: ignore[index]


def test_report_to_dict_emits_deterministic_set_evidence() -> None:
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={"violations": {"c", "a", "b"}},  # type: ignore[arg-type]
        diagnostics=(),
    )
    report = VerificationReport(1, "shopfloor", "physical", True, (outcome,), ())

    outcomes = report.to_dict()["outcomes"]
    assert isinstance(outcomes, list)
    first = outcomes[0]
    assert isinstance(first, dict)
    evidence = first["evidence"]
    assert isinstance(evidence, dict)
    violations = evidence["violations"]
    # Sets serialise as a deterministically ordered plain list.
    assert isinstance(violations, list)
    assert violations == ["a", "b", "c"]
    # The whole payload round-trips through JSON (sets are not JSON-serialisable,
    # so this confirms determinism + serialisability of the serialised form).
    json.dumps(report.to_dict())


def test_outcome_preserves_sequence_evidence_order() -> None:
    # Ordered sequence evidence must keep the caller's order exactly: lists and
    # tuples are frozen but NOT reordered (regression for the prior
    # _ordered()-based reordering that silently sorted [2, 1] -> (1, 2)).
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={"descending": [3, 1, 2], "pairs": [(2, 1), (1, 2)]},  # type: ignore[arg-type]
        diagnostics=(),
    )
    assert outcome.evidence["descending"] == (3, 1, 2)
    assert outcome.evidence["pairs"] == ((2, 1), (1, 2))

    report = VerificationReport(1, "shopfloor", "physical", True, (outcome,), ())
    outcomes = report.to_dict()["outcomes"]
    assert isinstance(outcomes, list)
    first = outcomes[0]
    assert isinstance(first, dict)
    evidence = first["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["descending"] == [3, 1, 2]
    assert evidence["pairs"] == [[2, 1], [1, 2]]


def test_outcome_canonicalizes_unordered_nested_evidence() -> None:
    # A set of partially ordered members (frozensets) is canonicalised to a
    # deterministic order that does not rely on Python's subset-based `<`,
    # which is not a total order and would otherwise leave members in iteration
    # order.
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={"groups": {frozenset({3, 1, 2}), frozenset({5, 4})}},  # type: ignore[arg-type]
        diagnostics=(),
    )
    groups = outcome.evidence["groups"]
    assert groups == ((1, 2, 3), (4, 5))

    report = VerificationReport(1, "shopfloor", "physical", True, (outcome,), ())
    outcomes = report.to_dict()["outcomes"]
    assert isinstance(outcomes, list)
    first = outcomes[0]
    assert isinstance(first, dict)
    evidence = first["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["groups"] == [[1, 2, 3], [4, 5]]


def test_unordered_nested_evidence_is_hash_seed_independent() -> None:
    # Members are frozensets of strings: PYTHONHASHSEED randomises str hashes,
    # so a partially-ordered ``sorted()`` over them is non-deterministic. The
    # canonical ordering must yield identical evidence in fresh interpreters
    # regardless of the seed (hash randomisation is fixed at interpreter start,
    # hence the subprocess). JSON is used as the wire format so the comparison
    # does not depend on tuple ``repr`` formatting.
    snippet = (
        "import json\n"
        "from selayer.verification.model import VerificationOutcome\n"
        "o = VerificationOutcome(\n"
        "    check_id='c', status='passed', scope='declaration', path='p',\n"
        "    evidence={'g': {frozenset({'alpha', 'beta'}),\n"
        "                    frozenset({'gamma', 'delta'}),\n"
        "                    frozenset({'epsilon', 'zeta'})}},\n"
        "    diagnostics=(),\n"
        ")\n"
        "print(json.dumps(o.evidence['g']))\n"
    )
    outputs: set[str] = set()
    for seed in ("0", "1", "2", "13", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, f"non-deterministic across hash seeds: {sorted(outputs)!r}"
    parsed = json.loads(next(iter(outputs)))
    assert parsed == [
        ["alpha", "beta"],
        ["delta", "gamma"],
        ["epsilon", "zeta"],
    ]
