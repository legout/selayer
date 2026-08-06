"""Group and batch approval attestations, apply-batch preparation, and safe
approved-summary previews (Stage 5 approval foundation).

This module owns the approval foundation that sits between verification
(Task 17) and the explicit apply transaction (Task 20):

* :class:`GroupAttestation` and :func:`attest_group` — a named-approver
  decision on one ready dependency group, bound to the charter, base catalog,
  active sample policy, evidence lock, proposal-group, candidate, and
  verification fingerprints, plus a fixed statement that the record is not a
  digital signature. The companion refuses to attest a blocked, stale, or
  incompletely verified group.
* :func:`validate_group_attestation` — any bound fingerprint change invalidates
  the attestation (a stale attestation cannot become current by editing its
  status).
* :class:`PreparedBatch` and :func:`prepare_apply_batch` — an explicit ordered,
  dependency-closed, non-overlapping batch of accepted groups reconstructed
  from a common base. It binds the ordered group list, group attestation
  hashes, base hashes, combined candidate and verification fingerprints, and
  the approved-summary hash.
* :class:`ApplyBatchAttestation` and :func:`attest_apply_batch` — the current
  named approver attests the exact prepared-batch hash. Changing the selection
  requires a new prepare and attestation.
* :class:`ApprovedSummary`, :func:`render_approved_summary`, and
  :func:`write_approved_summary_preview` — the deterministic, safe approved
  summary preview. It carries only the eight specified files (decision,
  proposal, evidence lock, catalog patch, references, overlays, verification,
  approval) and never a document body, sample value, full interview answer,
  salt, credential, runtime value, or backup path.

Design rules enforced here (see the approved discovery design and the global
plan constraints):

* Typed data — not patches — remains the sole authority. A preview is a
  rendering only; it is never parsed during apply.
* Approval does not write repository files. Task 20 publishes the identical
  hash-bound summary to ``semantic_changes/`` inside the apply transaction;
  Task 18 writes the preview under the Git-ignored session workspace only.
* Every artifact is a pure function of its bound inputs: a second run with
  unchanged inputs produces an identical fingerprint.
* Diagnostics never echo a decision reason, an evidence body, a sample value,
  or a raw cause — only stable codes, constant generic details, and validated
  safe identifiers.

The module performs no LLM, network, subprocess, Git, or SQL I/O.
:func:`write_approved_summary_preview` writes only under the caller-supplied
session directory.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML

from selayer_discovery import canonical
from selayer_discovery.model import MAX_TEXT_LENGTH, SCHEMA_VERSION

if TYPE_CHECKING:
    from selayer_discovery.proposal import (
        Candidate,
        Proposal,
        VerificationBundle,
    )

__all__ = [
    "APPLY_NOT_A_SIGNATURE_STATEMENT",
    "APPROVED_SUMMARY_FILES",
    "ApplyBatchAttestation",
    "ApprovalError",
    "ApprovedSummary",
    "ApprovedSummaryEntry",
    "GroupAttestation",
    "PreparedBatch",
    "attest_apply_batch",
    "attest_group",
    "compute_approved_summary_hash",
    "prepare_apply_batch",
    "render_approved_summary",
    "validate_group_attestation",
    "write_approved_summary_preview",
]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Fixed statement recorded on every attestation: it is a local named-approver
#: attestation, not a digital signature. Repository review and merge remain the
#: authority. The exact text is part of the attestation fingerprint, so it is a
#: module-level constant and never caller-supplied.
APPLY_NOT_A_SIGNATURE_STATEMENT: str = (
    "This is a local named-approver attestation recorded by the discovery "
    "companion. It is not a digital signature and confers no cryptographic "
    "guarantee; repository review and merge remain the authority."
)

#: Accepted group decision values.
_ATTENTION_ACCEPTED: str = "accepted"
_DECISIONS: frozenset[str] = frozenset(
    {_ATTENTION_ACCEPTED, "rejected", "deferred"}
)

#: The seven bound input-hash keys a group attestation carries.
_GROUP_HASH_KEYS: tuple[str, ...] = (
    "charter",
    "base_catalog",
    "policy",
    "evidence_lock",
    "group",
    "candidate",
    "verification",
)
#: The session-level bound input-hash keys (the four a group attestation shares
#: with every other group attestation and the apply-batch attestation).
_SESSION_HASH_KEYS: tuple[str, ...] = (
    "charter",
    "base_catalog",
    "policy",
    "evidence_lock",
)

#: The three common-base hashes a prepared batch binds.
_BASE_HASH_KEYS: tuple[str, ...] = ("catalog", "references", "overlays")

#: Stable node-id shape (lowercase letter first, then stable identifier chars).
_NODE_ID_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")
_HEX64_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")

# The eight files/directories the approved-summary preview may contain.
# Everything else is rejected by the safe renderer.
_APPROVED_SUMMARY_DECISION: str = "decision.md"
_APPROVED_SUMMARY_PROPOSAL: str = "proposal.yaml"
_APPROVED_SUMMARY_EVIDENCE_LOCK: str = "evidence.lock.json"
_APPROVED_SUMMARY_CATALOG_PATCH: str = "catalog.patch"
_APPROVED_SUMMARY_REFERENCES_DIR: str = "references"
_APPROVED_SUMMARY_OVERLAYS_DIR: str = "overlays"
_APPROVED_SUMMARY_VERIFICATION: str = "verification.json"
_APPROVED_SUMMARY_APPROVAL: str = "approval.json"
#: The closed set of top-level paths an approved summary may carry.
APPROVED_SUMMARY_FILES: frozenset[str] = frozenset(
    {
        _APPROVED_SUMMARY_DECISION,
        _APPROVED_SUMMARY_PROPOSAL,
        _APPROVED_SUMMARY_EVIDENCE_LOCK,
        _APPROVED_SUMMARY_CATALOG_PATCH,
        _APPROVED_SUMMARY_REFERENCES_DIR,
        _APPROVED_SUMMARY_OVERLAYS_DIR,
        _APPROVED_SUMMARY_VERIFICATION,
        _APPROVED_SUMMARY_APPROVAL,
    }
)

#: Maximum number of groups in one prepared batch (defence in depth).
_MAX_BATCH_GROUPS: int = 1_000

#: Substrings that must never appear in an approved-summary preview. The scan
#: is case-insensitive and applied to every rendered entry's text. Document
#: bodies, sample values, full interview answers, salts, credentials, runtime
#: values, and backup/recovery paths are all excluded by the design contract.
_FORBIDDEN_PREVIEW_TOKENS: tuple[str, ...] = (
    "salt",
    "password",
    "credential",
    "token",
    "secret",
    "result_set",
    "row_value",
    "sample_value",
    "query_result",
    "backup",
    "recovery",
    ".bak",
    "next_target",
)

#: Evidence-lock entry keys the safe builder accepts. Every other key is
#: rejected so a body-bearing or value-bearing entry can never reach the lock.
_EVIDENCE_LOCK_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "claim_id",
        "evidence_class",
        "record_id",
        "source_revision",
        "content_hash",
        "selector_kind",
        "selector_field",
        "selector_section",
        "selector_json_path",
        "selector_start_line",
        "selector_end_line",
    }
)
#: Keys a safe evidence-lock entry must carry.
_EVIDENCE_LOCK_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"claim_id", "evidence_class", "record_id", "content_hash"}
)
#: Tokens that must never appear in a safe evidence-lock entry value.
_FORBIDDEN_EVIDENCE_LOCK_TOKENS: tuple[str, ...] = (
    "statement",
    "body",
    "excerpt",
    "password",
    "token",
    "salt",
    "secret",
    "credential",
)

# --------------------------------------------------------------------------- #
# Sanitized diagnostics                                                       #
# --------------------------------------------------------------------------- #

CODE_APPROVAL_INVALID: str = "discovery.approval.invalid"
CODE_APPROVAL_ACTOR: str = "discovery.approval.actor_mismatch"
CODE_APPROVAL_GROUP_BLOCKED: str = "discovery.approval.group_blocked"
CODE_APPROVAL_GROUP_STALE: str = "discovery.approval.group_stale"
CODE_APPROVAL_GROUP_NOT_READY: str = "discovery.approval.group_not_ready"
CODE_APPROVAL_GROUP_NOT_ACCEPTED: str = "discovery.approval.group_not_accepted"
CODE_APPROVAL_FINGERPRINT_CHANGED: str = "discovery.approval.fingerprint_changed"
CODE_APPROVAL_BATCH_OVERLAP: str = "discovery.approval.batch_overlap"
CODE_APPROVAL_BATCH_DEPENDENCY: str = "discovery.approval.batch_dependency"
CODE_APPROVAL_BATCH_HASH: str = "discovery.approval.batch_hash"
CODE_APPROVAL_PREVIEW_UNSAFE: str = "discovery.approval.preview_unsafe"


class ApprovalError(Exception):
    """Sanitized approval diagnostic exception.

    Only a stable ``code`` and an optional constant ``safe_detail`` are ever
    rendered. Raw decision reasons, evidence bodies, sample values, and causes
    are never chained or surfaced.
    """

    def __init__(self, code: str, *, safe_detail: str | None = None) -> None:
        self.code = code
        self.safe_detail = safe_detail
        super().__init__(code)


def _require_id(value: object, *, detail: str) -> str:
    if type(value) is not str or _NODE_ID_RE.match(value) is None:
        raise ApprovalError(CODE_APPROVAL_INVALID, safe_detail=detail) from None
    return value


def _require_hash(value: object, *, detail: str) -> str:
    if type(value) is not str or _HEX64_RE.match(value) is None:
        raise ApprovalError(CODE_APPROVAL_INVALID, safe_detail=detail) from None
    return value


def _require_hash_mapping(
    value: object,
    *,
    keys: Sequence[str],
    detail: str,
) -> dict[str, str]:
    """Return ``value`` as a mapping of every key in ``keys`` to a hex64 hash."""

    if not isinstance(value, Mapping):
        raise ApprovalError(CODE_APPROVAL_INVALID, safe_detail=detail) from None
    result: dict[str, str] = {}
    for key in keys:
        if key not in value:
            raise ApprovalError(CODE_APPROVAL_INVALID, safe_detail=detail) from None
        result[key] = _require_hash(value[key], detail=detail)
    # Reject unknown keys so a hostile payload cannot smuggle extra bindings.
    for key in value:
        if key not in keys:
            raise ApprovalError(CODE_APPROVAL_INVALID, safe_detail=detail) from None
    return result


def _require_bounded_text(value: object, *, detail: str, allow_blank: bool = True) -> str:
    if type(value) is not str:
        raise ApprovalError(CODE_APPROVAL_INVALID, safe_detail=detail) from None
    if not allow_blank and not value.strip():
        raise ApprovalError(CODE_APPROVAL_INVALID, safe_detail=detail) from None
    if len(value) > MAX_TEXT_LENGTH:
        raise ApprovalError(CODE_APPROVAL_INVALID, safe_detail=detail) from None
    return value


# --------------------------------------------------------------------------- #
# Group attestation                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GroupAttestation:
    """A named-approver decision on one dependency group, bound to fingerprints.

    The attestation binds the decision, the normalized named approver, the
    seven bound input hashes (charter, base catalog, active sample policy,
    evidence lock, proposal-group, candidate, verification), the attestation
    timestamp, and the fixed local-attestation statement. Its ``fingerprint`` is
    a pure function of those fields (excluding itself), so a second attestation
    with identical inputs produces an identical fingerprint.

    The attestation is workflow enforcement, not authentication: a local user
    can still type another person's name. Repository review and merge remain
    the authority.
    """

    schema_version: int
    session_id: str
    group_id: str
    approver: str
    decision: str
    reason: str
    input_hashes: Mapping[str, str]
    not_a_signature: str
    timestamp: str
    fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid schema version"
            ) from None
        if self.not_a_signature != APPLY_NOT_A_SIGNATURE_STATEMENT:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid signature statement"
            ) from None
        if self.decision not in _DECISIONS:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid decision"
            ) from None
        if not _HEX64_RE.match(self.fingerprint):
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid fingerprint"
            ) from None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "group_id": self.group_id,
            "approver": self.approver,
            "decision": self.decision,
            "reason": self.reason,
            "input_hashes": dict(self.input_hashes),
            "not_a_signature": self.not_a_signature,
            "timestamp": self.timestamp,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_mapping(cls, data: object) -> GroupAttestation:
        """Reconstruct an attestation from a JSON-safe mapping."""

        if not isinstance(data, Mapping):
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid attestation"
            ) from None
        allowed = {
            "schema_version",
            "session_id",
            "group_id",
            "approver",
            "decision",
            "reason",
            "input_hashes",
            "not_a_signature",
            "timestamp",
            "fingerprint",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid attestation"
            ) from None
        missing = allowed - set(data)
        if missing:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid attestation"
            ) from None
        input_hashes = _require_hash_mapping(
            data["input_hashes"],
            keys=_GROUP_HASH_KEYS,
            detail="invalid attestation",
        )
        raw_schema = data["schema_version"]
        if type(raw_schema) is not int:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid attestation"
            ) from None
        return cls(
            schema_version=raw_schema,
            session_id=_require_id(data["session_id"], detail="invalid attestation"),
            group_id=_require_id(data["group_id"], detail="invalid attestation"),
            approver=_require_bounded_text(
                data["approver"], detail="invalid attestation", allow_blank=False
            ),
            decision=_require_bounded_text(
                data["decision"], detail="invalid attestation", allow_blank=False
            ),
            reason=_require_bounded_text(
                data["reason"], detail="invalid attestation"
            ),
            input_hashes=input_hashes,
            not_a_signature=_require_bounded_text(
                data["not_a_signature"], detail="invalid attestation"
            ),
            timestamp=_require_bounded_text(
                data["timestamp"], detail="invalid attestation", allow_blank=False
            ),
            fingerprint=_require_hash(
                data["fingerprint"], detail="invalid attestation"
            ),
        )


def _group_attestation_fingerprint(
    *,
    schema_version: int,
    session_id: str,
    group_id: str,
    approver: str,
    decision: str,
    reason: str,
    input_hashes: Mapping[str, str],
    not_a_signature: str,
    timestamp: str,
) -> str:
    return canonical.fingerprint(
        {
            "schema_version": schema_version,
            "session_id": session_id,
            "group_id": group_id,
            "approver": approver,
            "decision": decision,
            "reason": reason,
            "input_hashes": dict(input_hashes),
            "not_a_signature": not_a_signature,
            "timestamp": timestamp,
        }
    )


def attest_group(
    *,
    session_id: str,
    group_id: str,
    approver: str,
    current_approver: str,
    decision: str,
    input_hashes: Mapping[str, str],
    group_ready: bool,
    reason: str = "",
    group_blocked: Sequence[str] = (),
    group_stale: bool = False,
    timestamp: str,
) -> GroupAttestation:
    """Record a named-approver decision on one dependency group.

    Requires the claimed ``approver`` to equal the charter's current normalized
    approver, the group to be ready (complete verification), unblocked, and not
    stale, and every bound input hash to be present and well-formed. Returns an
    immutable :class:`GroupAttestation` bound to all seven fingerprints, the
    fixed local-attestation statement, and a stable fingerprint.

    The decision must be one of ``accepted``, ``rejected``, or ``deferred``.
    The ``reason`` is optional, bounded free text carried on the record (it
    never reaches a diagnostic).
    """

    session_id = _require_id(session_id, detail="invalid session id")
    group_id = _require_id(group_id, detail="invalid group id")
    approver = _require_bounded_text(
        approver, detail="invalid approver", allow_blank=False
    )
    current = _require_bounded_text(
        current_approver, detail="invalid approver", allow_blank=False
    )
    if decision not in _DECISIONS:
        raise ApprovalError(CODE_APPROVAL_INVALID, safe_detail="invalid decision") from None
    reason_text = _require_bounded_text(reason, detail="invalid reason")
    timestamp_text = _require_bounded_text(
        timestamp, detail="invalid timestamp", allow_blank=False
    )
    hashes = _require_hash_mapping(
        input_hashes, keys=_GROUP_HASH_KEYS, detail="invalid input hashes"
    )
    # The claimed actor must match the charter's current named approver.
    if approver != current:
        raise ApprovalError(CODE_APPROVAL_ACTOR) from None
    # A blocked, stale, or incompletely verified group cannot be attested.
    if group_stale:
        raise ApprovalError(CODE_APPROVAL_GROUP_STALE) from None
    if not group_ready:
        raise ApprovalError(CODE_APPROVAL_GROUP_NOT_READY) from None
    blocked = tuple(
        _require_id(item, detail="invalid conflict id")
        for item in group_blocked
    )
    if blocked:
        raise ApprovalError(CODE_APPROVAL_GROUP_BLOCKED) from None
    fingerprint = _group_attestation_fingerprint(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        group_id=group_id,
        approver=approver,
        decision=decision,
        reason=reason_text,
        input_hashes=hashes,
        not_a_signature=APPLY_NOT_A_SIGNATURE_STATEMENT,
        timestamp=timestamp_text,
    )
    return GroupAttestation(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        group_id=group_id,
        approver=approver,
        decision=decision,
        reason=reason_text,
        input_hashes=hashes,
        not_a_signature=APPLY_NOT_A_SIGNATURE_STATEMENT,
        timestamp=timestamp_text,
        fingerprint=fingerprint,
    )


def validate_group_attestation(
    attestation: GroupAttestation,
    *,
    current_input_hashes: Mapping[str, str],
    current_group_fingerprint: str,
    current_candidate_fingerprint: str,
    current_verification_fingerprint: str,
) -> None:
    """Reject an attestation whose bound fingerprints no longer match current.

    Any bound fingerprint change invalidates the attestation: a stale
    attestation cannot be revived. Raises :class:`ApprovalError` with
    :data:`CODE_APPROVAL_FINGERPRINT_CHANGED` when any of the charter, base
    catalog, active sample policy, evidence lock, group, candidate, or
    verification fingerprints differ from the current values.
    """

    current_session = _require_hash_mapping(
        current_input_hashes,
        keys=_SESSION_HASH_KEYS,
        detail="invalid current hashes",
    )
    current_group = _require_hash(
        current_group_fingerprint, detail="invalid current group hash"
    )
    current_candidate = _require_hash(
        current_candidate_fingerprint, detail="invalid current candidate hash"
    )
    current_verification = _require_hash(
        current_verification_fingerprint,
        detail="invalid current verification hash",
    )
    bound = attestation.input_hashes
    for key in _SESSION_HASH_KEYS:
        if bound[key] != current_session[key]:
            raise ApprovalError(CODE_APPROVAL_FINGERPRINT_CHANGED) from None
    if bound["group"] != current_group:
        raise ApprovalError(CODE_APPROVAL_FINGERPRINT_CHANGED) from None
    if bound["candidate"] != current_candidate:
        raise ApprovalError(CODE_APPROVAL_FINGERPRINT_CHANGED) from None
    if bound["verification"] != current_verification:
        raise ApprovalError(CODE_APPROVAL_FINGERPRINT_CHANGED) from None


# --------------------------------------------------------------------------- #
# Apply-batch preparation                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PreparedBatch:
    """A dependency-closed, non-overlapping batch of accepted groups.

    Binds the ordered group list, the per-group attestation fingerprints (in
    the same order), the common-base hashes (catalog, references, overlays),
    the combined candidate and verification fingerprints, the four session-level
    bound hashes, the approved-summary hash, and a stable fingerprint. A second
    prepare with identical inputs produces an identical fingerprint.
    """

    schema_version: int
    session_id: str
    proposal_id: str
    group_ids: tuple[str, ...]
    attestation_fingerprints: tuple[str, ...]
    base_hashes: Mapping[str, str]
    candidate_fingerprint: str
    verification_fingerprint: str
    approved_summary_hash: str
    session_hashes: Mapping[str, str]
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "proposal_id": self.proposal_id,
            "group_ids": list(self.group_ids),
            "attestation_fingerprints": list(self.attestation_fingerprints),
            "base_hashes": dict(self.base_hashes),
            "candidate_fingerprint": self.candidate_fingerprint,
            "verification_fingerprint": self.verification_fingerprint,
            "approved_summary_hash": self.approved_summary_hash,
            "session_hashes": dict(self.session_hashes),
            "fingerprint": self.fingerprint,
        }


def _selected_groups(
    proposal: Proposal, group_ids: Sequence[str]
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Return the validated ordered selection and a group lookup."""

    if isinstance(group_ids, str) or not isinstance(group_ids, Sequence):
        raise ApprovalError(
            CODE_APPROVAL_INVALID, safe_detail="invalid group selection"
        ) from None
    if not group_ids:
        raise ApprovalError(
            CODE_APPROVAL_INVALID, safe_detail="empty group selection"
        ) from None
    if len(group_ids) > _MAX_BATCH_GROUPS:
        raise ApprovalError(
            CODE_APPROVAL_INVALID, safe_detail="invalid group selection"
        ) from None
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in group_ids:
        gid = _require_id(raw, detail="invalid group id")
        if gid in seen:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="duplicate group id"
            ) from None
        seen.add(gid)
        ordered.append(gid)
    by_id = {group.group_id: group for group in proposal.groups}
    for gid in ordered:
        if gid not in by_id:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="unknown group"
            ) from None
    return tuple(ordered), by_id


def _reject_batch_overlap(by_id: Mapping[str, Any], ordered: Sequence[str]) -> None:
    """Reject two selected groups that target the same object or section.

    Catalog target overlap is already rejected at proposal build time across
    the whole proposal; this guard additionally catches two groups editing the
    same authored Reference or overlay path (a curated-section overlap) and
    defends against any future weakening of the proposal guard.
    """

    seen_targets: set[str] = set()
    for gid in ordered:
        for op in by_id[gid].operations:
            if op.target_id in seen_targets:
                raise ApprovalError(CODE_APPROVAL_BATCH_OVERLAP) from None
            seen_targets.add(op.target_id)


def _reject_dependency_open(by_id: Mapping[str, Any], ordered: Sequence[str]) -> None:
    """Require every selected group's dependencies to be selected too."""

    selected = set(ordered)
    for gid in ordered:
        for dep in by_id[gid].dependencies:
            if dep not in selected:
                raise ApprovalError(CODE_APPROVAL_BATCH_DEPENDENCY) from None


def prepare_apply_batch(
    *,
    proposal: Proposal,
    group_ids: Sequence[str],
    attestations: Mapping[str, GroupAttestation],
    combined_candidate: Candidate,
    combined_verification: VerificationBundle,
    base_hashes: Mapping[str, str],
    current_session_hashes: Mapping[str, str],
    approved_summary_hash: str,
) -> PreparedBatch:
    """Prepare a dependency-closed, non-overlapping batch of accepted groups.

    Validates the explicit ordered selection, requires each selected group to
    carry a current ``accepted`` attestation whose bound fingerprints still
    match the current session/group/candidate/verification state, requires
    dependency closure and no target overlap, and requires the combined
    verification to cover the combined candidate and every selected group as
    ready. Binds the group list, attestation fingerprints, common-base hashes,
    combined candidate and verification fingerprints, session hashes, and the
    approved-summary hash into an immutable :class:`PreparedBatch`.
    """

    ordered, by_id = _selected_groups(proposal, group_ids)
    _reject_dependency_open(by_id, ordered)
    _reject_batch_overlap(by_id, ordered)
    bases = _require_hash_mapping(
        base_hashes, keys=_BASE_HASH_KEYS, detail="invalid base hashes"
    )
    session_hashes = _require_hash_mapping(
        current_session_hashes,
        keys=_SESSION_HASH_KEYS,
        detail="invalid session hashes",
    )
    summary_hash = _require_hash(
        approved_summary_hash, detail="invalid summary hash"
    )
    if not isinstance(attestations, Mapping):
        raise ApprovalError(
            CODE_APPROVAL_INVALID, safe_detail="invalid attestations"
        ) from None
    # The combined verification must cover the combined candidate and the
    # selected groups (the union of mandatory checks rerun on the combined
    # candidate).
    if combined_verification.candidate_fingerprint != combined_candidate.fingerprint:
        raise ApprovalError(
            CODE_APPROVAL_INVALID, safe_detail="verification candidate mismatch"
        ) from None
    verified_groups = {gv.group_id for gv in combined_verification.groups}
    for gid in ordered:
        if gid not in verified_groups:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="verification incomplete"
            ) from None
    readiness = {gv.group_id: gv.readiness for gv in combined_verification.groups}
    attestation_fingerprints: list[str] = []
    for gid in ordered:
        attestation = attestations.get(gid)
        if not isinstance(attestation, GroupAttestation):
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="missing attestation"
            ) from None
        if attestation.decision != _ATTENTION_ACCEPTED:
            raise ApprovalError(CODE_APPROVAL_GROUP_NOT_ACCEPTED) from None
        # The attestation must be current: the bound session hashes (charter,
        # base catalog, active sample policy, evidence lock) and the proposal
        # group fingerprint must still match the live state. A changed base
        # catalog, policy, evidence lock, or charter invalidates the
        # attestation; so does a changed group (its operations or rationale).
        # The bound candidate and verification fingerprints are validated
        # against the current whole-proposal candidate and verification by
        # :func:`validate_group_attestation` at load time (the CLI reconstructs
        # and re-verifies the whole proposal before applying).
        group_fp = canonical.fingerprint(by_id[gid].to_dict())
        bound = attestation.input_hashes
        for key in _SESSION_HASH_KEYS:
            if bound[key] != session_hashes[key]:
                raise ApprovalError(CODE_APPROVAL_FINGERPRINT_CHANGED) from None
        if bound["group"] != group_fp:
            raise ApprovalError(CODE_APPROVAL_FINGERPRINT_CHANGED) from None
        if not readiness[gid].ready:
            raise ApprovalError(CODE_APPROVAL_GROUP_NOT_READY) from None
        attestation_fingerprints.append(attestation.fingerprint)
    fingerprint = canonical.fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "proposal_id": proposal.proposal_id,
            "group_ids": list(ordered),
            "attestation_fingerprints": attestation_fingerprints,
            "base_hashes": bases,
            "candidate_fingerprint": combined_candidate.fingerprint,
            "verification_fingerprint": combined_verification.fingerprint,
            "session_hashes": session_hashes,
            "approved_summary_hash": summary_hash,
        }
    )
    return PreparedBatch(
        schema_version=SCHEMA_VERSION,
        session_id=attestations[ordered[0]].session_id,
        proposal_id=proposal.proposal_id,
        group_ids=ordered,
        attestation_fingerprints=tuple(attestation_fingerprints),
        base_hashes=bases,
        candidate_fingerprint=combined_candidate.fingerprint,
        verification_fingerprint=combined_verification.fingerprint,
        approved_summary_hash=summary_hash,
        session_hashes=session_hashes,
        fingerprint=fingerprint,
    )


# --------------------------------------------------------------------------- #
# Apply-batch attestation                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ApplyBatchAttestation:
    """A named-approver attestation of one prepared apply batch.

    Binds the current named approver, the exact prepared-batch hash, the
    session id, the attestation timestamp, and the fixed local-attestation
    statement. Changing the selection requires a new prepare (a new batch hash)
    and therefore a new attestation.
    """

    schema_version: int
    session_id: str
    batch_hash: str
    approver: str
    not_a_signature: str
    timestamp: str
    fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid schema version"
            ) from None
        if self.not_a_signature != APPLY_NOT_A_SIGNATURE_STATEMENT:
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid signature statement"
            ) from None
        if not _HEX64_RE.match(self.fingerprint):
            raise ApprovalError(
                CODE_APPROVAL_INVALID, safe_detail="invalid fingerprint"
            ) from None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "batch_hash": self.batch_hash,
            "approver": self.approver,
            "not_a_signature": self.not_a_signature,
            "timestamp": self.timestamp,
            "fingerprint": self.fingerprint,
        }


def attest_apply_batch(
    *,
    batch: PreparedBatch,
    approver: str,
    current_approver: str,
    timestamp: str,
    prepared_batch_hash: str | None = None,
) -> ApplyBatchAttestation:
    """Attest one prepared apply batch as the current named approver.

    Requires the claimed ``approver`` to equal the charter's current normalized
    approver and, when ``prepared_batch_hash`` is supplied, that it exactly
    equals ``batch.fingerprint`` (so the attestation binds the exact batch the
    approver reviewed). Changing the selection requires a new prepare (a new
    batch hash) and therefore a new attestation.
    """

    approver_text = _require_bounded_text(
        approver, detail="invalid approver", allow_blank=False
    )
    current = _require_bounded_text(
        current_approver, detail="invalid approver", allow_blank=False
    )
    timestamp_text = _require_bounded_text(
        timestamp, detail="invalid timestamp", allow_blank=False
    )
    if approver_text != current:
        raise ApprovalError(CODE_APPROVAL_ACTOR) from None
    if prepared_batch_hash is not None:
        expected = _require_hash(
            prepared_batch_hash, detail="invalid batch hash"
        )
        if expected != batch.fingerprint:
            raise ApprovalError(CODE_APPROVAL_BATCH_HASH) from None
    fingerprint = canonical.fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "session_id": batch.session_id,
            "batch_hash": batch.fingerprint,
            "approver": approver_text,
            "not_a_signature": APPLY_NOT_A_SIGNATURE_STATEMENT,
            "timestamp": timestamp_text,
        }
    )
    return ApplyBatchAttestation(
        schema_version=SCHEMA_VERSION,
        session_id=batch.session_id,
        batch_hash=batch.fingerprint,
        approver=approver_text,
        not_a_signature=APPLY_NOT_A_SIGNATURE_STATEMENT,
        timestamp=timestamp_text,
        fingerprint=fingerprint,
    )


# --------------------------------------------------------------------------- #
# Safe approved-summary preview                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ApprovedSummaryEntry:
    """One rendered file in the approved-summary preview.

    ``path`` is a POSIX-style relative path under the preview root. ``text`` is
    the deterministic file content (no document body, sample value, full
    interview answer, salt, credential, runtime value, or backup path).
    """

    path: str
    text: str


@dataclass(frozen=True, slots=True)
class ApprovedSummary:
    """The deterministic, safe approved-summary preview.

    Carries the eight specified files only. Its ``fingerprint`` is a pure
    function of the entry paths and contents, so an identical summary published
    by Task 20 to ``semantic_changes/`` produces the same hash. The summary is
    never Git-visible from Task 18: it is written under the Git-ignored session
    workspace by :func:`write_approved_summary_preview`.
    """

    schema_version: int
    batch_hash: str
    entries: tuple[ApprovedSummaryEntry, ...]
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "batch_hash": self.batch_hash,
            "entries": [
                {"path": e.path, "fingerprint": canonical.fingerprint(e.text)}
                for e in self.entries
            ],
            "fingerprint": self.fingerprint,
        }


def _safe_preview_path(rel_path: str) -> str:
    """Validate that ``rel_path`` is a single POSIX path inside the allowlist."""

    if type(rel_path) is not str or not rel_path:
        raise ApprovalError(
            CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="invalid preview path"
        ) from None
    pure = PurePosixPath(rel_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ApprovalError(
            CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="invalid preview path"
        ) from None
    top = pure.parts[0]
    if top not in APPROVED_SUMMARY_FILES:
        raise ApprovalError(
            CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="disallowed preview path"
        ) from None
    # No backup/recovery/snapshot/runtime directory may appear anywhere.
    lowered = rel_path.lower()
    for token in _FORBIDDEN_PREVIEW_TOKENS:
        if token in lowered:
            raise ApprovalError(
                CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="disallowed preview path"
            ) from None
    return rel_path


def _scan_preview_text(text: str) -> None:
    """Reject any forbidden token in a rendered preview file's text."""

    lowered = text.lower()
    for token in _FORBIDDEN_PREVIEW_TOKENS:
        if token in lowered:
            raise ApprovalError(
                CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="unsafe preview content"
            ) from None


def _build_evidence_lock(entries: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return safe evidence-lock entries: ids, hashes, revisions, selectors only.

    Rejects unknown keys, missing required keys, and any token that would carry
    a body, statement, sample value, salt, or credential. The lock records
    evidence resource ids, revisions, selectors, and hashes only — never a
    document body, claim statement, sample value, or credential.
    """

    safe: list[dict[str, object]] = []
    if isinstance(entries, str) or not isinstance(entries, Sequence):
        raise ApprovalError(
            CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="invalid evidence lock"
        ) from None
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ApprovalError(
                CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="invalid evidence lock"
            ) from None
        unknown = set(entry) - _EVIDENCE_LOCK_ALLOWED_KEYS
        if unknown:
            raise ApprovalError(
                CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="invalid evidence lock"
            ) from None
        missing = _EVIDENCE_LOCK_REQUIRED_KEYS - set(entry)
        if missing:
            raise ApprovalError(
                CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="invalid evidence lock"
            ) from None
        record: dict[str, object] = {}
        for key, value in entry.items():
            if type(value) is not str and type(value) is not int:
                raise ApprovalError(
                    CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="invalid evidence lock"
                ) from None
            if type(value) is str:
                lowered = value.lower()
                for token in _FORBIDDEN_EVIDENCE_LOCK_TOKENS:
                    if token in lowered:
                        raise ApprovalError(
                            CODE_APPROVAL_PREVIEW_UNSAFE,
                            safe_detail="invalid evidence lock",
                        ) from None
            record[str(key)] = value
        safe.append(record)
    safe.sort(key=lambda item: str(item.get("claim_id", "")))
    return safe


def _reference_paths(candidate: Candidate) -> tuple[tuple[str, str], ...]:
    return candidate.references


def _overlay_paths(candidate: Candidate) -> tuple[tuple[str, str], ...]:
    return candidate.overlays


def render_approved_summary(
    *,
    proposal: Proposal,
    group_ids: Sequence[str],
    candidate: Candidate,
    verification: VerificationBundle,
    group_attestations: Mapping[str, GroupAttestation],
    apply_attestation: ApplyBatchAttestation,
    base_hashes: Mapping[str, str],
    base_catalog_text: str,
    evidence_lock: Sequence[Mapping[str, object]],
    decision_statement: str,
) -> ApprovedSummary:
    """Render the deterministic, safe approved-summary preview.

    Produces exactly the eight specified files:

    * ``decision.md`` — a body-free decision summary (approver, session, the
      accepted group ids, decisions, and the decision statement).
    * ``proposal.yaml`` — the accepted dependency groups only (typed
      operations, rationale, supporting claim/conflict/gate ids).
    * ``evidence.lock.json`` — evidence resource ids, revisions, selectors, and
      hashes (never a body, statement, sample value, salt, or credential).
    * ``catalog.patch`` — the deterministic review preview patch.
    * ``references/`` — the authored Reference documents from the accepted
      operations (curated knowledge sources).
    * ``overlays/`` — the authored overlay documents from the accepted
      operations (curated knowledge sources).
    * ``verification.json`` — the combined verification bundle.
    * ``approval.json`` — the apply-batch attestation and the per-group
      attestations.

    Every entry's path is validated against the allowlist and every entry's
    text is scanned for forbidden tokens. Raises :class:`ApprovalError` if any
    disallowed path or content would reach the summary.
    """

    bases = _require_hash_mapping(
        base_hashes, keys=_BASE_HASH_KEYS, detail="invalid base hashes"
    )
    decision = _require_bounded_text(
        decision_statement, detail="invalid decision statement", allow_blank=False
    )
    selected = set(group_ids)
    accepted_groups = [
        group for group in proposal.groups if group.group_id in selected
    ]
    # The sub-proposal of accepted groups only (typed operations remain apply
    # authority; the YAML is a review rendering).
    sub_proposal_payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal.proposal_id,
        "title": proposal.title,
        "groups": [group.to_dict() for group in accepted_groups],
    }
    yaml_buf = _dump_yaml(sub_proposal_payload)
    decision_lines: list[str] = [
        f"# Decision summary for proposal {proposal.proposal_id}",
        "",
        f"Session: {apply_attestation.session_id}",
        f"Approver: {apply_attestation.approver}",
        f"Batch hash: {apply_attestation.batch_hash}",
        f"Approved-summary hash: {canonical.fingerprint(yaml_buf)}",
        "",
        "Accepted dependency groups:",
    ]
    for gid in group_ids:
        attestation = group_attestations.get(gid)
        decision_text = attestation.decision if attestation else "unknown"
        decision_lines.append(f"- {gid}: {decision_text}")
    decision_lines.extend(["", decision.strip()])
    decision_md = "\n".join(decision_lines) + "\n"
    evidence_lock_payload = {
        "schema_version": SCHEMA_VERSION,
        "entries": _build_evidence_lock(evidence_lock),
    }
    evidence_lock_json = json.dumps(evidence_lock_payload, sort_keys=True) + "\n"
    verification_json = json.dumps(verification.to_dict(), sort_keys=True) + "\n"
    approval_payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "apply_attestation": apply_attestation.to_dict(),
        "group_attestations": [
            group_attestations[gid].to_dict()
            for gid in group_ids
            if gid in group_attestations
        ],
        "base_hashes": dict(bases),
    }
    approval_json = json.dumps(approval_payload, sort_keys=True) + "\n"
    entries: list[ApprovedSummaryEntry] = [
        ApprovedSummaryEntry(
            path=_safe_preview_path(_APPROVED_SUMMARY_DECISION),
            text=decision_md,
        ),
        ApprovedSummaryEntry(
            path=_safe_preview_path(_APPROVED_SUMMARY_PROPOSAL),
            text=yaml_buf,
        ),
        ApprovedSummaryEntry(
            path=_safe_preview_path(_APPROVED_SUMMARY_EVIDENCE_LOCK),
            text=evidence_lock_json,
        ),
        ApprovedSummaryEntry(
            path=_safe_preview_path(_APPROVED_SUMMARY_VERIFICATION),
            text=verification_json,
        ),
        ApprovedSummaryEntry(
            path=_safe_preview_path(_APPROVED_SUMMARY_APPROVAL),
            text=approval_json,
        ),
    ]
    # Catalog patch is the deterministic base-to-candidate diff (the same
    # review rendering ``proposal show`` produces). It is a rendering only;
    # typed operations remain apply authority.
    patch_text = _catalog_patch(base_catalog_text, candidate)
    entries.append(
        ApprovedSummaryEntry(
            path=_safe_preview_path(_APPROVED_SUMMARY_CATALOG_PATCH),
            text=patch_text,
        )
    )
    # Authored Reference and overlay documents (curated knowledge sources).
    for rel_path, text in _reference_paths(candidate):
        entries.append(
            ApprovedSummaryEntry(
                path=_safe_preview_path(f"{_APPROVED_SUMMARY_REFERENCES_DIR}/{rel_path}"),
                text=text,
            )
        )
    for rel_path, text in _overlay_paths(candidate):
        entries.append(
            ApprovedSummaryEntry(
                path=_safe_preview_path(f"{_APPROVED_SUMMARY_OVERLAYS_DIR}/{rel_path}"),
                text=text,
            )
        )
    # Final safety scan over every rendered entry.
    for entry in entries:
        _scan_preview_text(entry.text)
    fingerprint = canonical.fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "batch_hash": apply_attestation.batch_hash,
            "entries": [
                {"path": e.path, "fingerprint": canonical.fingerprint(e.text)}
                for e in entries
            ],
        }
    )
    return ApprovedSummary(
        schema_version=SCHEMA_VERSION,
        batch_hash=apply_attestation.batch_hash,
        entries=tuple(entries),
        fingerprint=fingerprint,
    )


def _catalog_patch(base_catalog_text: str, candidate: Candidate) -> str:
    """Render the deterministic base-to-candidate catalog diff.

    This is the same review rendering ``render_review_preview`` produces: a
    unified diff from the authored base catalog text to the reconstructed
    candidate catalog text. It is a rendering only; typed operations remain
    apply authority.
    """

    import difflib

    return "".join(
        difflib.unified_diff(
            base_catalog_text.splitlines(keepends=True),
            candidate.catalog_text.splitlines(keepends=True),
            fromfile="a/catalog.yaml",
            tofile="b/catalog.yaml",
            n=0,
        )
    )


def compute_approved_summary_hash(
    *,
    proposal: Proposal,
    group_ids: Sequence[str],
    candidate: Candidate,
    verification: VerificationBundle,
    base_hashes: Mapping[str, str],
    base_catalog_text: str,
    evidence_lock: Sequence[Mapping[str, object]],
    decision_statement: str,
) -> str:
    """Compute the deterministic content-commitment hash a prepared batch binds.

    The prepared batch binds this hash as its ``approved_summary_hash``. It is
    a pure function of the approved-summary *content* — the accepted groups,
    candidate, verification, base hashes, evidence lock, catalog patch,
    authored knowledge, and decision statement — and is deliberately
    independent of the apply-batch attestation (which does not yet exist at
    prepare time). This breaks the otherwise-circular dependency between the
    batch fingerprint (which binds the summary hash) and the summary
    fingerprint (which binds the batch hash via the attestation metadata).

    The final preview rendered by :func:`render_approved_summary` layers the
    attestation metadata (``decision.md``, ``approval.json``) on top of this
    content commitment; its own ``fingerprint`` differs because it additionally
    binds the batch hash.
    """

    bases = _require_hash_mapping(
        base_hashes, keys=_BASE_HASH_KEYS, detail="invalid base hashes"
    )
    decision = _require_bounded_text(
        decision_statement, detail="invalid decision statement", allow_blank=False
    )
    selected = set(group_ids)
    accepted = [group for group in proposal.groups if group.group_id in selected]
    return canonical.fingerprint(
        {
            "groups": [group.to_dict() for group in accepted],
            "candidate_fingerprint": candidate.fingerprint,
            "catalog_patch": _catalog_patch(base_catalog_text, candidate),
            "references": [
                [path, canonical.fingerprint(text)]
                for path, text in candidate.references
            ],
            "overlays": [
                [path, canonical.fingerprint(text)]
                for path, text in candidate.overlays
            ],
            "verification_fingerprint": verification.fingerprint,
            "evidence_lock": _build_evidence_lock(evidence_lock),
            "base_hashes": dict(bases),
            "decision_statement": decision.strip(),
        }
    )


def _dump_yaml(data: object) -> str:
    import io

    buf = io.StringIO()
    YAML().dump(data, buf)
    return buf.getvalue()


def write_approved_summary_preview(
    summary: ApprovedSummary,
    session_dir: Path,
    batch_hash: str,
) -> Path:
    """Write the approved-summary preview under the Git-ignored session workspace.

    Writes each entry under ``<session_dir>/exports/<batch_hash>/`` with
    owner-only permissions on POSIX. The preview is never Git-visible: Task 20
    publishes the identical hash-bound summary to ``semantic_changes/`` inside
    the apply transaction; Task 18 writes under the ignored workspace only.
    """

    if not _HEX64_RE.match(batch_hash):
        raise ApprovalError(
            CODE_APPROVAL_INVALID, safe_detail="invalid batch hash"
        ) from None
    exports = session_dir / "exports" / batch_hash
    # Defence in depth: the exports root must stay inside the session dir.
    try:
        exports.resolve(strict=False).relative_to(
            session_dir.resolve(strict=False)
        )
    except ValueError:
        raise ApprovalError(
            CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="invalid preview root"
        ) from None
    exports.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(exports, 0o700)
        except OSError:
            pass
    for entry in summary.entries:
        target = exports / PurePosixPath(entry.path)
        try:
            target.resolve(strict=False).relative_to(
                exports.resolve(strict=False)
            )
        except ValueError:
            raise ApprovalError(
                CODE_APPROVAL_PREVIEW_UNSAFE, safe_detail="invalid preview path"
            ) from None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry.text, encoding="utf-8")
        if os.name == "posix":
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
    return exports
