"""Append-only session store: state machine, event integrity, concurrency,
permissions, and transitive dependency invalidation.

These tests pin the Task 7 contract of the discovery package:

* :class:`selayer_discovery.session.SessionStore` records a hash-chained,
  append-only event journal and reconstructs state from it on every path; the
  materialized state cache is never authority.
* Allowed state transitions advance the session; direct skips, duplicate
  terminal events, and any mutation after close are rejected; editing the cache
  cannot revive state.
* Every event carries schema version, event id, previous hash, event hash,
  normalized actor, UTC timestamp, type, and a bounded payload; tampering,
  truncation, reordering, and duplicate ids fail reconstruction.
* Writers serialize through ``filelock``; lock timeout reports only safe owner
  metadata; session directories are ``0700`` and files are ``0600`` (POSIX).
* An explicit directed dependency index emits sorted transitive stale targets
  when a charter, approver, or recorded artifact hash changes.
"""

from __future__ import annotations

import itertools
import json
import os
import stat
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from selayer_discovery import canonical
from selayer_discovery.model import (
    MAX_TEXT_LENGTH,
    SCHEMA_VERSION,
    normalize_actor_identity,
)
from selayer_discovery.session import (
    GENESIS_HASH,
    MAX_EVENT_PAYLOAD_BYTES,
    ArtifactRecord,
    SessionCharter,
    SessionError,
    SessionEvent,
    SessionLockTimeoutError,
    SessionSnapshot,
    SessionState,
    SessionStore,
)

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file permissions"
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _journal_path(root: Path) -> Path:
    return root / "events.jsonl"


def _journal_lines(root: Path) -> list[str]:
    return _journal_path(root).read_text(encoding="utf-8").splitlines()


def _rewrite_journal(root: Path, lines: list[str]) -> None:
    _journal_path(root).write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _advance_to(
    store: SessionStore,
    target: SessionState,
    *,
    actor: str,
) -> None:
    """Walk a fresh store forward to ``target`` along allowed transitions."""

    chain = [
        SessionState.INTAKE,
        SessionState.SAMPLE_POLICY_PENDING,
        SessionState.INTERVIEWING,
        SessionState.DRAFTING,
        SessionState.REVIEW_READY,
        SessionState.APPROVED,
        SessionState.APPLIED,
        SessionState.CLOSED,
    ]
    for state in chain:
        if store.state is target:
            return
        if state is SessionState.CLOSED:
            store.close(actor=actor)
        else:
            store.transition(state, actor=actor)
        if store.state is target:
            return


def _event_line(
    previous_hash: str,
    event_type: str,
    payload: Mapping[str, object],
    *,
    actor: str,
) -> str:
    """Build a single hash-valid, chain-linked journal event line.

    Used by replay-regression tests to construct journals whose events are
    individually hash-valid (so the integrity failure must come from the
    replayed state-machine or payload-bound logic, never a hash mismatch).
    """

    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "previous_hash": previous_hash,
        "actor": normalize_actor_identity(actor),
        "timestamp": datetime.now(UTC).isoformat(timespec="microseconds"),
        "type": event_type,
        "payload": dict(payload),
    }
    event_hash = canonical.fingerprint(record)
    return json.dumps(
        {**record, "event_hash": event_hash}, ensure_ascii=False, sort_keys=True
    )


# --------------------------------------------------------------------------- #
# Step 1: state-transition tests                                              #
# --------------------------------------------------------------------------- #


def test_create_records_charter_and_sets_initialized(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)

    assert store.state is SessionState.INITIALIZED
    assert store.session_id == charter.session_id
    # The reconstructed charter equals the recorded one (approver normalized).
    assert store.charter == charter
    assert store.charter.approver == normalize_actor_identity(actor)
    assert _journal_path(session_root).exists()


def test_charter_fingerprint_is_stable_canonical_sha256(
    charter: SessionCharter,
) -> None:
    digest = charter.fingerprint
    assert len(digest) == 64
    int(digest, 16)
    # Order-independent over equivalent field construction.
    assert charter.fingerprint == canonical.fingerprint(charter)


def test_allowed_transitions_advance_state_in_order(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)

    store.transition(SessionState.INTAKE, actor=actor)
    assert store.state is SessionState.INTAKE
    store.transition(SessionState.SAMPLE_POLICY_PENDING, actor=actor)
    assert store.state is SessionState.SAMPLE_POLICY_PENDING
    store.transition(SessionState.INTERVIEWING, actor=actor)
    assert store.state is SessionState.INTERVIEWING
    store.transition(SessionState.DRAFTING, actor=actor)
    assert store.state is SessionState.DRAFTING
    store.transition(SessionState.REVIEW_READY, actor=actor)
    assert store.state is SessionState.REVIEW_READY
    store.transition(SessionState.APPROVED, actor=actor)
    assert store.state is SessionState.APPROVED
    store.transition(SessionState.APPLIED, actor=actor)
    assert store.state is SessionState.APPLIED
    store.close(actor=actor)
    assert store.state is SessionState.CLOSED


@pytest.mark.parametrize(
    "skip_target",
    [
        SessionState.INTERVIEWING,
        SessionState.DRAFTING,
        SessionState.REVIEW_READY,
        SessionState.APPROVED,
        SessionState.APPLIED,
    ],
)
def test_direct_skips_are_rejected(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    skip_target: SessionState,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    with pytest.raises(SessionError, match="transition_invalid"):
        store.transition(skip_target, actor=actor)
    # State is unchanged after a rejected transition.
    assert store.state is SessionState.INITIALIZED


def test_return_from_drafting_and_review_ready_to_interviewing(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    _advance_to(store, SessionState.DRAFTING, actor=actor)
    store.transition(SessionState.INTERVIEWING, actor=actor)
    assert store.state is SessionState.INTERVIEWING
    store.transition(SessionState.DRAFTING, actor=actor)
    store.transition(SessionState.REVIEW_READY, actor=actor)
    store.transition(SessionState.INTERVIEWING, actor=actor)
    assert store.state is SessionState.INTERVIEWING


def test_self_transition_is_rejected(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    with pytest.raises(SessionError):
        store.transition(SessionState.INITIALIZED, actor=actor)


def test_duplicate_close_is_rejected(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.close(actor=actor)
    assert store.state is SessionState.CLOSED
    with pytest.raises(SessionError):
        store.close(actor=actor)


def test_mutation_after_close_is_rejected(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.close(actor=actor)

    with pytest.raises(SessionError):
        store.transition(SessionState.INTAKE, actor=actor)
    with pytest.raises(SessionError):
        store.record_artifact(
            "claim-c1", content_hash=hash_factory(1), actor=actor
        )
    with pytest.raises(SessionError):
        store.revise_charter(charter, actor=actor)


def test_state_is_not_revived_by_editing_the_cache(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)
    assert store.state is SessionState.INTAKE

    # Tamper with the non-authority materialized state cache.
    cache = session_root / "state.json"
    assert cache.exists()
    cache.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "state": "applied",
                "head_hash": "deadbeef",
                "charter_fingerprint": "x",
                "stale_nodes": ["fake-node"],
            }
        ),
        encoding="utf-8",
    )

    reopened = SessionStore.open(session_root)
    snapshot = reopened.reconstruct()
    # The journal is authority: the cache edit is ignored.
    assert snapshot.state is SessionState.INTAKE
    assert snapshot.head_hash == store.head_hash
    assert snapshot.stale_nodes == ()


def test_reopen_rebuilds_state_from_journal(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)
    store.transition(SessionState.SAMPLE_POLICY_PENDING, actor=actor)
    head = store.head_hash

    reopened = SessionStore.open(session_root)
    assert reopened.state is SessionState.SAMPLE_POLICY_PENDING
    assert reopened.charter == charter
    assert reopened.head_hash == head
    assert len(reopened.events) == len(store.events)


def test_repeated_create_fails_when_journal_exists(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    with pytest.raises(SessionError, match="exists"):
        SessionStore.create(session_root, charter=charter, actor=actor)


def test_open_missing_journal_is_not_initialized(session_root: Path) -> None:
    with pytest.raises(SessionError, match="not_initialized"):
        SessionStore.open(session_root)


# --------------------------------------------------------------------------- #
# Step 2: event-integrity tests                                               #
# --------------------------------------------------------------------------- #


def test_events_have_all_required_fields_and_utc_timestamp(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor, payload={"note": "ok"})

    for event in store.events:
        assert event.schema_version == SCHEMA_VERSION
        assert isinstance(event.event_id, str) and len(event.event_id) == 32
        assert isinstance(event.previous_hash, str)
        assert isinstance(event.event_hash, str) and len(event.event_hash) == 64
        assert event.actor == normalize_actor_identity(actor)
        assert isinstance(event.type, str) and event.type
        assert isinstance(event.payload, dict)
        # Timestamp parses as UTC.
        parsed = datetime.fromisoformat(event.timestamp)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() is not None
        assert parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_event_hash_covers_all_fields_except_itself(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)

    for event in store.events:
        record = {
            "schema_version": event.schema_version,
            "event_id": event.event_id,
            "previous_hash": event.previous_hash,
            "actor": event.actor,
            "timestamp": event.timestamp,
            "type": event.type,
            "payload": dict(event.payload),
        }
        assert canonical.fingerprint(record) == event.event_hash

    genesis = store.events[0]
    tampered = {
        "schema_version": genesis.schema_version,
        "event_id": genesis.event_id,
        "previous_hash": genesis.previous_hash,
        "actor": "someone-else",
        "timestamp": genesis.timestamp,
        "type": genesis.type,
        "payload": dict(genesis.payload),
    }
    assert canonical.fingerprint(tampered) != genesis.event_hash


def test_genesis_previous_hash_is_sentinel_and_chain_links(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)

    events = store.events
    assert events[0].previous_hash == GENESIS_HASH
    for prior, current in itertools.pairwise(events):
        assert current.previous_hash == prior.event_hash


def test_tampered_payload_fails_reconstruction(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    lines = _journal_lines(session_root)
    obj = json.loads(lines[0])
    # Mutate the charter business question without updating the event hash.
    obj["payload"]["charter"]["business_question"] = "tampered question"
    lines[0] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    _rewrite_journal(session_root, lines)

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_tampered_event_hash_fails_reconstruction(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    lines = _journal_lines(session_root)
    obj = json.loads(lines[0])
    obj["event_hash"] = "f" * 64
    lines[0] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    _rewrite_journal(session_root, lines)

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_truncated_journal_line_fails_reconstruction(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    lines = _journal_lines(session_root)
    # Cut the last line short (a partial fsync on crash).
    lines[-1] = lines[-1][: len(lines[-1]) // 2]
    _rewrite_journal(session_root, lines)

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_missing_middle_event_fails_reconstruction(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)
    store.transition(SessionState.SAMPLE_POLICY_PENDING, actor=actor)

    lines = _journal_lines(session_root)
    # Drop the middle event; the chain link breaks.
    del lines[1]
    _rewrite_journal(session_root, lines)

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_reordered_events_fail_reconstruction(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)
    store.transition(SessionState.SAMPLE_POLICY_PENDING, actor=actor)

    lines = _journal_lines(session_root)
    # Swap the two non-genesis events.
    lines[1], lines[2] = lines[2], lines[1]
    _rewrite_journal(session_root, lines)

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_duplicate_event_line_fails_reconstruction(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)

    lines = _journal_lines(session_root)
    # Duplicate the last event line.
    lines.append(lines[-1])
    _rewrite_journal(session_root, lines)

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_duplicate_genesis_event_fails_reconstruction(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    lines = _journal_lines(session_root)
    # Insert a second genesis line after the first (chain/id breaks).
    lines.insert(1, lines[0])
    _rewrite_journal(session_root, lines)

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_payload_is_validated_before_append(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    with pytest.raises(SessionError):
        store.transition(
            SessionState.INTAKE, actor=actor, payload="not-a-mapping"  # type: ignore[arg-type]
        )
    with pytest.raises(SessionError):
        store.transition(
            SessionState.INTAKE,
            actor=actor,
            payload={"bad": {1, 2, 3}},  # uncanonicalizable set
        )


# --------------------------------------------------------------------------- #
# Step 3: concurrency and permissions tests                                   #
# --------------------------------------------------------------------------- #


def test_two_writers_serialize_through_the_lock(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store1 = SessionStore.create(session_root, charter=charter, actor=actor)
    store2 = SessionStore.open(session_root)

    # store1 advances to INTAKE and releases the lock.
    store1.transition(SessionState.INTAKE, actor=actor)
    # store2 builds on store1's committed state (lock-protected re-read).
    store2.transition(SessionState.SAMPLE_POLICY_PENDING, actor="bob")

    snap1 = store1.reconstruct()
    snap2 = store2.reconstruct()
    assert snap1.state is SessionState.SAMPLE_POLICY_PENDING
    assert snap2.state is SessionState.SAMPLE_POLICY_PENDING
    assert snap1.head_hash == snap2.head_hash


def test_lock_timeout_reports_safe_owner_metadata(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store1 = SessionStore.create(session_root, charter=charter, actor=actor)
    # Open store2 while the lock is free, then contend.
    store2 = SessionStore.open(session_root, lock_timeout=0.2)

    with store1.lock():
        with pytest.raises(SessionLockTimeoutError) as raised:
            store2.transition(SessionState.INTAKE, actor="bob")

        owner = raised.value.owner
        assert owner is not None
        assert owner["session_id"] == charter.session_id
        assert owner["actor"] == normalize_actor_identity(actor)
        assert set(owner) == {"session_id", "actor", "acquired_at"}

        rendered = str(raised.value) + repr(raised.value) + json.dumps(
            raised.value.to_dict(), sort_keys=True
        )
        # No process arguments or secrets leak through the lock diagnostic.
        assert "argv" not in rendered
        assert "bob" not in rendered

    # After release, the contending writer succeeds.
    store2.transition(SessionState.INTAKE, actor="bob")
    assert store2.state is SessionState.INTAKE


@posix_only
def test_session_directory_mode_is_0700(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    mode = stat.S_IMODE(session_root.stat().st_mode)
    assert mode == 0o700


@posix_only
def test_journal_and_cache_files_are_mode_0600(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)

    for name in ("events.jsonl", "state.json"):
        path = session_root / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@posix_only
def test_owner_metadata_file_is_mode_0600_while_locked(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    with store.lock():
        owner = session_root / "session.lock.owner"
        assert owner.exists()
        assert stat.S_IMODE(owner.stat().st_mode) == 0o600
    # The owner sidecar is cleared once the lock is released.
    assert not (session_root / "session.lock.owner").exists()


# --------------------------------------------------------------------------- #
# Step 5: dependency invalidation events                                      #
# --------------------------------------------------------------------------- #


def _seed_dependency_chain(
    store: SessionStore,
    *,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    """Record a transitive dependency chain for invalidation tests."""

    store.record_artifact(
        "claim-c1", content_hash=hash_factory(1), depends_on=(), actor=actor
    )
    store.record_artifact(
        "verification-g1",
        content_hash=hash_factory(2),
        depends_on=("claim-c1",),
        actor=actor,
    )
    store.record_artifact(
        "proposal-g1",
        content_hash=hash_factory(3),
        depends_on=("verification-g1", "charter"),
        actor=actor,
    )
    store.record_artifact(
        "approval-g1",
        content_hash=hash_factory(4),
        depends_on=("proposal-g1",),
        actor=actor,
    )


def test_record_artifact_registers_node_and_dependency_edges(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    record = store.record_artifact(
        "verification-g1",
        content_hash=hash_factory(2),
        depends_on=("claim-c1", "charter"),
        actor=actor,
    )
    assert isinstance(record, ArtifactRecord)
    assert record.stale_targets == ()

    snapshot = store.reconstruct()
    assert snapshot.dependencies["verification-g1"] == frozenset(
        {"claim-c1", "charter"}
    )
    assert "verification-g1" in snapshot.dependencies
    # The genesis charter node is itself a registered dependency node.
    assert "charter" in snapshot.artifact_hashes


def test_changed_claim_emits_sorted_transitive_stale_targets(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    _seed_dependency_chain(store, actor=actor, hash_factory=hash_factory)

    # Pure query before any revision.
    assert store.compute_stale("claim-c1") == (
        "approval-g1",
        "proposal-g1",
        "verification-g1",
    )

    # Revise the claim's hash.
    record = store.record_artifact(
        "claim-c1", content_hash=hash_factory(9), depends_on=(), actor=actor
    )
    assert record.stale_targets == (
        "approval-g1",
        "proposal-g1",
        "verification-g1",
    )
    assert store.stale_nodes == (
        "approval-g1",
        "proposal-g1",
        "verification-g1",
    )
    # The changed node itself is not reported as a stale target.
    assert "claim-c1" not in record.stale_targets


def test_changed_charter_stales_charter_dependents(
    session_root: Path,
    make_charter,  # type: ignore[no-untyped-def]
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    _seed_dependency_chain(store, actor=actor, hash_factory=hash_factory)

    # Revise the charter (business question only; approver unchanged).
    revised = make_charter(business_question="A different business question.")
    record = store.revise_charter(revised, actor=actor)

    assert "proposal-g1" in record.stale_targets
    assert "approval-g1" in record.stale_targets
    assert "verification-g1" not in record.stale_targets
    assert store.charter == revised


def test_changed_approver_stales_approver_dependents(
    session_root: Path,
    make_charter,  # type: ignore[no-untyped-def]
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.record_artifact(
        "policy-activation",
        content_hash=hash_factory(5),
        depends_on=("approver",),
        actor=actor,
    )

    # Revise only the approver.
    revised = make_charter(approver="Dr. Bo  Okafor")
    record = store.revise_charter(revised, actor=actor)

    assert "policy-activation" in record.stale_targets
    assert store.charter.approver == normalize_actor_identity("Dr. Bo  Okafor")


def test_revision_is_idempotent_when_hash_is_unchanged(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.record_artifact(
        "claim-c1", content_hash=hash_factory(1), depends_on=(), actor=actor
    )
    store.record_artifact(
        "proposal-g1",
        content_hash=hash_factory(3),
        depends_on=("claim-c1",),
        actor=actor,
    )
    # Re-record the claim with the SAME hash: no stale targets emitted.
    record = store.record_artifact(
        "claim-c1", content_hash=hash_factory(1), depends_on=(), actor=actor
    )
    assert record.stale_targets == ()
    assert store.stale_nodes == ()


def test_independent_branch_is_not_staled(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    # Two independent chains.
    store.record_artifact(
        "claim-a", content_hash=hash_factory(1), depends_on=(), actor=actor
    )
    store.record_artifact(
        "proposal-a",
        content_hash=hash_factory(2),
        depends_on=("claim-a",),
        actor=actor,
    )
    store.record_artifact(
        "claim-b", content_hash=hash_factory(3), depends_on=(), actor=actor
    )
    store.record_artifact(
        "proposal-b",
        content_hash=hash_factory(4),
        depends_on=("claim-b",),
        actor=actor,
    )

    record = store.record_artifact(
        "claim-a", content_hash=hash_factory(11), depends_on=(), actor=actor
    )
    assert record.stale_targets == ("proposal-a",)
    assert "proposal-b" not in record.stale_targets


def test_compute_stale_is_transitive_and_excludes_changed_nodes(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    _seed_dependency_chain(store, actor=actor, hash_factory=hash_factory)

    stale = store.compute_stale("verification-g1")
    assert stale == ("approval-g1", "proposal-g1")
    assert "verification-g1" not in stale
    assert "claim-c1" not in stale


def test_invalid_node_id_and_hash_are_rejected(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    with pytest.raises(SessionError):
        store.record_artifact(
            "UPPERCASE", content_hash=hash_factory(1), actor=actor
        )
    with pytest.raises(SessionError):
        store.record_artifact(
            "claim-x", content_hash="not-a-hash", actor=actor
        )
    with pytest.raises(SessionError):
        store.record_artifact(
            "claim-x",
            content_hash=hash_factory(1),
            depends_on=("Bad Dep",),
            actor=actor,
        )


def test_revise_charter_rejects_immutable_session_id_change(
    session_root: Path,
    make_charter,  # type: ignore[no-untyped-def]
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    revised = make_charter(session_id="session-other-002")
    with pytest.raises(SessionError):
        store.revise_charter(revised, actor=actor)


def test_staleness_survives_reopen_from_journal(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    _seed_dependency_chain(store, actor=actor, hash_factory=hash_factory)
    store.record_artifact(
        "claim-c1", content_hash=hash_factory(9), depends_on=(), actor=actor
    )
    assert store.stale_nodes == (
        "approval-g1",
        "proposal-g1",
        "verification-g1",
    )

    reopened = SessionStore.open(session_root)
    assert isinstance(reopened.reconstruct(), SessionSnapshot)
    assert reopened.stale_nodes == (
        "approval-g1",
        "proposal-g1",
        "verification-g1",
    )


def test_session_snapshot_exposes_reconstructed_view(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    snapshot = store.reconstruct()
    assert isinstance(snapshot, SessionSnapshot)
    assert snapshot.state is SessionState.INITIALIZED
    assert snapshot.charter == charter
    assert snapshot.events  # genesis present
    assert all(isinstance(event, SessionEvent) for event in snapshot.events)


# --------------------------------------------------------------------------- #
# Review-fix regression tests                                                 #
#                                                                             #
# These pin the four hardening fixes layered on top of the Task 7 contract:   #
# committed-head trailing-deletion detection, replay-time state-machine        #
# enforcement for hash-valid journals, recursive text/size payload bounds on   #
# append and replay, SessionCharter field bounds, and the session.lock 0600   #
# file mode.                                                                  #
# --------------------------------------------------------------------------- #


def test_deletion_of_complete_trailing_event_is_detected(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)
    store.transition(SessionState.SAMPLE_POLICY_PENDING, actor=actor)

    lines = _journal_lines(session_root)
    assert len(lines) == 3
    # Remove the last *complete* trailing event. The two remaining events still
    # form an intact hash chain, so without the committed-head sidecar this
    # would silently reconstruct as a shorter-but-valid session.
    del lines[-1]
    _rewrite_journal(session_root, lines)

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_committed_head_detects_equal_count_head_mismatch(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)

    # The committed-head sidecar now records count=2, head=H_intake. Replace the
    # intake event with a different hash-valid transition to the same state
    # (extra payload field) so the rebuilt count stays 2 but the head differs.
    lines = _journal_lines(session_root)
    genesis_line = lines[0]
    genesis_hash = json.loads(genesis_line)["event_hash"]
    alt_line = _event_line(
        genesis_hash,
        "state_transition",
        {"target": "intake", "note": "alternate content"},
        actor=actor,
    )
    _rewrite_journal(session_root, [genesis_line, alt_line])

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_hash_valid_skipped_transition_replay_is_rejected(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    genesis_line = _journal_lines(session_root)[0]
    genesis_hash = json.loads(genesis_line)["event_hash"]

    intake_line = _event_line(
        genesis_hash, "state_transition", {"target": "intake"}, actor=actor
    )
    intake_hash = json.loads(intake_line)["event_hash"]
    # A direct skip from INTAKE to DRAFTING (not an allowed transition), each
    # event individually hash-valid so only the replayed state-machine guard
    # can reject it.
    skip_line = _event_line(
        intake_hash, "state_transition", {"target": "drafting"}, actor=actor
    )
    _rewrite_journal(session_root, [genesis_line, intake_line, skip_line])

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_hash_valid_event_after_close_replay_is_rejected(
    session_root: Path,
    charter: SessionCharter,
 actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    genesis_line = _journal_lines(session_root)[0]
    genesis_hash = json.loads(genesis_line)["event_hash"]

    close_line = _event_line(
        genesis_hash, "closed", {"target": "closed"}, actor=actor
    )
    close_hash = json.loads(close_line)["event_hash"]
    # A transition appended after the terminal close event; the journal is
    # hash-valid, so only the replayed "event after close" guard rejects it.
    after_line = _event_line(
        close_hash, "state_transition", {"target": "intake"}, actor=actor
    )
    _rewrite_journal(session_root, [genesis_line, close_line, after_line])

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_oversized_text_field_is_rejected_on_append(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    oversized = "x" * (MAX_TEXT_LENGTH + 1)
    with pytest.raises(SessionError):
        store.transition(
            SessionState.INTAKE, actor=actor, payload={"note": oversized}
        )
    # The rejected transition did not mutate the journal.
    assert len(_journal_lines(session_root)) == 1


def test_oversized_serialized_payload_is_rejected_on_append(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    # Many individually-bounded text fields (each exactly at the text bound) so
    # the recursive text guard passes but the aggregate serialization exceeds
    # the payload byte limit.
    fields = {f"field_{i}": "x" * MAX_TEXT_LENGTH for i in range(17)}
    assert len(json.dumps(fields).encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES
    with pytest.raises(SessionError):
        store.transition(SessionState.INTAKE, actor=actor, payload=fields)
    assert len(_journal_lines(session_root)) == 1


def test_oversized_text_field_is_rejected_on_replay(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    genesis_line = _journal_lines(session_root)[0]
    genesis_hash = json.loads(genesis_line)["event_hash"]
    oversized = "x" * (MAX_TEXT_LENGTH + 1)
    bad_line = _event_line(
        genesis_hash,
        "state_transition",
        {"target": "intake", "note": oversized},
        actor=actor,
    )
    _rewrite_journal(session_root, [genesis_line, bad_line])

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_oversized_serialized_payload_is_rejected_on_replay(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    genesis_line = _journal_lines(session_root)[0]
    genesis_hash = json.loads(genesis_line)["event_hash"]
    fields = {f"field_{i}": "x" * MAX_TEXT_LENGTH for i in range(17)}
    fields["target"] = "intake"
    bad_line = _event_line(
        genesis_hash, "state_transition", fields, actor=actor
    )
    _rewrite_journal(session_root, [genesis_line, bad_line])

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()


def test_charter_rejects_oversized_business_question(
    make_charter,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(SessionError, match="invalid_charter"):
        make_charter(business_question="q" * (MAX_TEXT_LENGTH + 1))


def test_charter_rejects_oversized_approver(
    make_charter,  # type: ignore[no-untyped-def]
) -> None:
    # No whitespace to collapse, so the normalized form stays oversized.
    with pytest.raises(SessionError, match="invalid_charter"):
        make_charter(approver="a" * (MAX_TEXT_LENGTH + 1))


@pytest.mark.parametrize(
    "collection", ["inclusions", "exclusions", "acceptance_questions"]
)
def test_charter_rejects_oversized_collection_item(
    make_charter,  # type: ignore[no-untyped-def]
    collection: str,
) -> None:
    with pytest.raises(SessionError, match="invalid_charter"):
        make_charter(**{collection: ("x" * (MAX_TEXT_LENGTH + 1),)})


def test_charter_accepts_text_at_the_bound(
    make_charter,  # type: ignore[no-untyped-def]
) -> None:
    # Exactly MAX_TEXT_LENGTH is allowed (the bound is inclusive).
    charter = make_charter(business_question="q" * MAX_TEXT_LENGTH)
    assert len(charter.business_question) == MAX_TEXT_LENGTH


@posix_only
def test_session_lock_file_is_mode_0600_while_locked(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    with store.lock():
        lock_path = session_root / "session.lock"
        assert lock_path.exists()
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- #
# Fail-closed committed-head sidecar + mapping-key bounds regression tests   #
# --------------------------------------------------------------------------- #


def test_missing_committed_head_sidecar_fails_closed(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)
    # The journal is non-empty; a missing sidecar proves the trailing event
    # (and its sidecar) were removed. Fail closed rather than silently accept.
    (session_root / "committed_head").unlink()
    with pytest.raises(SessionError, match="committed head missing"):
        SessionStore.open(session_root)


def test_corrupting_committed_head_sidecar_fails_closed(
    session_root: Path,
    charter: SessionCharter,
 actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    store.transition(SessionState.INTAKE, actor=actor)
    # A corrupt (unparseable) sidecar is indistinguishable from a removed one:
    # _read_committed_head returns None, so fail closed.
    (session_root / "committed_head").write_text("not-json", encoding="utf-8")
    with pytest.raises(SessionError, match="committed head missing"):
        SessionStore.open(session_root)


def test_oversized_mapping_key_is_rejected_on_append(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    store = SessionStore.create(session_root, charter=charter, actor=actor)
    oversized_key = "k" * (MAX_TEXT_LENGTH + 1)
    with pytest.raises(SessionError):
        store.transition(
            SessionState.INTAKE,
            actor=actor,
            payload={"nested": {"items": {oversized_key: "value"}}},
        )
    # The rejected transition did not mutate the journal.
    assert len(_journal_lines(session_root)) == 1


def test_oversized_mapping_key_is_rejected_on_replay(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    SessionStore.create(session_root, charter=charter, actor=actor)
    genesis_line = _journal_lines(session_root)[0]
    genesis_hash = json.loads(genesis_line)["event_hash"]
    oversized_key = "k" * (MAX_TEXT_LENGTH + 1)
    bad_line = _event_line(
        genesis_hash,
        "state_transition",
        {
            "target": "intake",
            "nested": {"items": {oversized_key: "value"}},
        },
        actor=actor,
    )
    _rewrite_journal(session_root, [genesis_line, bad_line])

    with pytest.raises(SessionError, match="integrity"):
        SessionStore.open(session_root).reconstruct()
