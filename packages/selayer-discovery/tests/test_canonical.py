"""Canonical artifact, fingerprint, and safe-diagnostic secrecy tests.

These tests pin two load-bearing contracts of the discovery package:

* :mod:`selayer_discovery.canonical` produces deterministic canonical JSON and
  SHA-256 fingerprints over versioned artifacts. Mapping order is irrelevant,
  list order is semantic, enums/dataclasses/dates are normalized, non-finite
  floats and wall-clock timestamps are rejected, and nesting/item bounds are
  enforced.
* :mod:`selayer_discovery.diagnostics` never lets a DSN, token, source row, or
  document text reach ``repr``, ``str``, JSON, stdout, or stderr. Only stable
  codes and validated safe identifiers render.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, date, datetime, time

import pytest
from selayer_discovery.canonical import (
    UnsupportedArtifactError,
    canonical_bytes,
    fingerprint,
    normalize_artifact,
)
from selayer_discovery.diagnostics import DiscoveryError, format_diagnostic
from selayer_discovery.model import (
    MAX_COLLECTION_ITEMS,
    MAX_NESTING_DEPTH,
    SCHEMA_VERSION,
    Artifact,
    EvidenceClass,
    GateDisposition,
    GroupStatus,
    bounded_mapping,
    bounded_sequence,
    bounded_text,
    normalize_actor_identity,
    validate_artifact_id,
)

# --------------------------------------------------------------------------- #
# Canonicalization                                                            #
# --------------------------------------------------------------------------- #


def test_canonical_bytes_are_sorted_compact_utf8() -> None:
    payload = {"b": "é", "a": [1, 2]}
    assert canonical_bytes(payload) == '{"a":[1,2],"b":"é"}'.encode()


def test_mapping_order_does_not_change_fingerprint() -> None:
    left = {"b": 1, "a": 2, "c": 3}
    right = {"c": 3, "a": 2, "b": 1}
    assert normalize_artifact(left) == normalize_artifact(right)
    assert fingerprint(left) == fingerprint(right)


def test_list_order_is_semantic() -> None:
    assert fingerprint([1, 2, 3]) == fingerprint([1, 2, 3])
    assert fingerprint([1, 2, 3]) != fingerprint([3, 2, 1])


def test_enum_is_normalized_to_value() -> None:
    assert normalize_artifact(EvidenceClass.OBSERVED) == "observed"
    assert fingerprint({"class": EvidenceClass.OBSERVED}) == fingerprint(
        {"class": "observed"}
    )


def test_dataclass_canonicalizes_to_field_dict() -> None:
    @dataclass(frozen=True)
    class Point:
        x: int
        y: int

    assert normalize_artifact(Point(1, 2)) == {"x": 1, "y": 2}
    assert fingerprint(Point(1, 2)) == fingerprint({"x": 1, "y": 2})


def test_date_canonicalizes_to_iso_string() -> None:
    assert normalize_artifact(date(2026, 1, 15)) == "2026-01-15"
    assert fingerprint(date(2026, 1, 15)) == fingerprint("2026-01-15")


def test_int_float_and_bool_are_distinct() -> None:
    assert normalize_artifact(3) == 3
    assert normalize_artifact(3.0) == 3.0
    assert normalize_artifact(True) is True
    # JSON distinguishes ``3`` / ``3.0`` / ``true``, so fingerprints differ.
    assert canonical_bytes(3) != canonical_bytes(3.0)
    assert fingerprint(True) != fingerprint(1)
    assert fingerprint(0) != fingerprint(False)


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), float("-inf"), math.nan, math.inf, -math.inf],
)
def test_non_finite_floats_are_rejected(bad: float) -> None:
    with pytest.raises(UnsupportedArtifactError):
        normalize_artifact(bad)
    with pytest.raises(UnsupportedArtifactError):
        canonical_bytes(bad)


@pytest.mark.parametrize(
    "bad",
    [object(), {1, 2}, frozenset({1}), b"bytes", bytearray(b"x"), complex(1, 2)],
)
def test_unsupported_objects_are_rejected(bad: object) -> None:
    with pytest.raises(UnsupportedArtifactError):
        canonical_bytes(bad)


def test_non_string_mapping_keys_are_rejected() -> None:
    with pytest.raises(UnsupportedArtifactError):
        canonical_bytes({1: "a"})


def test_nested_structure_round_trips_through_json() -> None:
    normalized = normalize_artifact(
        {
            "id": "claim-1",
            "class": EvidenceClass.ASSERTED,
            "observed_on": date(2026, 2, 3),
            "evidence": ["a", "b"],
        }
    )
    # The normalized form is JSON-native and stable.
    assert json.dumps(normalized, sort_keys=True) is not None
    assert fingerprint(normalized) == fingerprint(
        {
            "evidence": ["a", "b"],
            "class": "asserted",
            "id": "claim-1",
            "observed_on": "2026-02-03",
        }
    )


def _nested(depth: int) -> object:
    node: object = "leaf"
    for _ in range(depth):
        node = {"k": node}
    return node


def test_maximum_nesting_is_enforced() -> None:
    # ``MAX_NESTING_DEPTH`` container wrappers are accepted; one more is not.
    fingerprint(_nested(MAX_NESTING_DEPTH))
    with pytest.raises(UnsupportedArtifactError):
        fingerprint(_nested(MAX_NESTING_DEPTH + 1))


def test_maximum_collection_items_are_enforced() -> None:
    fingerprint(list(range(MAX_COLLECTION_ITEMS)))
    fingerprint({str(i): i for i in range(MAX_COLLECTION_ITEMS)})
    with pytest.raises(UnsupportedArtifactError):
        fingerprint(list(range(MAX_COLLECTION_ITEMS + 1)))
    with pytest.raises(UnsupportedArtifactError):
        fingerprint({str(i): i for i in range(MAX_COLLECTION_ITEMS + 1)})


def test_timestamps_are_rejected_from_semantic_payloads() -> None:
    with pytest.raises(UnsupportedArtifactError):
        canonical_bytes({"id": "x", "captured_at": datetime(2026, 1, 1, 12, 30, 45, tzinfo=UTC)})
    with pytest.raises(UnsupportedArtifactError):
        normalize_artifact(time(12, 30, 45))


def test_fingerprint_is_deterministic_sha256_hex() -> None:
    payload = {"a": 1, "b": [1, 2, 3]}
    assert fingerprint(payload) == fingerprint({"b": [1, 2, 3], "a": 1})
    digest = fingerprint(payload)
    assert len(digest) == 64
    int(digest, 16)  # valid lowercase hex


# --------------------------------------------------------------------------- #
# Versioned artifact base                                                     #
# --------------------------------------------------------------------------- #


def test_artifact_base_carries_schema_version() -> None:
    @dataclass(frozen=True)
    class ClaimRecord(Artifact):
        claim_id: str
        klass: EvidenceClass
        observed_on: date

    record = ClaimRecord(
        artifact_id="claim-c1",
        claim_id="c1",
        klass=EvidenceClass.OBSERVED,
        observed_on=date(2026, 1, 2),
    )
    normalized = normalize_artifact(record)
    assert normalized == {
        "artifact_id": "claim-c1",
        "schema_version": SCHEMA_VERSION,
        "claim_id": "c1",
        "klass": "observed",
        "observed_on": "2026-01-02",
    }
    assert fingerprint(record) == fingerprint(normalized)
    assert "schema_version" not in {"claim_id"}  # sanity guard against typos


def test_artifact_base_is_immutable() -> None:
    @dataclass(frozen=True)
    class Record(Artifact):
        value: int

    record = Record(artifact_id="record-1", value=1)
    with pytest.raises(FrozenInstanceError):
        record.value = 2  # type: ignore[misc]


def test_enums_cover_design_vocabulary() -> None:
    assert {c.value for c in EvidenceClass} == {"observed", "asserted", "inferred"}
    assert {c.value for c in GroupStatus} == {
        "draft",
        "blocked",
        "ready",
        "accepted",
        "rejected",
        "deferred",
        "stale",
        "applied",
    }
    assert {c.value for c in GateDisposition} == {
        "answered",
        "not_applicable",
        "blocked",
    }


# --------------------------------------------------------------------------- #
# Validators and actor identity                                               #
# --------------------------------------------------------------------------- #


def test_bounded_text_accepts_and_rejects() -> None:
    class _HostileStr(str):
        """A str subclass that must be rejected as plain text."""

    assert bounded_text("ok") == "ok"
    assert bounded_text("x", max_length=2) == "x"
    with pytest.raises(DiscoveryError):
        bounded_text("xyz", max_length=2)
    with pytest.raises(DiscoveryError):
        bounded_text(_HostileStr("ok"))  # str subclass is not plain text
    with pytest.raises(DiscoveryError):
        bounded_text(123)  # type: ignore[arg-type]


def test_bounded_sequence_accepts_lists_and_tuples_only() -> None:
    assert bounded_sequence([1, 2, 3]) == [1, 2, 3]
    assert bounded_sequence((1, 2)) == [1, 2]
    with pytest.raises(DiscoveryError):
        bounded_sequence("ab")  # type: ignore[arg-type]
    with pytest.raises(DiscoveryError):
        bounded_sequence({1, 2})  # type: ignore[arg-type]
    with pytest.raises(DiscoveryError):
        bounded_sequence({"a": 1})  # type: ignore[arg-type]
    with pytest.raises(DiscoveryError):
        bounded_sequence(list(range(MAX_COLLECTION_ITEMS + 1)))


def test_bounded_mapping_requires_string_keys() -> None:
    assert bounded_mapping({"a": 1}) == {"a": 1}
    with pytest.raises(DiscoveryError):
        bounded_mapping([1, 2])  # type: ignore[arg-type]
    with pytest.raises(DiscoveryError):
        bounded_mapping({1: "a"})  # type: ignore[arg-type]
    with pytest.raises(DiscoveryError):
        bounded_mapping({str(i): i for i in range(MAX_COLLECTION_ITEMS + 1)})


def test_normalize_actor_identity_collapses_and_composes() -> None:
    assert normalize_actor_identity("  Alice   Smith ") == "Alice Smith"
    assert normalize_actor_identity("Alice\tSmith\n") == "Alice Smith"
    # NFC composes a decomposed "é".
    assert normalize_actor_identity("e\u0301") == "é"
    with pytest.raises(DiscoveryError):
        normalize_actor_identity("")
    with pytest.raises(DiscoveryError):
        normalize_actor_identity("   ")
    with pytest.raises(DiscoveryError):
        normalize_actor_identity(123)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Diagnostic secrecy                                                          #
# --------------------------------------------------------------------------- #

# Each fragment is a secret that must never appear in any rendered surface.
_SECRET_FRAGMENTS = [
    "postgres://user:s3cr3t@db.example.com:5432/prod",
    "s3cr3t",
    "sk-live-token-9f8a7c6b5d",
    "alice@example.com",
    "SN-0001",
    "SN-0002",
    "CONFIDENTIAL drive serial numbers",
    "db.example.com",
]

_FULL_SECRETS = [
    "postgres://user:s3cr3t@db.example.com:5432/prod",  # DSN
    "sk-live-token-9f8a7c6b5d",  # token
    "Alice|42|alice@example.com",  # source row
    "CONFIDENTIAL: the drive serial numbers are SN-0001 and SN-0002",  # document text
]


def _secret_error() -> DiscoveryError:
    """Build a diagnostic whose raw context carries every secret.

    The ``RuntimeError`` is captured and the diagnostic is constructed
    *outside* the ``except`` scope so ``__cause__`` / ``__context__`` remain
    ``None``, mirroring :class:`selayer.sources.errors.SourceError`.
    """
    try:
        raise RuntimeError("\n".join(_FULL_SECRETS))
    except RuntimeError as caught:
        cause: object = caught
    return DiscoveryError(
        "discovery.artifact.invalid",
        safe_detail="evidence record rejected",
        safe_ids=("session-abc123", "record-001"),
        context=cause,
    )


def _assert_no_secrets(text: str) -> None:
    for fragment in _SECRET_FRAGMENTS:
        assert fragment not in text, f"secret fragment leaked: {fragment!r}"


def test_diagnostic_rejects_untrusted_rendered_fields() -> None:
    token = "sk-live-token-9f8a7c6b5d"
    error = DiscoveryError(
        "discovery.artifact.invalid",
        safe_detail="postgres://user:s3cr3t@db.example.com/prod",
        safe_ids=[token] * (MAX_COLLECTION_ITEMS + 1),
    )
    rendered = json.dumps(error.to_dict(), sort_keys=True) + repr(error)
    _assert_no_secrets(rendered)
    assert "<id>" in rendered
    assert "postgres://" not in rendered


def test_artifact_ids_are_bounded_and_grammar_checked() -> None:
    assert str(validate_artifact_id("claim-c1")) == "claim-c1"
    with pytest.raises(DiscoveryError):
        validate_artifact_id("../secret")
    with pytest.raises(DiscoveryError):
        validate_artifact_id("x" * 129)


def test_exception_str_is_safe() -> None:
    error = _secret_error()
    rendered = str(error)
    _assert_no_secrets(rendered)
    assert error.code in rendered
    assert "session-abc123" in rendered


def test_exception_repr_is_safe() -> None:
    error = _secret_error()
    rendered = repr(error)
    _assert_no_secrets(rendered)
    assert error.code in rendered
    assert "_context" not in rendered


def test_exception_to_dict_json_is_safe() -> None:
    error = _secret_error()
    payload = json.dumps(error.to_dict(), sort_keys=True)
    _assert_no_secrets(payload)
    assert error.code in payload


def test_diagnostic_output_never_reaches_stdout_or_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = _secret_error()
    rendered = format_diagnostic(error)
    print(rendered)  # stdout
    print(rendered, file=sys.stderr)  # stderr
    out, err = capsys.readouterr()
    _assert_no_secrets(rendered)
    _assert_no_secrets(out)
    _assert_no_secrets(err)
    assert error.code in out
    assert error.code in err


def test_unknown_code_is_coerced_to_fallback() -> None:
    error = DiscoveryError("discovery.totally.made-up secret=leak")
    assert error.code == "discovery.internal"
    assert "leak" not in str(error)
    assert "discovery.internal" in str(error)


def test_unsafe_safe_id_is_replaced() -> None:
    error = DiscoveryError(
        "discovery.artifact.invalid",
        safe_ids=("session-abc123", "postgres://s3cr3t@db.example.com"),
    )
    rendered = repr(error)
    _assert_no_secrets(rendered)
    assert "session-abc123" in rendered
    assert "<id>" in rendered


def test_unsupported_artifact_error_is_a_discovery_error() -> None:
    error = UnsupportedArtifactError("discovery.canonical.unsupported")
    assert isinstance(error, DiscoveryError)
    assert error.code == "discovery.canonical.unsupported"
    _assert_no_secrets(str(error))
