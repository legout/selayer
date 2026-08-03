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


def test_mapping_evidence_keys_are_canonicalised() -> None:
    # Mapping keys are reordered deterministically (sorted) while values are
    # preserved exactly, so serialised evidence is stable regardless of the
    # source mapping's iteration order.
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={"zebra": 1, "apple": 2, "mango": 3},
        diagnostics=(),
    )
    assert list(outcome.evidence.keys()) == ["apple", "mango", "zebra"]
    assert outcome.evidence == {"zebra": 1, "apple": 2, "mango": 3}

    report = VerificationReport(1, "shopfloor", "physical", True, (outcome,), ())
    outcomes = report.to_dict()["outcomes"]
    assert isinstance(outcomes, list)
    first = outcomes[0]
    assert isinstance(first, dict)
    evidence = first["evidence"]
    assert isinstance(evidence, dict)
    assert list(evidence.keys()) == ["apple", "mango", "zebra"]


def test_mapping_evidence_key_order_is_hash_seed_independent() -> None:
    # Mapping keys built from an unordered set have PYTHONHASHSEED-dependent
    # insertion order. The canonicalised frozen evidence (and its JSON form via
    # to_dict) must be identical across seeds, with values preserved verbatim.
    snippet = (
        "import json\n"
        "from selayer.verification.model import "
        "VerificationOutcome, VerificationReport\n"
        "o = VerificationOutcome(\n"
        "    check_id='c', status='passed', scope='declaration', path='p',\n"
        "    evidence={k: len(k) for k in {'alpha', 'beta', 'gamma'}},\n"
        "    diagnostics=(),\n"
        ")\n"
        "report = VerificationReport(1, 'shopfloor', 'physical', True, (o,), ())\n"
        "print(json.dumps(report.to_dict()['outcomes'][0]['evidence']))\n"
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
    assert len(outputs) == 1, (
        f"non-deterministic across hash seeds: {sorted(outputs)!r}"
    )
    parsed = json.loads(next(iter(outputs)))
    # Keys canonicalised (sorted); values preserved verbatim.
    assert list(parsed.keys()) == ["alpha", "beta", "gamma"]
    assert parsed == {"alpha": 5, "beta": 4, "gamma": 5}


def test_outcome_rejects_non_finite_float_evidence() -> None:
    # ``nan``/``inf``/``-inf`` cannot be emitted by strict JSON
    # (``json.dumps(..., allow_nan=False)`` raises), so they are rejected at
    # construction. Finite scalar floats are retained.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            VerificationOutcome(
                check_id="source.orders.grain",
                status="passed",
                scope="full_scan",
                path="data_sources.orders.grain",
                evidence={"ratio": bad},
                diagnostics=(),
            )
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={"ratio": 1.5},
        diagnostics=(),
    )
    assert outcome.evidence["ratio"] == 1.5


def test_outcome_rejects_nested_non_finite_float_evidence() -> None:
    # Non-finite floats are rejected wherever they appear: nested in mappings,
    # sequences, and sets.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            VerificationOutcome(
                check_id="c",
                status="passed",
                scope="declaration",
                path="p",
                evidence={"summary": {"ratio": bad}},  # type: ignore[arg-type]
                diagnostics=(),
            )
        with pytest.raises(ValueError):
            VerificationOutcome(
                check_id="c",
                status="passed",
                scope="declaration",
                path="p",
                evidence={"rows": [bad]},  # type: ignore[arg-type]
                diagnostics=(),
            )
        with pytest.raises(ValueError):
            VerificationOutcome(
                check_id="c",
                status="passed",
                scope="declaration",
                path="p",
                evidence={"vals": {bad}},  # type: ignore[arg-type]
                diagnostics=(),
            )


def test_valid_evidence_serialises_strict_json() -> None:
    # Finite scalar evidence round-trips through strict JSON (allow_nan=False).
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={
            "count": 3,
            "ratio": 0.5,
            "name": "orders",
            "flag": True,
            "missing": None,
        },
        diagnostics=(),
    )
    report = VerificationReport(1, "shopfloor", "physical", True, (outcome,), ())
    json.dumps(report.to_dict(), allow_nan=False)


def test_outcome_rejects_unsupported_value_types() -> None:
    # Unsupported / aliasing leaves (bytes, bytearray, arbitrary objects, and
    # other non-JSON types) break strict json.dumps(allow_nan=False) or violate
    # deep immutability, so they are rejected at construction with ValueError.
    for bad in (b"raw", bytearray(b"raw"), object(), complex(1, 2)):
        with pytest.raises(ValueError):
            VerificationOutcome(
                check_id="c",
                status="passed",
                scope="declaration",
                path="p",
                evidence={"value": bad},  # type: ignore[arg-type]
                diagnostics=(),
            )


def test_outcome_rejects_nested_unsupported_value_types() -> None:
    # Unsupported values are rejected wherever they appear: nested in mappings,
    # sequences, and sets.
    bad = b"raw"
    with pytest.raises(ValueError):
        VerificationOutcome(
            check_id="c",
            status="passed",
            scope="declaration",
            path="p",
            evidence={"summary": {"value": bad}},  # type: ignore[arg-type]
            diagnostics=(),
        )
    with pytest.raises(ValueError):
        VerificationOutcome(
            check_id="c",
            status="passed",
            scope="declaration",
            path="p",
            evidence={"rows": [bad]},  # type: ignore[arg-type]
            diagnostics=(),
        )
    with pytest.raises(ValueError):
        VerificationOutcome(
            check_id="c",
            status="passed",
            scope="declaration",
            path="p",
            evidence={"vals": {bad}},  # type: ignore[arg-type]
            diagnostics=(),
        )


def test_outcome_rejects_non_string_mapping_keys() -> None:
    # Mapping keys must be plain strings: bool/int/float/None keys are silently
    # JSON-coerced (lossy, non-deterministic), and tuple/bytes keys are
    # unserialisable or aliasing. All non-string keys are rejected.
    for key in (1, 1.5, True, None, (1, 2), b"k"):
        with pytest.raises(ValueError):
            VerificationOutcome(
                check_id="c",
                status="passed",
                scope="declaration",
                path="p",
                evidence={key: "v"},  # type: ignore[dict-item]
                diagnostics=(),
            )


def test_full_valid_evidence_tree_is_strict_json_safe() -> None:
    # A full evidence tree using every supported leaf and container type
    # round-trips through strict JSON (allow_nan=False) and is byte-stable.
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence={  # type: ignore[arg-type]
            "count": 3,
            "ratio": 0.5,
            "name": "orders",
            "flag": True,
            "missing": None,
            "rows": [1, 2, 3],
            "summary": {"min": 1, "max": 9},
            "tags": {"a", "b"},
        },
        diagnostics=(),
    )
    report = VerificationReport(1, "shopfloor", "physical", True, (outcome,), ())
    encoded = json.dumps(report.to_dict(), allow_nan=False)
    assert json.loads(encoded)["outcomes"][0]["evidence"]["tags"] == ["a", "b"]


# --- Finding 1: cyclic supported containers must not recurse into RecursionError ---


def test_outcome_rejects_cyclic_mapping_evidence() -> None:
    # A mapping that transitively contains itself must be rejected with a
    # controlled ValueError instead of recursing into RecursionError.
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError):
        VerificationOutcome(
            check_id="c",
            status="passed",
            scope="declaration",
            path="p",
            evidence=cyclic,  # type: ignore[arg-type]
            diagnostics=(),
        )


def test_outcome_rejects_cyclic_list_evidence() -> None:
    # A self-referential list nested under the top-level mapping must be
    # rejected rather than recursing until RecursionError.
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError):
        VerificationOutcome(
            check_id="c",
            status="passed",
            scope="declaration",
            path="p",
            evidence={"rows": cyclic},  # type: ignore[arg-type]
            diagnostics=(),
        )


def test_outcome_rejects_mixed_cycle_evidence() -> None:
    # A cycle that crosses container kinds (mapping -> list -> mapping) is
    # detected on the recursion path and rejected.
    outer: dict[str, object] = {"value": 1}
    middle: list[object] = [outer]
    outer["back"] = middle
    with pytest.raises(ValueError):
        VerificationOutcome(
            check_id="c",
            status="passed",
            scope="declaration",
            path="p",
            evidence={"root": outer},  # type: ignore[arg-type]
            diagnostics=(),
        )


def test_outcome_accepts_shared_non_cyclic_evidence_without_alias() -> None:
    # A shared (non-cyclic) sub-structure referenced from two parents is
    # permitted. The deep freeze breaks the external alias, so mutating the
    # caller's original object afterwards cannot reach the frozen evidence.
    shared = {"count": 1}
    outcome = VerificationOutcome(
        check_id="c",
        status="passed",
        scope="declaration",
        path="p",
        evidence={"a": shared, "b": shared},  # type: ignore[arg-type]
        diagnostics=(),
    )
    shared["count"] = 99
    assert outcome.evidence["a"] == MappingProxyType({"count": 1})
    assert outcome.evidence["b"] == MappingProxyType({"count": 1})
    # Each frozen branch is an independent copy (no surviving shared alias).
    assert outcome.evidence["a"] is not outcome.evidence["b"]


# --- Finding 2: top-level evidence must be a Mapping[str, EvidenceValue] ---


def test_outcome_rejects_non_mapping_top_level_evidence() -> None:
    # The declared type is Mapping[str, EvidenceValue]; a non-mapping
    # top-level evidence (list/tuple/set or any scalar) must be rejected at
    # construction rather than frozen into a wrong public shape.
    for bad in ([1, 2], (1, 2), {1, 2}, "scalar", 3, 1.5, True, None):
        with pytest.raises(ValueError):
            VerificationOutcome(
                check_id="c",
                status="passed",
                scope="declaration",
                path="p",
                evidence=bad,  # type: ignore[arg-type]
                diagnostics=(),
            )


# --- Finding 3: scalar leaves must be exact built-ins (no mutable subclasses) ---


class _StrSub(str):
    pass


class _IntSub(int):
    pass


class _FloatSub(float):
    pass


def test_outcome_rejects_scalar_subclass_evidence() -> None:
    # str/int/float subclasses may carry mutable state and are retained
    # verbatim by _freeze_evidence (a scalar alias leak), so they are rejected.
    for bad in (_StrSub("x"), _IntSub(3), _FloatSub(1.5)):
        with pytest.raises(ValueError):
            VerificationOutcome(
                check_id="c",
                status="passed",
                scope="declaration",
                path="p",
                evidence={"value": bad},  # type: ignore[arg-type]
                diagnostics=(),
            )


def test_outcome_rejects_scalar_subclass_mapping_keys() -> None:
    # A str-subclass key is retained verbatim by _freeze_evidence (alias
    # leak), so only exact ``str`` keys are accepted.
    with pytest.raises(ValueError):
        VerificationOutcome(
            check_id="c",
            status="passed",
            scope="declaration",
            path="p",
            evidence={_StrSub("k"): "v"},  # type: ignore[dict-item]
            diagnostics=(),
        )


def test_outcome_accepts_exact_builtin_scalars_without_alias() -> None:
    # Exact built-in scalar leaves are accepted and retained as exact types,
    # so no mutable subclass instance can leak into the frozen evidence.
    outcome = VerificationOutcome(
        check_id="c",
        status="passed",
        scope="declaration",
        path="p",
        evidence={"b": True, "i": 3, "f": 1.5, "s": "x", "n": None},
        diagnostics=(),
    )
    assert outcome.evidence["b"] is True
    assert type(outcome.evidence["i"]) is int
    assert outcome.evidence["i"] == 3
    assert type(outcome.evidence["f"]) is float
    assert outcome.evidence["f"] == 1.5
    assert type(outcome.evidence["s"]) is str
    assert outcome.evidence["s"] == "x"
    assert outcome.evidence["n"] is None
