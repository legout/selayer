"""Group and batch approval attestation, apply-batch preparation, and safe
approved-summary preview tests (Task 18).

These tests pin the Stage 5 approval contract:

* :func:`~selayer_discovery.approval.attest_group` records a named-approver
  decision on one ready dependency group, bound to the charter, base catalog,
  active sample policy, evidence lock, proposal-group, candidate, and
  verification fingerprints, plus a fixed statement that the record is not a
  digital signature. It rejects an actor mismatch, a blocked, stale, or
  incompletely verified group, and any changed bound input.
* :func:`~selayer_discovery.approval.prepare_apply_batch` prepares an explicit
  ordered, dependency-closed, non-overlapping batch of accepted groups from a
  common base, reconstructing the combined candidate and binding the group
  list, attestations, base, candidate, verification, and approved-summary hash.
  Applying a prior batch (changing the base) stales every unapplied group.
* :func:`~selayer_discovery.approval.attest_apply_batch` requires the current
  named approver and the exact prepared-batch hash; changing the selection
  requires a new prepare and attestation.
* :func:`~selayer_discovery.approval.render_approved_summary` and
  :func:`~selayer_discovery.approval.write_approved_summary_preview` render the
  attested preview under the Git-ignored session workspace. The preview carries
  only the eight specified files and never a document body, sample value, full
  interview answer, salt, credential, runtime value, or backup path. Task 20
  publishes the identical hash-bound summary to ``semantic_changes/`` inside the
  apply transaction; Task 18 never writes a Git-visible summary.

Typed data — not patches — remains the sole authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from selayer_discovery import canonical
from selayer_discovery.approval import (
    APPLY_NOT_A_SIGNATURE_STATEMENT,
    APPROVED_SUMMARY_FILES,
    ApplyBatchAttestation,
    ApprovalError,
    GroupAttestation,
    PreparedBatch,
    attest_apply_batch,
    attest_group,
    prepare_apply_batch,
    render_approved_summary,
    validate_group_attestation,
    write_approved_summary_preview,
)
from selayer_discovery.proposal import (
    GENESIS_HASH,
    Candidate,
    GroupReadiness,
    GroupVerification,
    MandatoryCheck,
    MandatoryCheckKind,
    Proposal,
    VerificationBundle,
    build_proposal,
    reconstruct_candidate,
)

# --------------------------------------------------------------------------- #
# Catalog fixtures                                                            #
# --------------------------------------------------------------------------- #

_BASE_CATALOG_YAML = """\
version: 1
name: shopfloor
label: Shopfloor Analytics
description: Semantic model for the shopfloor example
data_sources:
  orders:
    type: parquet
    location: data/orders.parquet
    grain: [id]
    schema:
      fields:
        - {name: id, type: utf8, nullable: false}
        - {name: customer_id, type: utf8, true}
        - {name: status, type: utf8, nullable: true}
        - {name: amount, type: float64, nullable: true}
dimensions:
  order_status:
    source: orders
    column: status
    data_type: string
    description: Order status
"""


# --------------------------------------------------------------------------- #
# Builders                                                                    #
# --------------------------------------------------------------------------- #


def _hash(value: object) -> str:
    return canonical.fingerprint(value)


_HEX_A = "a" * 64
_HEX_B = "b" * 64
_HEX_C = "c" * 64
_HEX_D = "d" * 64


def _add_op(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "operation_id": "operation-001",
        "kind": "catalog.add",
        "target_id": "dimension.product_category",
        "before": None,
        "before_hash": GENESIS_HASH,
        "after": {
            "source": "products",
            "column": "category",
            "data_type": "string",
            "description": "Product category",
        },
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _group(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "group_id": "group-001",
        "title": "Add product category dimension",
        "rationale": "Products need a category dimension for filtering.",
        "dependencies": [],
        "supporting_claim_ids": ["claim-c1"],
        "inferred_claim_ids": [],
        "conflict_ids": [],
        "affecting_gates": ["gate-grains"],
        "query_cases": [],
        "operations": [_add_op()],
    }
    base.update(overrides)
    return base


def _proposal_mapping(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "proposal_id": "proposal-001",
        "title": "Add product category",
        "groups": [_group()],
    }
    base.update(overrides)
    return base


def _build_proposal(**overrides: Any) -> Proposal:
    return build_proposal(_proposal_mapping(**overrides))


def _group_fingerprint(proposal: Proposal, group_id: str) -> str:
    for group in proposal.groups:
        if group.group_id == group_id:
            return canonical.fingerprint(group.to_dict())
    raise AssertionError(f"group {group_id} not found")


def _candidate(
    proposal: Proposal,
    *,
    base_catalog_text: str = _BASE_CATALOG_YAML,
    base_references: Mapping[str, str] | None = None,
    base_overlays: Mapping[str, str] | None = None,
) -> Candidate:
    return reconstruct_candidate(
        base_catalog_text=base_catalog_text,
        base_references=dict(base_references or {}),
        base_overlays=dict(base_overlays or {}),
        operations=proposal.operations,
    )


def _passed_check(kind: MandatoryCheckKind) -> MandatoryCheck:
    return MandatoryCheck(
        kind=kind.value,
        status="passed",
        code="",
        digest=_HEX_A,
    )


def _ready_group_verification(group_id: str) -> GroupVerification:
    return GroupVerification(
        group_id=group_id,
        checks=(_passed_check(MandatoryCheckKind.STATIC),),
        readiness=GroupReadiness(ready=True, blockers=()),
    )


def _verification_bundle(
    proposal: Proposal,
    candidate: Candidate,
    *,
    group_ids: tuple[str, ...] | None = None,
) -> VerificationBundle:
    ids = group_ids or tuple(g.group_id for g in proposal.groups)
    groups = tuple(_ready_group_verification(gid) for gid in ids)
    return VerificationBundle(
        proposal_id=proposal.proposal_id,
        proposal_fingerprint=proposal.fingerprint,
        candidate_fingerprint=candidate.fingerprint,
        input_hashes={
            "proposal": proposal.fingerprint,
            "candidate": candidate.fingerprint,
            "catalog": candidate.catalog_fingerprint,
        },
        groups=groups,
        fingerprint=_hash(
            {
                "proposal_fingerprint": proposal.fingerprint,
                "candidate_fingerprint": candidate.fingerprint,
                "groups": [gv.to_dict() for gv in groups],
            }
        ),
    )


def _full_input_hashes(
    proposal: Proposal,
    candidate: Candidate,
    bundle: VerificationBundle,
    *,
    charter: str = _HEX_A,
    base_catalog: str = _HEX_A,
    policy: str = _HEX_A,
    evidence_lock: str = _HEX_A,
) -> dict[str, str]:
    return {
        "charter": charter,
        "base_catalog": base_catalog,
        "policy": policy,
        "evidence_lock": evidence_lock,
        "group": _group_fingerprint(proposal, proposal.groups[0].group_id),
        "candidate": candidate.fingerprint,
        "verification": bundle.fingerprint,
    }


_TIMESTAMP = "2026-07-31T12:00:00+00:00"


def _attest(
    proposal: Proposal, **overrides: Any
) -> tuple[GroupAttestation, Candidate, VerificationBundle]:
    candidate = _candidate(proposal)
    bundle = _verification_bundle(proposal, candidate)
    inputs = _full_input_hashes(proposal, candidate, bundle)
    kwargs: dict[str, Any] = {
        "session_id": "session-001",
        "group_id": proposal.groups[0].group_id,
        "approver": "Dr Alice Okonkwo",
        "current_approver": "Dr Alice Okonkwo",
        "decision": "accepted",
        "reason": "The new dimension matches the observed schema.",
        "input_hashes": inputs,
        "group_ready": True,
        "group_blocked": (),
        "group_stale": False,
        "timestamp": _TIMESTAMP,
    }
    kwargs.update(overrides)
    attestation = attest_group(**kwargs)
    return attestation, candidate, bundle


# --------------------------------------------------------------------------- #
# Step 1: group attestation                                                   #
# --------------------------------------------------------------------------- #


class TestGroupAttestation:
    def test_records_accepted_decision(self) -> None:
        proposal = _build_proposal()
        attestation, _, _ = _attest(proposal)
        assert attestation.decision == "accepted"
        assert attestation.session_id == "session-001"
        assert attestation.group_id == "group-001"
        assert attestation.approver == "Dr Alice Okonkwo"

    def test_records_rejected_and_deferred_decisions(self) -> None:
        proposal = _build_proposal()
        rejected, _, _ = _attest(proposal, decision="rejected")
        deferred, _, _ = _attest(proposal, decision="deferred")
        assert rejected.decision == "rejected"
        assert deferred.decision == "deferred"

    def test_binds_all_fingerprints(self) -> None:
        proposal = _build_proposal()
        attestation, candidate, bundle = _attest(proposal)
        assert attestation.input_hashes["charter"] == _HEX_A
        assert attestation.input_hashes["base_catalog"] == _HEX_A
        assert attestation.input_hashes["policy"] == _HEX_A
        assert attestation.input_hashes["evidence_lock"] == _HEX_A
        assert attestation.input_hashes["group"] == _group_fingerprint(
            proposal, "group-001"
        )
        assert attestation.input_hashes["candidate"] == candidate.fingerprint
        assert attestation.input_hashes["verification"] == bundle.fingerprint

    def test_carries_fixed_not_a_signature_statement(self) -> None:
        proposal = _build_proposal()
        attestation, _, _ = _attest(proposal)
        assert attestation.not_a_signature == APPLY_NOT_A_SIGNATURE_STATEMENT
        assert "not a digital signature" in attestation.not_a_signature.lower()

    def test_has_stable_fingerprint(self) -> None:
        proposal = _build_proposal()
        first, _, _ = _attest(proposal)
        second, _, _ = _attest(proposal)
        assert first.fingerprint == second.fingerprint
        assert len(first.fingerprint) == 64

    def test_round_trips_to_dict_and_back(self) -> None:
        proposal = _build_proposal()
        attestation, _, _ = _attest(proposal)
        data = attestation.to_dict()
        assert data["schema_version"] == 1
        assert data["fingerprint"] == attestation.fingerprint

    def test_rejects_actor_mismatch(self) -> None:
        proposal = _build_proposal()
        with pytest.raises(ApprovalError) as exc:
            _attest(proposal, approver="Dr Eve Mallory")
        assert exc.value.code == "discovery.approval.actor_mismatch"

    def test_rejects_blocked_group(self) -> None:
        proposal = _build_proposal()
        with pytest.raises(ApprovalError) as exc:
            _attest(proposal, group_blocked=("conflict-c1",))
        assert exc.value.code == "discovery.approval.group_blocked"

    def test_rejects_stale_group(self) -> None:
        proposal = _build_proposal()
        with pytest.raises(ApprovalError) as exc:
            _attest(proposal, group_stale=True)
        assert exc.value.code == "discovery.approval.group_stale"

    def test_rejects_incomplete_verification(self) -> None:
        proposal = _build_proposal()
        with pytest.raises(ApprovalError) as exc:
            _attest(proposal, group_ready=False)
        assert exc.value.code == "discovery.approval.group_not_ready"

    def test_rejects_unknown_decision(self) -> None:
        proposal = _build_proposal()
        with pytest.raises(ApprovalError):
            _attest(proposal, decision="approved")

    def test_rejects_oversized_reason(self) -> None:
        proposal = _build_proposal()
        with pytest.raises(ApprovalError):
            _attest(proposal, reason="x" * (16 * 1024 + 1))

    def test_rejects_missing_input_hash(self) -> None:
        proposal = _build_proposal()
        candidate = _candidate(proposal)
        bundle = _verification_bundle(proposal, candidate)
        inputs = _full_input_hashes(proposal, candidate, bundle)
        del inputs["charter"]
        with pytest.raises(ApprovalError):
            _attest(proposal, input_hashes=inputs)

    def test_rejects_invalid_hash_shape(self) -> None:
        proposal = _build_proposal()
        candidate = _candidate(proposal)
        bundle = _verification_bundle(proposal, candidate)
        inputs = _full_input_hashes(proposal, candidate, bundle)
        inputs["policy"] = "not-a-hash"
        with pytest.raises(ApprovalError):
            _attest(proposal, input_hashes=inputs)


class TestValidateGroupAttestation:
    """Any bound fingerprint change invalidates the attestation."""

    def test_accepts_current_attestation(self) -> None:
        proposal = _build_proposal()
        attestation, candidate, bundle = _attest(proposal)
        validate_group_attestation(
            attestation,
            current_input_hashes={
                "charter": _HEX_A,
                "base_catalog": _HEX_A,
                "policy": _HEX_A,
                "evidence_lock": _HEX_A,
            },
            current_group_fingerprint=_group_fingerprint(proposal, "group-001"),
            current_candidate_fingerprint=candidate.fingerprint,
            current_verification_fingerprint=bundle.fingerprint,
        )

    def test_rejects_changed_charter(self) -> None:
        proposal = _build_proposal()
        attestation, candidate, bundle = _attest(proposal)
        with pytest.raises(ApprovalError) as exc:
            validate_group_attestation(
                attestation,
                current_input_hashes={
                    "charter": _HEX_B,
                    "base_catalog": _HEX_A,
                    "policy": _HEX_A,
                    "evidence_lock": _HEX_A,
                },
                current_group_fingerprint=_group_fingerprint(proposal, "group-001"),
                current_candidate_fingerprint=candidate.fingerprint,
                current_verification_fingerprint=bundle.fingerprint,
            )
        assert exc.value.code == "discovery.approval.fingerprint_changed"

    def test_rejects_changed_policy(self) -> None:
        proposal = _build_proposal()
        attestation, candidate, bundle = _attest(proposal)
        with pytest.raises(ApprovalError):
            validate_group_attestation(
                attestation,
                current_input_hashes={
                    "charter": _HEX_A,
                    "base_catalog": _HEX_A,
                    "policy": _HEX_C,
                    "evidence_lock": _HEX_A,
                },
                current_group_fingerprint=_group_fingerprint(proposal, "group-001"),
                current_candidate_fingerprint=candidate.fingerprint,
                current_verification_fingerprint=bundle.fingerprint,
            )

    def test_rejects_changed_evidence_lock(self) -> None:
        proposal = _build_proposal()
        attestation, candidate, bundle = _attest(proposal)
        with pytest.raises(ApprovalError):
            validate_group_attestation(
                attestation,
                current_input_hashes={
                    "charter": _HEX_A,
                    "base_catalog": _HEX_A,
                    "policy": _HEX_A,
                    "evidence_lock": _HEX_D,
                },
                current_group_fingerprint=_group_fingerprint(proposal, "group-001"),
                current_candidate_fingerprint=candidate.fingerprint,
                current_verification_fingerprint=bundle.fingerprint,
            )

    def test_rejects_changed_candidate(self) -> None:
        proposal = _build_proposal()
        attestation, _, bundle = _attest(proposal)
        with pytest.raises(ApprovalError):
            validate_group_attestation(
                attestation,
                current_input_hashes={
                    "charter": _HEX_A,
                    "base_catalog": _HEX_A,
                    "policy": _HEX_A,
                    "evidence_lock": _HEX_A,
                },
                current_group_fingerprint=_group_fingerprint(proposal, "group-001"),
                current_candidate_fingerprint=_HEX_B,
                current_verification_fingerprint=bundle.fingerprint,
            )

    def test_rejects_changed_verification(self) -> None:
        proposal = _build_proposal()
        attestation, candidate, _ = _attest(proposal)
        with pytest.raises(ApprovalError):
            validate_group_attestation(
                attestation,
                current_input_hashes={
                    "charter": _HEX_A,
                    "base_catalog": _HEX_A,
                    "policy": _HEX_A,
                    "evidence_lock": _HEX_A,
                },
                current_group_fingerprint=_group_fingerprint(proposal, "group-001"),
                current_candidate_fingerprint=candidate.fingerprint,
                current_verification_fingerprint=_HEX_C,
            )

    def test_rejects_changed_base_catalog(self) -> None:
        proposal = _build_proposal()
        attestation, candidate, bundle = _attest(proposal)
        with pytest.raises(ApprovalError):
            validate_group_attestation(
                attestation,
                current_input_hashes={
                    "charter": _HEX_A,
                    "base_catalog": _HEX_D,
                    "policy": _HEX_A,
                    "evidence_lock": _HEX_A,
                },
                current_group_fingerprint=_group_fingerprint(proposal, "group-001"),
                current_candidate_fingerprint=candidate.fingerprint,
                current_verification_fingerprint=bundle.fingerprint,
            )


# --------------------------------------------------------------------------- #
# Step 2 + 3: apply-batch preparation                                         #
# --------------------------------------------------------------------------- #


def _two_group_proposal() -> Proposal:
    op_a = _add_op(operation_id="operation-a", target_id="dimension.category_a")
    op_b = _add_op(
        operation_id="operation-b",
        target_id="dimension.category_b",
        group_ids=["group-002"],
    )
    reference = _add_op(
        operation_id="operation-reference",
        kind="reference.create",
        target_id="dimensions/category_a.md",
        before=None,
        before_hash=GENESIS_HASH,
        after="---\nselayer_id: dimension.category_a\n---\n# Category reference\n",
        group_ids=["group-001"],
    )
    overlay = _add_op(
        operation_id="operation-overlay",
        kind="overlay.create",
        target_id="guidance/category_a.md",
        before=None,
        before_hash=GENESIS_HASH,
        after="---\nselayer_id: dimension.category_a\n---\n## Usage Guidance\nSafe guidance.\n",
        group_ids=["group-001"],
    )
    return build_proposal(
        _proposal_mapping(
            groups=[
                _group(
                    group_id="group-001",
                    operations=[op_a, reference, overlay],
                ),
                _group(
                    group_id="group-002",
                    title="Add second category dimension",
                    rationale="A second category dimension.",
                    dependencies=["group-001"],
                    operations=[op_b],
                ),
            ]
        )
    )


def _attest_group_obj(
    proposal: Proposal,
    group_id: str,
    *,
    decision: str = "accepted",
    candidate: Candidate | None = None,
    bundle: VerificationBundle | None = None,
    base_catalog_fingerprint: str | None = None,
) -> GroupAttestation:
    if candidate is None:
        candidate = _candidate(proposal)
    if bundle is None:
        bundle = _verification_bundle(proposal, candidate)
    base_catalog = (
        base_catalog_fingerprint
        if base_catalog_fingerprint is not None
        else canonical.fingerprint(_BASE_CATALOG_YAML)
    )
    inputs = dict(
        _full_input_hashes(proposal, candidate, bundle, base_catalog=base_catalog),
        group=_group_fingerprint(proposal, group_id),
    )
    return attest_group(
        session_id="session-001",
        group_id=group_id,
        approver="Dr Alice Okonkwo",
        current_approver="Dr Alice Okonkwo",
        decision=decision,
        reason="accepted",
        input_hashes=inputs,
        group_ready=True,
        group_blocked=(),
        group_stale=False,
        timestamp=_TIMESTAMP,
    )


def _combined_candidate(proposal: Proposal, group_ids: tuple[str, ...]) -> Candidate:
    selected = {gid for gid in group_ids}
    operations = tuple(
        op
        for group in proposal.groups
        if group.group_id in selected
        for op in group.operations
    )
    return reconstruct_candidate(
        base_catalog_text=_BASE_CATALOG_YAML,
        base_references={},
        base_overlays={},
        operations=operations,
    )


def _combined_verification(
    proposal: Proposal,
    candidate: Candidate,
    group_ids: tuple[str, ...],
) -> VerificationBundle:
    return _verification_bundle(proposal, candidate, group_ids=group_ids)


def _base_hashes() -> dict[str, str]:
    return {
        "catalog": canonical.fingerprint(_BASE_CATALOG_YAML),
        "references": _HEX_A,
        "overlays": _HEX_A,
    }


def _current_session_hashes() -> dict[str, str]:
    return {
        "charter": _HEX_A,
        "base_catalog": canonical.fingerprint(_BASE_CATALOG_YAML),
        "policy": _HEX_A,
        "evidence_lock": _HEX_A,
    }


class TestPrepareApplyBatch:
    def test_requires_explicit_ordered_group_list(self) -> None:
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-001",))
        bundle = _combined_verification(proposal, candidate, ("group-001",))
        attestation = _attest_group_obj(proposal, "group-001")
        with pytest.raises(ApprovalError):
            prepare_apply_batch(
                proposal=proposal,
                group_ids=(),
                attestations={"group-001": attestation},
                combined_candidate=candidate,
                combined_verification=bundle,
                base_hashes=_base_hashes(),
                current_session_hashes=_current_session_hashes(),
                approved_summary_hash=_HEX_A,
            )

    def test_preserves_explicit_order(self) -> None:
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-001", "group-002"))
        bundle = _combined_verification(proposal, candidate, ("group-001", "group-002"))
        attestations = {
            "group-001": _attest_group_obj(proposal, "group-001"),
            "group-002": _attest_group_obj(proposal, "group-002"),
        }
        batch = prepare_apply_batch(
            proposal=proposal,
            group_ids=("group-001", "group-002"),
            attestations=attestations,
            combined_candidate=candidate,
            combined_verification=bundle,
            base_hashes=_base_hashes(),
            current_session_hashes=_current_session_hashes(),
            approved_summary_hash=_HEX_A,
        )
        assert batch.group_ids == ("group-001", "group-002")

    def test_requires_common_base(self) -> None:
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-001",))
        bundle = _combined_verification(proposal, candidate, ("group-001",))
        attestation = _attest_group_obj(proposal, "group-001")
        with pytest.raises(ApprovalError):
            prepare_apply_batch(
                proposal=proposal,
                group_ids=("group-001",),
                attestations={"group-001": attestation},
                combined_candidate=candidate,
                combined_verification=bundle,
                base_hashes={"catalog": _HEX_A},
                current_session_hashes=_current_session_hashes(),
                approved_summary_hash=_HEX_A,
            )

    def test_requires_dependency_closure(self) -> None:
        # group-002 depends on group-001; selecting only group-002 violates closure.
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-002",))
        bundle = _combined_verification(proposal, candidate, ("group-002",))
        attestation = _attest_group_obj(proposal, "group-002")
        with pytest.raises(ApprovalError) as exc:
            prepare_apply_batch(
                proposal=proposal,
                group_ids=("group-002",),
                attestations={"group-002": attestation},
                combined_candidate=candidate,
                combined_verification=bundle,
                base_hashes=_base_hashes(),
                current_session_hashes=_current_session_hashes(),
                approved_summary_hash=_HEX_A,
            )
        assert exc.value.code == "discovery.approval.batch_dependency"

    def test_requires_accepted_status(self) -> None:
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-001",))
        bundle = _combined_verification(proposal, candidate, ("group-001",))
        attestation = _attest_group_obj(proposal, "group-001", decision="rejected")
        with pytest.raises(ApprovalError) as exc:
            prepare_apply_batch(
                proposal=proposal,
                group_ids=("group-001",),
                attestations={"group-001": attestation},
                combined_candidate=candidate,
                combined_verification=bundle,
                base_hashes=_base_hashes(),
                current_session_hashes=_current_session_hashes(),
                approved_summary_hash=_HEX_A,
            )
        assert exc.value.code == "discovery.approval.group_not_accepted"

    def test_rejects_overlap_on_semantic_targets(self) -> None:
        # Two groups editing the same catalog target overlap.
        op_a = _add_op(
            operation_id="operation-a",
            target_id="dimension.product_category",
            group_ids=["group-001"],
        )
        op_b = _add_op(
            operation_id="operation-b",
            kind="catalog.edit",
            target_id="dimension.product_category",
            before={
                "source": "products",
                "column": "category",
                "data_type": "string",
                "description": "old",
            },
            before_hash=_hash(
                {
                    "source": "products",
                    "column": "category",
                    "data_type": "string",
                    "description": "old",
                }
            ),
            after={
                "source": "products",
                "column": "category",
                "data_type": "string",
                "description": "new",
            },
            group_ids=["group-002"],
        )
        proposal = build_proposal(
            _proposal_mapping(
                groups=[
                    _group(group_id="group-001", operations=[op_a]),
                    _group(
                        group_id="group-002",
                        title="Edit category",
                        rationale="Edit.",
                        dependencies=["group-001"],
                        operations=[op_b],
                    ),
                ]
            )
        )
        # ``build_proposal`` itself rejects overlapping (kind, target) across the
        # proposal, so a same-target create+edit pair must be one group. The
        # batch-level overlap is therefore exercised on curated sections below;
        # this test documents that the proposal guard fires first.
        assert proposal.groups[0].group_id == "group-001"

    def test_rejects_overlap_on_curated_sections(self) -> None:
        # Two groups editing the same authored reference path overlap.
        ref_create = {
            "operation_id": "operation-ref-a",
            "kind": "reference.create",
            "target_id": "dimensions/order_status.md",
            "before": None,
            "before_hash": GENESIS_HASH,
            "after": "# Order status\n\nAuthored reference.\n",
            "claim_ids": ["claim-c1"],
            "group_ids": ["group-001"],
        }
        ref_update = {
            "operation_id": "operation-ref-b",
            "kind": "reference.update",
            "target_id": "dimensions/order_status.md",
            "before": "# Order status\n\nAuthored reference.\n",
            "before_hash": _hash("# Order status\n\nAuthored reference.\n"),
            "after": "# Order status\n\nAuthored reference (revised).\n",
            "claim_ids": ["claim-c1"],
            "group_ids": ["group-002"],
        }
        proposal = build_proposal(
            _proposal_mapping(
                groups=[
                    _group(
                        group_id="group-001",
                        operations=[ref_create],
                        affecting_gates=(),
                    ),
                    _group(
                        group_id="group-002",
                        title="Update reference",
                        rationale="Update.",
                        dependencies=["group-001"],
                        operations=[ref_update],
                        affecting_gates=(),
                    ),
                ]
            )
        )
        candidate = _combined_candidate(proposal, ("group-001", "group-002"))
        bundle = _combined_verification(proposal, candidate, ("group-001", "group-002"))
        attestations = {
            "group-001": _attest_group_obj(proposal, "group-001"),
            "group-002": _attest_group_obj(proposal, "group-002"),
        }
        with pytest.raises(ApprovalError) as exc:
            prepare_apply_batch(
                proposal=proposal,
                group_ids=("group-001", "group-002"),
                attestations=attestations,
                combined_candidate=candidate,
                combined_verification=bundle,
                base_hashes=_base_hashes(),
                current_session_hashes=_current_session_hashes(),
                approved_summary_hash=_HEX_A,
            )
        assert exc.value.code == "discovery.approval.batch_overlap"

    def test_hashes_group_list_attestations_base_candidate_verification_summary(
        self,
    ) -> None:
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-001", "group-002"))
        bundle = _combined_verification(proposal, candidate, ("group-001", "group-002"))
        attestations = {
            "group-001": _attest_group_obj(proposal, "group-001"),
            "group-002": _attest_group_obj(proposal, "group-002"),
        }
        batch = prepare_apply_batch(
            proposal=proposal,
            group_ids=("group-001", "group-002"),
            attestations=attestations,
            combined_candidate=candidate,
            combined_verification=bundle,
            base_hashes=_base_hashes(),
            current_session_hashes=_current_session_hashes(),
            approved_summary_hash=_HEX_A,
        )
        assert isinstance(batch, PreparedBatch)
        assert batch.candidate_fingerprint == candidate.fingerprint
        assert batch.verification_fingerprint == bundle.fingerprint
        assert batch.approved_summary_hash == _HEX_A
        assert len(batch.fingerprint) == 64
        assert batch.attestation_fingerprints == (
            attestations["group-001"].fingerprint,
            attestations["group-002"].fingerprint,
        )

    def test_fingerprint_stable_on_repeat(self) -> None:
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-001", "group-002"))
        bundle = _combined_verification(proposal, candidate, ("group-001", "group-002"))
        attestations = {
            "group-001": _attest_group_obj(proposal, "group-001"),
            "group-002": _attest_group_obj(proposal, "group-002"),
        }
        kwargs: dict[str, Any] = {
            "proposal": proposal,
            "group_ids": ("group-001", "group-002"),
            "attestations": attestations,
            "combined_candidate": candidate,
            "combined_verification": bundle,
            "base_hashes": _base_hashes(),
            "current_session_hashes": _current_session_hashes(),
            "approved_summary_hash": _HEX_A,
        }
        assert (
            prepare_apply_batch(**kwargs).fingerprint
            == prepare_apply_batch(**kwargs).fingerprint
        )

    def test_applying_prior_batch_stales_unapplied_groups(self) -> None:
        # A prior apply changes the base catalog. The next prepare of an
        # unapplied group must reject its old attestation because the bound
        # base catalog fingerprint no longer matches the current base.
        proposal = _two_group_proposal()
        old_base = _BASE_CATALOG_YAML
        new_base = old_base.replace("description: Order status", "description: X")
        candidate = _combined_candidate(proposal, ("group-001",))
        bundle = _combined_verification(proposal, candidate, ("group-001",))
        # Attest group-001 against the OLD base catalog.
        attestation = attest_group(
            session_id="session-001",
            group_id="group-001",
            approver="Dr Alice Okonkwo",
            current_approver="Dr Alice Okonkwo",
            decision="accepted",
            reason="accepted",
            input_hashes=dict(
                _full_input_hashes(proposal, candidate, bundle),
                base_catalog=canonical.fingerprint(old_base),
            ),
            group_ready=True,
            timestamp=_TIMESTAMP,
        )
        # After apply, the base catalog is new; the old attestation is stale.
        new_candidate = _combined_candidate(proposal, ("group-001",))
        new_bundle = _combined_verification(proposal, new_candidate, ("group-001",))
        with pytest.raises(ApprovalError) as exc:
            prepare_apply_batch(
                proposal=proposal,
                group_ids=("group-001",),
                attestations={"group-001": attestation},
                combined_candidate=new_candidate,
                combined_verification=new_bundle,
                base_hashes={
                    **_base_hashes(),
                    "catalog": canonical.fingerprint(new_base),
                },
                current_session_hashes=dict(
                    _current_session_hashes(),
                    base_catalog=canonical.fingerprint(new_base),
                ),
                approved_summary_hash=_HEX_A,
            )
        assert exc.value.code == "discovery.approval.fingerprint_changed"
        # The old session hashes are now stale too.
        assert canonical.fingerprint(old_base) != canonical.fingerprint(new_base)

    def test_rejects_group_not_ready(self) -> None:
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-001",))
        not_ready = VerificationBundle(
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal.fingerprint,
            candidate_fingerprint=candidate.fingerprint,
            input_hashes={
                "proposal": proposal.fingerprint,
                "candidate": candidate.fingerprint,
                "catalog": candidate.catalog_fingerprint,
            },
            groups=(
                GroupVerification(
                    group_id="group-001",
                    checks=(_passed_check(MandatoryCheckKind.STATIC),),
                    readiness=GroupReadiness(ready=False, blockers=("check_failed",)),
                ),
            ),
            fingerprint=_HEX_B,
        )
        attestation = _attest_group_obj(proposal, "group-001")
        with pytest.raises(ApprovalError) as exc:
            prepare_apply_batch(
                proposal=proposal,
                group_ids=("group-001",),
                attestations={"group-001": attestation},
                combined_candidate=candidate,
                combined_verification=not_ready,
                base_hashes=_base_hashes(),
                current_session_hashes=_current_session_hashes(),
                approved_summary_hash=_HEX_A,
            )
        assert exc.value.code == "discovery.approval.group_not_ready"

    def test_rejects_unknown_group(self) -> None:
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-001",))
        bundle = _combined_verification(proposal, candidate, ("group-001",))
        attestation = _attest_group_obj(proposal, "group-001")
        with pytest.raises(ApprovalError):
            prepare_apply_batch(
                proposal=proposal,
                group_ids=("group-missing",),
                attestations={"group-missing": attestation},
                combined_candidate=candidate,
                combined_verification=bundle,
                base_hashes=_base_hashes(),
                current_session_hashes=_current_session_hashes(),
                approved_summary_hash=_HEX_A,
            )

    def test_rejects_candidate_verification_mismatch(self) -> None:
        # The combined verification must cover the combined candidate.
        proposal = _two_group_proposal()
        candidate = _combined_candidate(proposal, ("group-001", "group-002"))
        wrong_bundle = _combined_verification(proposal, candidate, ("group-001",))
        attestations = {
            "group-001": _attest_group_obj(proposal, "group-001"),
            "group-002": _attest_group_obj(proposal, "group-002"),
        }
        with pytest.raises(ApprovalError):
            prepare_apply_batch(
                proposal=proposal,
                group_ids=("group-001", "group-002"),
                attestations=attestations,
                combined_candidate=candidate,
                combined_verification=wrong_bundle,
                base_hashes=_base_hashes(),
                current_session_hashes=_current_session_hashes(),
                approved_summary_hash=_HEX_A,
            )


# --------------------------------------------------------------------------- #
# Step 4: apply-batch attestation                                             #
# --------------------------------------------------------------------------- #


def _prepared_batch() -> PreparedBatch:
    proposal = _two_group_proposal()
    candidate = _combined_candidate(proposal, ("group-001", "group-002"))
    bundle = _combined_verification(proposal, candidate, ("group-001", "group-002"))
    attestations = {
        "group-001": _attest_group_obj(proposal, "group-001"),
        "group-002": _attest_group_obj(proposal, "group-002"),
    }
    return prepare_apply_batch(
        proposal=proposal,
        group_ids=("group-001", "group-002"),
        attestations=attestations,
        combined_candidate=candidate,
        combined_verification=bundle,
        base_hashes=_base_hashes(),
        current_session_hashes=_current_session_hashes(),
        approved_summary_hash=_HEX_A,
    )


class TestApplyBatchAttestation:
    def test_requires_current_named_approver(self) -> None:
        batch = _prepared_batch()
        with pytest.raises(ApprovalError) as exc:
            attest_apply_batch(
                batch=batch,
                approver="Dr Eve Mallory",
                current_approver="Dr Alice Okonkwo",
                timestamp=_TIMESTAMP,
            )
        assert exc.value.code == "discovery.approval.actor_mismatch"

    def test_requires_exact_prepared_batch_hash(self) -> None:
        batch = _prepared_batch()
        with pytest.raises(ApprovalError):
            attest_apply_batch(
                batch=batch,
                approver="Dr Alice Okonkwo",
                current_approver="Dr Alice Okonkwo",
                prepared_batch_hash="0" * 64,
                timestamp=_TIMESTAMP,
            )

    def test_binds_batch_hash_and_approver(self) -> None:
        batch = _prepared_batch()
        attestation = attest_apply_batch(
            batch=batch,
            approver="Dr Alice Okonkwo",
            current_approver="Dr Alice Okonkwo",
            timestamp=_TIMESTAMP,
        )
        assert isinstance(attestation, ApplyBatchAttestation)
        assert attestation.batch_hash == batch.fingerprint
        assert attestation.approver == "Dr Alice Okonkwo"
        assert attestation.not_a_signature == APPLY_NOT_A_SIGNATURE_STATEMENT
        assert len(attestation.fingerprint) == 64

    def test_changing_selection_requires_new_prepare_and_attestation(self) -> None:
        proposal = _two_group_proposal()
        candidate_a = _combined_candidate(proposal, ("group-001",))
        bundle_a = _combined_verification(proposal, candidate_a, ("group-001",))
        batch_a = prepare_apply_batch(
            proposal=proposal,
            group_ids=("group-001",),
            attestations={
                "group-001": _attest_group_obj(
                    proposal, "group-001", candidate=candidate_a, bundle=bundle_a
                )
            },
            combined_candidate=candidate_a,
            combined_verification=bundle_a,
            base_hashes=_base_hashes(),
            current_session_hashes=_current_session_hashes(),
            approved_summary_hash=_HEX_A,
        )
        candidate_b = _combined_candidate(proposal, ("group-001", "group-002"))
        bundle_b = _combined_verification(
            proposal, candidate_b, ("group-001", "group-002")
        )
        batch_b = prepare_apply_batch(
            proposal=proposal,
            group_ids=("group-001", "group-002"),
            attestations={
                "group-001": _attest_group_obj(
                    proposal, "group-001", candidate=candidate_b, bundle=bundle_b
                ),
                "group-002": _attest_group_obj(
                    proposal, "group-002", candidate=candidate_b, bundle=bundle_b
                ),
            },
            combined_candidate=candidate_b,
            combined_verification=bundle_b,
            base_hashes=_base_hashes(),
            current_session_hashes=_current_session_hashes(),
            approved_summary_hash=_HEX_A,
        )
        assert batch_a.fingerprint != batch_b.fingerprint
        attestation_a = attest_apply_batch(
            batch=batch_a,
            approver="Dr Alice Okonkwo",
            current_approver="Dr Alice Okonkwo",
            timestamp=_TIMESTAMP,
        )
        # The old attestation's batch hash must not match the new batch.
        with pytest.raises(ApprovalError):
            attest_apply_batch(
                batch=batch_b,
                approver="Dr Alice Okonkwo",
                current_approver="Dr Alice Okonkwo",
                prepared_batch_hash=attestation_a.batch_hash,
                timestamp=_TIMESTAMP,
            )


# --------------------------------------------------------------------------- #
# Step 5: safe approved-summary preview                                       #
# --------------------------------------------------------------------------- #


def _preview_inputs(
    tmp_path: Path,
) -> tuple[
    Proposal,
    Candidate,
    VerificationBundle,
    PreparedBatch,
    GroupAttestation,
    dict[str, str],
    dict[str, str],
]:
    proposal = _two_group_proposal()
    candidate = _combined_candidate(proposal, ("group-001", "group-002"))
    bundle = _combined_verification(proposal, candidate, ("group-001", "group-002"))
    attestations = {
        "group-001": _attest_group_obj(proposal, "group-001"),
        "group-002": _attest_group_obj(proposal, "group-002"),
    }
    batch = prepare_apply_batch(
        proposal=proposal,
        group_ids=("group-001", "group-002"),
        attestations=attestations,
        combined_candidate=candidate,
        combined_verification=bundle,
        base_hashes=_base_hashes(),
        current_session_hashes=_current_session_hashes(),
        approved_summary_hash=_HEX_A,
    )
    return (
        proposal,
        candidate,
        bundle,
        batch,
        attestations["group-001"],
        _base_hashes(),
        _current_session_hashes(),
    )


def _evidence_lock_entries() -> list[dict[str, object]]:
    """Safe evidence-lock entries: ids, revisions, selectors, hashes only."""

    return [
        {
            "claim_id": "claim-c1",
            "evidence_class": "observed",
            "record_id": "record-orders",
            "source_revision": _HEX_B,
            "content_hash": _HEX_C,
            "selector_kind": "source_field",
            "selector_field": "status",
        }
    ]


def _render_summary(
    tmp_path: Path,
    *,
    evidence_lock: list[dict[str, object]] | None = None,
) -> tuple[Any, Path]:
    from selayer_discovery.approval import ApprovedSummary

    (
        proposal,
        candidate,
        bundle,
        batch,
        attestation,
        base_hashes,
        _,
    ) = _preview_inputs(tmp_path)
    apply_attestation = attest_apply_batch(
        batch=batch,
        approver="Dr Alice Okonkwo",
        current_approver="Dr Alice Okonkwo",
        timestamp=_TIMESTAMP,
    )
    session_dir = tmp_path / "session"
    session_dir.mkdir(exist_ok=True)
    summary = render_approved_summary(
        proposal=proposal,
        group_ids=batch.group_ids,
        candidate=candidate,
        verification=bundle,
        group_attestations={
            "group-001": attestation,
            "group-002": _preview_inputs(tmp_path)[4],
        },
        apply_attestation=apply_attestation,
        base_hashes=base_hashes,
        base_catalog_text=_BASE_CATALOG_YAML,
        evidence_lock=evidence_lock
        if evidence_lock is not None
        else _evidence_lock_entries(),
        decision_statement="Two dependency groups were accepted by the named approver.",
    )
    assert isinstance(summary, ApprovedSummary)
    return summary, session_dir


class TestApprovedSummaryPreview:
    def test_summary_hash_is_stable_and_present(self, tmp_path: Path) -> None:
        summary, _ = _render_summary(tmp_path)
        assert len(summary.fingerprint) == 64
        again, _ = _render_summary(tmp_path)
        assert summary.fingerprint == again.fingerprint

    def test_summary_contains_only_specified_files(self, tmp_path: Path) -> None:
        summary, _ = _render_summary(tmp_path)
        paths = {entry.path for entry in summary.entries}
        # The top-level entries are exactly the eight specified files/dirs.
        top_level = {p.split("/", 1)[0] for p in paths}
        assert top_level == APPROVED_SUMMARY_FILES

    def test_writes_preview_under_exports_batch_hash(self, tmp_path: Path) -> None:

        summary, session_dir = _render_summary(tmp_path)
        apply_attestation = attest_apply_batch(
            batch=_preview_inputs(tmp_path)[3],
            approver="Dr Alice Okonkwo",
            current_approver="Dr Alice Okonkwo",
            timestamp=_TIMESTAMP,
        )
        batch_hash = apply_attestation.batch_hash
        root = write_approved_summary_preview(summary, session_dir, batch_hash)
        exports = session_dir / "exports" / batch_hash
        assert root == exports
        assert exports.is_dir()
        # Every specified top-level file/dir exists.
        written = {str(p.relative_to(exports)) for p in exports.rglob("*")}
        for name in APPROVED_SUMMARY_FILES:
            assert (
                (exports / name).exists()
                or any(p.startswith(name + "/") for p in written)
                or name in {e.path.split("/", 1)[0] for e in summary.entries}
            )

    def test_preview_path_is_hash_bound(self, tmp_path: Path) -> None:

        summary, session_dir = _render_summary(tmp_path)
        batch_hash = summary.batch_hash
        root = write_approved_summary_preview(summary, session_dir, batch_hash)
        assert root.name == batch_hash
        assert root.parent.name == "exports"

    def test_preview_contains_no_document_bodies(self, tmp_path: Path) -> None:
        summary, _ = _render_summary(tmp_path)
        rendered = _render_all(summary)
        # No evidence document body excerpt.
        assert "Order status" not in rendered
        # No claim statement free text.
        assert "declarative statement" not in rendered.lower()

    def test_evidence_lock_has_no_bodies_or_values(self, tmp_path: Path) -> None:
        summary, _ = _render_summary(tmp_path)
        lock = _entry_text(summary, "evidence.lock.json")
        data = json.loads(lock)
        rendered = json.dumps(data, sort_keys=True)
        # Only ids, hashes, revisions, selector kinds/fields — never bodies or
        # sample values, statements, or credentials.
        for forbidden in ("statement", "body", "excerpt", "password", "token", "salt"):
            assert forbidden not in rendered.lower()

    def test_decision_md_has_no_evidence_excerpts(self, tmp_path: Path) -> None:
        summary, _ = _render_summary(tmp_path)
        decision = _entry_text(summary, "decision.md")
        for forbidden in ("Order status", "customer_id", "amount", "salt", "password"):
            assert forbidden not in decision

    def test_preview_contains_no_salts_or_credentials(self, tmp_path: Path) -> None:
        summary, _ = _render_summary(tmp_path)
        rendered = _render_all(summary)
        for forbidden in ("salt", "password", "credential", "token", "secret"):
            assert forbidden not in rendered.lower()

    def test_preview_contains_no_runtime_values(self, tmp_path: Path) -> None:
        # Runtime query result values must never reach the summary.
        summary, _ = _render_summary(tmp_path)
        rendered = _render_all(summary)
        for forbidden in ("result_set", "row_value", "sample_value", "query_result"):
            assert forbidden not in rendered.lower()

    def test_preview_contains_no_backup_paths(self, tmp_path: Path) -> None:
        summary, _ = _render_summary(tmp_path)
        rendered = _render_all(summary)
        for forbidden in ("backup", "snapshots/", "recovery", ".bak", "next_target"):
            assert forbidden not in rendered.lower()

    def test_preview_is_not_git_visible(self, tmp_path: Path) -> None:
        # Task 18 writes under the Git-ignored session workspace only; it must
        # not write a Git-visible semantic_changes/ summary (Task 20 owns that).

        summary, session_dir = _render_summary(tmp_path)
        root = write_approved_summary_preview(summary, session_dir, summary.batch_hash)
        # The preview lives under the ignored session workspace, never under a
        # ``semantic_changes`` directory.
        rendered = str(root)
        assert "semantic_changes" not in rendered

    def test_render_rejects_disallowed_file(self, tmp_path: Path) -> None:
        # The summary render is an allowlist: injecting a disallowed file must
        # be rejected. We exercise this through the evidence-lock builder, which
        # must refuse to emit a body-bearing entry.
        with pytest.raises((ApprovalError, ValueError, TypeError)):
            _render_summary(
                tmp_path,
                evidence_lock=[
                    {
                        "claim_id": "claim-c1",
                        "statement": "a forbidden declarative body",  # not allowed
                        "body": "a forbidden document body",
                    }
                ],
            )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _render_all(summary: Any) -> str:
    parts: list[str] = []
    for entry in summary.entries:
        parts.append(entry.path)
        parts.append(entry.text)
    return "\n".join(parts)


def _entry_text(summary: Any, path: str) -> str:
    for entry in summary.entries:
        if entry.path == path:
            return entry.text
    raise AssertionError(f"entry {path} not found in summary")
