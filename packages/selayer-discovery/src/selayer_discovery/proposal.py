"""Typed semantic proposal operations, candidate reconstruction, and previews.

This module owns the Stage 4 typed-proposal foundation for the discovery
companion package:

* :class:`Operation` — an immutable, validated typed catalog or knowledge
  operation with a complete normalized before/after state and derived impact
  flags and changed-field set.
* :class:`DependencyGroup` — an atomic, acyclic group of operations bound to
  rationale, current non-inferred claims, affecting gates, conflicts, query
  cases, and inter-group dependencies.
* :class:`Proposal` — a versioned, deterministically-ordered collection of
  dependency groups with a stable fingerprint.
* :func:`reconstruct_candidate` — round-trips the base catalog through
  ``ruamel.yaml`` and applies typed operations so comments, key order outside
  changed objects, quoting, and newline style are preserved, and the
  reconstructed catalog loads to the exact expected
  :class:`~selayer.model.SemanticLayer`.
* :func:`render_review_preview` — renders a deterministic ``catalog.patch`` and
  knowledge diff. Previews are never verify or apply authority.

Design rules enforced here (see the approved discovery design and the global
plan constraints):

* Version 1 supports exactly seven normalized operation kinds:
  ``catalog.add``, ``catalog.edit``, ``catalog.deprecate``,
  ``reference.create``, ``reference.update``, ``overlay.create``,
  ``overlay.update``. Every other kind (including ``catalog.delete`` and
  ``catalog.rename``) is rejected.
* Each operation carries a fully-qualified target id, a normalized before
  state (or an absent marker with the genesis hash), a before-state hash, a
  complete normalized after state, evidence claim ids, and dependency-group
  ids. The changed-field set and impact flags are *derived* by the companion
  from the normalized before/after state; an agent-supplied impact list is
  ignored.
* Catalog operations may not rename an object, change its semantic kind, edit
  outside the target object, or carry arbitrary patch input.
* Knowledge operations target authored Reference or overlay directories only.
  They may not target generated OKF output, escape the directory root, edit
  generated frontmatter, or edit the ``Catalog Definition`` section.
* Atomic dependency groups reject cycles and unknown dependencies.
* Review previews are deterministic renderings only; they are never parsed
  during verify or apply.

The module performs no LLM, network, subprocess, Git, or SQL I/O, and it never
mutates a session state file directly.
"""

from __future__ import annotations

import difflib
import io
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

from ruamel.yaml import YAML, YAMLError

from selayer_discovery import canonical
from selayer_discovery.model import MAX_TEXT_LENGTH, bounded_mapping

if TYPE_CHECKING:
    from selayer.catalog import SemanticLayer
    from selayer_discovery.evidence import ClaimRecord, ClaimStore, EvidenceStore
    from selayer_discovery.interview import InterviewStore

__all__ = [
    "CATALOG_COLLECTION_BY_KIND",
    "CATALOG_KINDS",
    "GENESIS_HASH",
    "KNOWLEDGE_KINDS",
    "CaseAssertion",
    "CaseFilter",
    "GroupReadiness",
    "GroupVerification",
    "KnowledgeSubject",
    "KnowledgeSummaryEntry",
    "MandatoryCheck",
    "MandatoryCheckKind",
    "Operation",
    "OperationKind",
    "Proposal",
    "ProposalError",
    "QueryCase",
    "ReviewPreview",
    "ReviewSummary",
    "VerificationBundle",
    "build_proposal",
    "mandatory_check_kinds",
    "reconstruct_candidate",
    "render_review_preview",
    "render_review_summary",
    "verify_proposal",
]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Previous-state sentinel for create/add operations (64-zero hex, hash-shaped).
GENESIS_HASH: str = "0" * 64

#: Maximum number of operations, groups, or query cases in one proposal.
_MAX_OPERATIONS: int = 1_000
_MAX_GROUPS: int = 1_000
_MAX_QUERY_CASES: int = 1_000

_HEX64_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")
#: Stable operation/group/case id shape (lowercase letter first, then stable
#: identifier characters). Matches the session node-id grammar so an id is safe
#: to render in diagnostics.
_NODE_ID_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")
#: Fully-qualified catalog target id: ``<kind>.<identifier>`` where the kind is
#: one of the catalog collection singular names.
_CATALOG_TARGET_RE: re.Pattern[str] = re.compile(
    r"\A(source|dimension|fact|measure|metric|relationship)\.([a-z][a-z0-9_]*)\Z"
)
#: Claim ids use the evidence ``claim-`` prefix shape.
_CLAIM_ID_RE: re.Pattern[str] = re.compile(r"\Aclaim-[a-z0-9_.-]{1,127}\Z")
#: Gate ids mirror the interview gate grammar.
_GATE_ID_RE: re.Pattern[str] = re.compile(r"\Agate-[a-z0-9_.-]{1,127}\Z")
_CONFLICT_ID_RE: re.Pattern[str] = re.compile(r"\Aconflict-[a-z0-9_.-]{1,127}\Z")
#: Knowledge operation relative path: a POSIX-style relative path with no
#: leading slash, no parent traversal, and a ``.md`` suffix.
_KNOWLEDGE_PATH_RE: re.Pattern[str] = re.compile(
    r"\A(?!etc/)[a-z0-9][a-z0-9_./-]*\.md\Z"
)

# --------------------------------------------------------------------------- #
# Sanitized diagnostics                                                       #
# --------------------------------------------------------------------------- #

CODE_PROPOSAL_INVALID: str = "discovery.proposal.invalid"
CODE_PROPOSAL_OPERATION_INVALID: str = "discovery.proposal.operation_invalid"
CODE_PROPOSAL_RECONSTRUCTION_FAILED: str = "discovery.proposal.reconstruction_failed"
CODE_PROPOSAL_CYCLE: str = "discovery.proposal.dependency_cycle"
CODE_PROPOSAL_OVERLAPPING_TARGET: str = "discovery.proposal.overlapping_target"


class ProposalError(Exception):
    """Sanitized proposal diagnostic exception.

    Only a stable ``code`` and optional constant ``safe_detail`` are ever
    rendered. Raw before/after state, document bodies, and causes are never
    chained or surfaced.
    """

    def __init__(self, code: str, *, safe_detail: str | None = None) -> None:
        self.code = code
        self.safe_detail = safe_detail
        super().__init__(code)


def _require(value: object, *, message: str) -> None:
    if not value:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail=message)


# --------------------------------------------------------------------------- #
# Operation kinds                                                             #
# --------------------------------------------------------------------------- #


class OperationKind(StrEnum):
    """The seven supported normalized operation kinds."""

    CATALOG_ADD = "catalog.add"
    CATALOG_EDIT = "catalog.edit"
    CATALOG_DEPRECATE = "catalog.deprecate"
    REFERENCE_CREATE = "reference.create"
    REFERENCE_UPDATE = "reference.update"
    OVERLAY_CREATE = "overlay.create"
    OVERLAY_UPDATE = "overlay.update"


class KnowledgeSubject(StrEnum):
    """Whether a knowledge operation targets References or overlays."""

    REFERENCE = "reference"
    OVERLAY = "overlay"


#: Catalog operation kinds.
CATALOG_KINDS: frozenset[OperationKind] = frozenset(
    {
        OperationKind.CATALOG_ADD,
        OperationKind.CATALOG_EDIT,
        OperationKind.CATALOG_DEPRECATE,
    }
)
#: Knowledge operation kinds.
KNOWLEDGE_KINDS: frozenset[OperationKind] = frozenset(
    {
        OperationKind.REFERENCE_CREATE,
        OperationKind.REFERENCE_UPDATE,
        OperationKind.OVERLAY_CREATE,
        OperationKind.OVERLAY_UPDATE,
    }
)

#: Maps a catalog target-id prefix to the YAML collection key and the set of
#: field names that identify the object's semantic kind. The kind set is used
#: to reject a target-kind change (the before/after shape must belong to the
#: same semantic kind family).
_CATALOG_COLLECTION_BY_PREFIX: Mapping[str, str] = {
    "source": "data_sources",
    "dimension": "dimensions",
    "fact": "facts",
    "measure": "measures",
    "metric": "metrics",
    "relationship": "relationships",
}

#: Inverse map: kind -> prefix.
_PREFIX_BY_COLLECTION: Mapping[str, str] = {
    v: k for k, v in _CATALOG_COLLECTION_BY_PREFIX.items()
}

#: Discriminator fields per catalog kind. An edit's before/after must satisfy
#: the discriminator of the target kind so a dimension edit cannot carry a
#: fact's ``expression`` field as its identity.
_KIND_DISCRIMINATORS: Mapping[str, frozenset[str]] = {
    "source": frozenset({"grain"}),
    "dimension": frozenset({"source", "column", "data_type"}),
    "fact": frozenset({"source", "expression", "data_type"}),
    "measure": frozenset({"fact", "aggregation"}),
    "metric": frozenset({"expression", "measures"}),
    "relationship": frozenset({"source", "target", "type"}),
}

#: Maps each catalog operation kind to its collection key. All three catalog
#: operation kinds address the same collection families; the constant documents
#: that there is no per-kind collection split.
CATALOG_COLLECTION_BY_KIND: Mapping[OperationKind, str] = {
    kind: "catalog" for kind in CATALOG_KINDS
}

# Impact-flag derivation vocabulary. These flags are derived from the
# normalized before/after state and are the only authority for verification
# readiness (Task 17). Agents never supply them.
_IMPACT_OBJECT_ADDED = "object_added"
_IMPACT_OBJECT_EDITED = "object_edited"
_IMPACT_ID_DEPRECATED = "id_deprecated"
_IMPACT_SOURCE_CHANGED = "source_changed"
_IMPACT_SCHEMA_CHANGED = "schema_changed"
_IMPACT_GRAIN_CHANGED = "grain_changed"
_IMPACT_RELATIONSHIP_CHANGED = "relationship_changed"
_IMPACT_TYPE_CHANGED = "type_changed"
_IMPACT_EXPRESSION_CHANGED = "expression_changed"
_IMPACT_AGGREGATION_CHANGED = "aggregation_changed"
_IMPACT_FORMULA_CHANGED = "formula_changed"
_IMPACT_REFERENCE_CHANGED = "reference_changed"
_IMPACT_OVERLAY_CHANGED = "overlay_changed"

# Fields whose change drives specific derived impacts for catalog edits.
_FIELD_IMPACTS: Mapping[str, str] = {
    "grain": _IMPACT_GRAIN_CHANGED,
    "schema": _IMPACT_SCHEMA_CHANGED,
    "connector": _IMPACT_SOURCE_CHANGED,
    "type": _IMPACT_RELATIONSHIP_CHANGED,
    "source_column": _IMPACT_RELATIONSHIP_CHANGED,
    "target_column": _IMPACT_RELATIONSHIP_CHANGED,
    "target": _IMPACT_RELATIONSHIP_CHANGED,
    "data_type": _IMPACT_TYPE_CHANGED,
    "expression": _IMPACT_EXPRESSION_CHANGED,
    "aggregation": _IMPACT_AGGREGATION_CHANGED,
    "measures": _IMPACT_FORMULA_CHANGED,
}

# --------------------------------------------------------------------------- #
# Mandatory-check matrix (Task 17)                                            #
# --------------------------------------------------------------------------- #

#: Impacts whose change is a ``type`` or ``expression`` revision. These always
#: require a static source-column/type check; a reopenable data audit is added
#: by :func:`verify_proposal` only when the group cites observed data evidence.
_TYPE_EXPRESSION_IMPACTS: frozenset[str] = frozenset(
    {_IMPACT_TYPE_CHANGED, _IMPACT_EXPRESSION_CHANGED}
)


class MandatoryCheckKind(StrEnum):
    """The five mandatory verification kinds derived from operation impacts.

    The matrix maps every derived impact to one or more of these kinds. The
    kinds are the sole authority for which core public verification API a
    group must satisfy before it can become review-ready. An agent never
    supplies them; they are derived from the normalized before/after state.
    """

    STATIC = "static"
    PHYSICAL = "physical"
    COMPATIBILITY = "compatibility"
    ACCEPTANCE = "acceptance"
    OKF = "okf"


#: Maps each derived impact flag to the mandatory checks it requires. This is
#: the exact impact -> mandatory-check matrix. Unknown impact flags (never
#: produced by the companion, but possibly supplied by a hostile agent record)
#: contribute no checks, so a fabricated impact can never widen the required
#: evidence.
_IMPACT_CHECK_MATRIX: Mapping[str, frozenset[str]] = {
    _IMPACT_OBJECT_ADDED: frozenset({MandatoryCheckKind.STATIC.value}),
    _IMPACT_OBJECT_EDITED: frozenset({MandatoryCheckKind.STATIC.value}),
    _IMPACT_ID_DEPRECATED: frozenset(
        {MandatoryCheckKind.STATIC.value, MandatoryCheckKind.COMPATIBILITY.value}
    ),
    _IMPACT_SOURCE_CHANGED: frozenset({MandatoryCheckKind.PHYSICAL.value}),
    _IMPACT_SCHEMA_CHANGED: frozenset({MandatoryCheckKind.PHYSICAL.value}),
    _IMPACT_GRAIN_CHANGED: frozenset({MandatoryCheckKind.PHYSICAL.value}),
    _IMPACT_RELATIONSHIP_CHANGED: frozenset({MandatoryCheckKind.PHYSICAL.value}),
    _IMPACT_AGGREGATION_CHANGED: frozenset(
        {
            MandatoryCheckKind.COMPATIBILITY.value,
            MandatoryCheckKind.ACCEPTANCE.value,
        }
    ),
    _IMPACT_FORMULA_CHANGED: frozenset(
        {
            MandatoryCheckKind.COMPATIBILITY.value,
            MandatoryCheckKind.ACCEPTANCE.value,
        }
    ),
    _IMPACT_REFERENCE_CHANGED: frozenset({MandatoryCheckKind.OKF.value}),
    _IMPACT_OVERLAY_CHANGED: frozenset({MandatoryCheckKind.OKF.value}),
    # ``type_changed`` and ``expression_changed`` contribute no *unconditional*
    # check beyond the static check carried by ``object_edited``. A reopenable
    # data audit (physical) is added by :func:`verify_proposal` when the group
    # cites observed data evidence ("reopenable data evidence when cited").
}


def mandatory_check_kinds(impacts: Sequence[str]) -> frozenset[str]:
    """Return the mandatory check kinds derived solely from ``impacts``.

    The matrix is the single authority for verification readiness. It is a
    pure function of the derived impact flags: an agent-supplied impact list
    is never honoured, and a fabricated or unknown flag contributes nothing.
    """

    result: set[str] = set()
    for flag in impacts:
        result |= _IMPACT_CHECK_MATRIX.get(flag, frozenset())
    return frozenset(result)


#: Accepted statuses for a mandatory check outcome. ``unavailable`` records a
#: check that could not complete (a required source was missing); ``failed``
#: records a check that completed but found an error; ``skipped`` records a
#: check intentionally not run. Readiness requires every mandatory check to be
#: ``passed``.
_CHECK_STATUSES: frozenset[str] = frozenset(
    {"passed", "failed", "unavailable", "skipped"}
)


@dataclass(frozen=True, slots=True)
class MandatoryCheck:
    """One mandatory verification outcome for a dependency group.

    The outcome is a safe, derived record: ``status`` is one of
    :data:`_CHECK_STATUSES`, ``code`` is a stable error code (or empty), and
    ``digest`` is a hash of the check's *inputs* (never raw evidence bodies,
    SQL, values, or errors). An agent never supplies these; they are derived
    from the verification report produced by the core public verification API.
    """

    kind: str
    status: str
    code: str
    digest: str

    def __post_init__(self) -> None:
        if self.kind not in {kind.value for kind in MandatoryCheckKind}:
            raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid check kind")
        if self.status not in _CHECK_STATUSES:
            raise ProposalError(
                CODE_PROPOSAL_INVALID, safe_detail="invalid check status"
            )
        if type(self.code) is not str:
            raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid check code")
        if type(self.digest) is not str or _HEX64_RE.match(self.digest) is None:
            raise ProposalError(
                CODE_PROPOSAL_INVALID, safe_detail="invalid check digest"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "status": self.status,
            "code": self.code,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class GroupReadiness:
    """The review-readiness verdict for one dependency group.

    ``ready`` is ``True`` only when every readiness gate is satisfied. Each
    unsatisfied gate contributes one stable blocker code to ``blockers``; the
    tuple is deterministically ordered so the verdict is stable on repeated
    unchanged inputs.
    """

    ready: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"ready": self.ready, "blockers": list(self.blockers)}


@dataclass(frozen=True, slots=True)
class GroupVerification:
    """The mandatory checks and readiness verdict for one dependency group."""

    group_id: str
    checks: tuple[MandatoryCheck, ...]
    readiness: GroupReadiness

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "checks": [check.to_dict() for check in self.checks],
            "readiness": self.readiness.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class VerificationBundle:
    """The immutable verification report for a proposal.

    The bundle binds every mandatory check to its group, computes each group's
    review-readiness, and carries a stable semantic ``fingerprint`` plus the
    ``input_hashes`` it was derived from. A second run with unchanged inputs
    produces an identical fingerprint because every field is a pure, safe
    function of the proposal, candidate, and verification outcomes (never of
    filesystem paths, timestamps, raw values, or error text).
    """

    proposal_id: str
    proposal_fingerprint: str
    candidate_fingerprint: str
    input_hashes: Mapping[str, str]
    groups: tuple[GroupVerification, ...]
    fingerprint: str

    def checks_for(self, group_id: str) -> tuple[MandatoryCheck, ...]:
        """Return the mandatory checks for ``group_id``."""

        for group in self.groups:
            if group.group_id == group_id:
                return group.checks
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="unknown group")

    def check(self, group_id: str, kind: str) -> MandatoryCheck:
        """Return the single mandatory check of ``kind`` for ``group_id``."""

        for check in self.checks_for(group_id):
            if check.kind == kind:
                return check
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="unknown check")

    def readiness_for(self, group_id: str) -> GroupReadiness:
        """Return the readiness verdict for ``group_id``."""

        for group in self.groups:
            if group.group_id == group_id:
                return group.readiness
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="unknown group")

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "input_hashes": dict(self.input_hashes),
            "groups": [group.to_dict() for group in self.groups],
            "fingerprint": self.fingerprint,
        }


# --------------------------------------------------------------------------- #
# Overlay vocabulary (mirrors selayer.okf.composition)                        #
# --------------------------------------------------------------------------- #

#: Frontmatter fields an overlay may carry. Generated frontmatter fields
#: (``type``, ``title``, ``description``, ``fingerprint``, ``status``,
#: ``replaced_by``, ``generated``) are intentionally absent: an overlay may not
#: edit generated frontmatter.
_ALLOWED_OVERLAY_FRONTMATTER: frozenset[str] = frozenset(
    {"selayer_id", "sources", "stale_after"}
)
#: Curated overlay sections. ``Catalog Definition`` is generated and may never
#: be edited by an overlay.
_ALLOWED_OVERLAY_SECTIONS: frozenset[str] = frozenset(
    {"Usage Guidance", "Examples", "Caveats", "Related Concepts"}
)

#: Subdirectories that belong to generated OKF output and may never be the
#: target of a knowledge operation.
_GENERATED_OKF_DIRS: frozenset[str] = frozenset(
    {"generated", "build", "dist", "output", "_build"}
)


# --------------------------------------------------------------------------- #
# Query cases                                                                 #
# --------------------------------------------------------------------------- #


#: Accepted query-case kinds (Task 16 carries cases structurally; Task 17
#: enforces their verification semantics).
_QUERY_CASE_KINDS: frozenset[str] = frozenset(
    {"compatible_plan", "planner_rejection", "execution_assertion"}
)

#: Accepted filter operators for a typed query case. SQL-like operators (``like``,
#: ``regex``, raw SQL fragments) are intentionally absent: a filter value is a
#: safe scalar or a bounded list/range, never executable text.
_ALLOWED_FILTER_OPERATORS: frozenset[str] = frozenset({"equals", "in", "between"})

#: Accepted bounded result-assertion operators. Unrestricted row capture
#: (``capture_rows``, ``result_set``) and arbitrary callable checks are
#: intentionally absent: an assertion records only a bounded pass/fail.
_ALLOWED_ASSERTION_OPERATORS: frozenset[str] = frozenset(
    {"row_count_max", "row_count_min", "non_empty"}
)

#: Stable planner rejection codes (mirrors :class:`QueryPlanningError` codes).
#: A ``planner_rejection`` case must cite one of these; an unknown code is
#: rejected so a fabricated code can never appear in a report.
_ALLOWED_REJECTION_CODES: frozenset[str] = frozenset(
    {
        "unknown_metric",
        "unknown_dimension",
        "unknown_filter_dimension",
        "duplicate_output_name",
        "mixed_grain",
        "ambiguous_relationship_path",
        "row_expanding_path",
        "no_relationship_path",
        # ``invalid_filter_type`` is raised by the core planner when a filter
        # value's type does not match a dimension's declared data type.
        "invalid_filter_type",
    }
)

#: Keys permitted on a query-case mapping (the allowlist). Every other key
#: (``sql``, ``check``, ``capture_rows``, ``rows``, ``script``, ...) is
#: rejected as an unknown key so SQL, callable assertions, and unrestricted
#: row capture can never reach verification.
_CASE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "case_id",
        "kind",
        "description",
        "metrics",
        "dimensions",
        "filters",
        "expected_rejection_code",
        "assertions",
    }
)


@dataclass(frozen=True, slots=True)
class CaseFilter:
    """A safe, typed filter bound to a candidate dimension.

    ``operator`` is one of :data:`_ALLOWED_FILTER_OPERATORS`. ``value`` is a
    safe scalar (``equals``), a tuple of scalars (``in``), or a two-tuple
    ``(start, end)`` (``between``). It never carries SQL or a callable.
    """

    dimension_id: str
    operator: str
    value: object


@dataclass(frozen=True, slots=True)
class CaseAssertion:
    """A bounded result assertion over an executed query.

    ``operator`` is one of :data:`_ALLOWED_ASSERTION_OPERATORS`. ``value`` is
    a non-negative int for ``row_count_max`` / ``row_count_min``. The assertion
    records only a bounded pass/fail; it never captures result rows.
    """

    operator: str
    value: object


def _is_safe_scalar(value: object) -> bool:
    return (
        value is None
        or type(value) is str
        or type(value) is int
        or type(value) is float
        or type(value) is bool
    )


def _validate_case_selector_value(value: object) -> object:
    """Return ``value`` as a safe scalar or tuple of safe scalars."""

    if _is_safe_scalar(value):
        return value
    if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
        frozen: list[object] = []
        for item in value:
            if not _is_safe_scalar(item):
                raise ProposalError(
                    CODE_PROPOSAL_INVALID, safe_detail="invalid case filter"
                )
            frozen.append(item)
        return tuple(frozen)
    raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case filter")


def _build_case_filter(data: object) -> CaseFilter:
    if not isinstance(data, Mapping):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case filter")
    unknown = set(data) - {"dimension_id", "operator", "value"}
    if unknown:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case filter")
    if {"dimension_id", "operator"} - set(data):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case filter")
    dimension_id = _validate_id(
        data["dimension_id"], regex=_NODE_ID_RE, detail="invalid case filter"
    )
    operator = data["operator"]
    if type(operator) is not str or operator not in _ALLOWED_FILTER_OPERATORS:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case filter")
    value = _validate_case_selector_value(data.get("value"))
    return CaseFilter(dimension_id=dimension_id, operator=operator, value=value)


def _build_case_assertion(data: object) -> CaseAssertion:
    if not isinstance(data, Mapping):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case assertion")
    unknown = set(data) - {"operator", "value"}
    if unknown:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case assertion")
    if "operator" not in data:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case assertion")
    operator = data["operator"]
    if type(operator) is not str or operator not in _ALLOWED_ASSERTION_OPERATORS:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case assertion")
    value = data.get("value")
    if operator in ("row_count_max", "row_count_min") and (
        type(value) is not int or isinstance(value, bool) or value < 0
    ):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case assertion")
    return CaseAssertion(operator=operator, value=value)


@dataclass(frozen=True, slots=True)
class QueryCase:
    """A structured acceptance or counterexample query case.

    Task 16 carries query cases as structured, bounded values so a group's
    fingerprint is stable. Task 17 binds them to verification semantics
    (expected compatible plans, stable planner rejection codes, and optional
    bounded execution assertions).

    The payload is typed and safe: selectors carry safe scalars or bounded
    lists/ranges, assertions carry bounded row-count operators, and SQL,
    callable assertions, unrestricted row capture, and unknown operators are
    rejected at construction.
    """

    case_id: str
    kind: str
    description: str
    metrics: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    filters: tuple[CaseFilter, ...] = ()
    expected_rejection_code: str = ""
    assertions: tuple[CaseAssertion, ...] = ()

    def __post_init__(self) -> None:
        if not _NODE_ID_RE.match(self.case_id):
            raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case id")
        if self.kind not in _QUERY_CASE_KINDS:
            raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case kind")
        if type(self.description) is not str or len(self.description) > MAX_TEXT_LENGTH:
            raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case text")
        for metric in self.metrics:
            if not _NODE_ID_RE.match(metric):
                raise ProposalError(
                    CODE_PROPOSAL_INVALID, safe_detail="invalid case metric"
                )
        for dimension in self.dimensions:
            if not _NODE_ID_RE.match(dimension):
                raise ProposalError(
                    CODE_PROPOSAL_INVALID, safe_detail="invalid case dimension"
                )
        if (
            self.expected_rejection_code
            and self.expected_rejection_code not in _ALLOWED_REJECTION_CODES
        ):
            raise ProposalError(
                CODE_PROPOSAL_INVALID, safe_detail="invalid rejection code"
            )

    @classmethod
    def from_mapping(cls, data: object) -> QueryCase:
        """Validate a caller-supplied mapping and return a :class:`QueryCase`."""

        return _build_case(data)


# --------------------------------------------------------------------------- #
# Operation                                                                   #
# --------------------------------------------------------------------------- #


def _validate_id(value: object, *, regex: re.Pattern[str], detail: str) -> str:
    if type(value) is not str or regex.match(value) is None:
        raise ProposalError(CODE_PROPOSAL_OPERATION_INVALID, safe_detail=detail)
    return value


def _validate_id_list(
    value: object,
    *,
    regex: re.Pattern[str],
    detail: str,
) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ProposalError(CODE_PROPOSAL_OPERATION_INVALID, safe_detail=detail)
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        validated = _validate_id(item, regex=regex, detail=detail)
        if validated in seen:
            raise ProposalError(CODE_PROPOSAL_OPERATION_INVALID, safe_detail=detail)
        seen.add(validated)
        result.append(validated)
        if len(result) > _MAX_OPERATIONS:
            raise ProposalError(CODE_PROPOSAL_OPERATION_INVALID, safe_detail=detail)
    return tuple(result)


def _normalize_catalog_state(value: object) -> dict[str, object]:
    """Return ``value`` as a bounded JSON-native mapping for a catalog object."""

    try:
        mapping = bounded_mapping(value)
    except Exception:  # noqa: BLE001 - bounded_mapping raises DiscoveryError
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="invalid catalog state"
        ) from None
    try:
        normalized = canonical.normalize_artifact(mapping)
    except Exception:  # noqa: BLE001 - canonical raises UnsupportedArtifactError
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="invalid catalog state"
        ) from None
    if not isinstance(normalized, dict):
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="invalid catalog state"
        ) from None
    return cast("dict[str, object]", normalized)


def _validate_catalog_state_text(value: object) -> None:
    """Recursively bound every string in a normalized catalog state."""

    if type(value) is str:
        if len(value) > MAX_TEXT_LENGTH:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID, safe_detail="oversized prose"
            )
    elif isinstance(value, Mapping):
        for item in value.values():
            _validate_catalog_state_text(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _validate_catalog_state_text(item)


def _knowledge_subject(kind: OperationKind) -> KnowledgeSubject:
    if kind in (OperationKind.REFERENCE_CREATE, OperationKind.REFERENCE_UPDATE):
        return KnowledgeSubject.REFERENCE
    return KnowledgeSubject.OVERLAY


def _is_create(kind: OperationKind) -> bool:
    return kind in (
        OperationKind.CATALOG_ADD,
        OperationKind.REFERENCE_CREATE,
        OperationKind.OVERLAY_CREATE,
    )


def _parse_knowledge_target(target_id: str, subject: KnowledgeSubject) -> str:
    """Validate and return a knowledge operation relative path."""

    if _KNOWLEDGE_PATH_RE.match(target_id) is None:
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="invalid knowledge target"
        )
    pure = PurePosixPath(target_id)
    # Reject any parent traversal component explicitly (defence in depth: the
    # regex already forbids ``..`` but PurePosixPath normalizes it).
    if any(part == ".." for part in pure.parts):
        raise ProposalError(CODE_PROPOSAL_OPERATION_INVALID, safe_detail="path escape")
    # Reject generated OKF output directories.
    if pure.parts and pure.parts[0] in _GENERATED_OKF_DIRS:
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="generated okf target"
        )
    # Overlay paths are conventionally grouped by semantic kind directory; the
    # subject only selects the root (references vs overlays), not the layout.
    _ = subject
    return target_id


def _split_frontmatter_and_sections(text: str) -> tuple[dict[str, object], list[str]]:
    """Parse a markdown document into frontmatter and section headings.

    Returns a ``(frontmatter, sections)`` pair where ``frontmatter`` is a dict
    (empty when absent or unparseable) and ``sections`` is the ordered list of
    ``## `` heading titles. Frontmatter parsing is intentionally lenient about
    shape (it only needs the keys), but malformed YAML is rejected.
    """

    frontmatter: dict[str, object] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end == -1:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="malformed overlay document",
            )
        block = text[3:end].strip("\n")
        try:
            loaded = YAML(typ="safe").load(block)
        except YAMLError:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="malformed overlay document",
            ) from None
        if loaded is None:
            frontmatter = {}
        elif isinstance(loaded, Mapping):
            frontmatter = {str(k): v for k, v in loaded.items()}
        else:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="malformed overlay document",
            ) from None
        body = text[end + 4 :]
    sections: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            sections.append(line[3:].strip())
    return frontmatter, sections


def _validate_overlay_document(text: str) -> None:
    """Reject generated frontmatter or curated-section violations in an overlay."""

    frontmatter, sections = _split_frontmatter_and_sections(text)
    for key in frontmatter:
        if key not in _ALLOWED_OVERLAY_FRONTMATTER:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="generated frontmatter edit",
            )
    for section in sections:
        if section == "Catalog Definition":
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="catalog definition edit",
            )
        if section not in _ALLOWED_OVERLAY_SECTIONS:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="disallowed overlay section",
            )


def _validate_source_after_shape(after: Mapping[str, object]) -> None:
    """Require the complete source fields needed by a strict catalog load.

    A data source is fully described by a connector (``type``), a declared
    schema (inline ``schema`` or a ``schema_ref``), and a non-empty ``grain``.
    This rejects an after-state that only carries ``grain`` (or grain without
    a schema) so a proposal never produces a candidate source that fails to
    load through :meth:`SemanticLayer.load`.
    """

    grain = after.get("grain")
    if not isinstance(grain, list) or not grain:
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="incomplete after state"
        )
    connector_type = after.get("type")
    if type(connector_type) is not str or not connector_type:
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="incomplete after state"
        )
    has_schema = isinstance(after.get("schema"), Mapping)
    has_schema_ref = type(after.get("schema_ref")) is str and bool(
        after.get("schema_ref")
    )
    if not has_schema and not has_schema_ref:
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="incomplete after state"
        )


def _validate_catalog_after_shape(
    kind: OperationKind,
    target_kind: str,
    after: Mapping[str, object],
) -> None:
    """Reject arbitrary patch input and target-kind changes for catalog ops."""

    if not isinstance(after, Mapping) or not after:
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="incomplete after state"
        )
    # A complete normalized object state must declare the discriminator fields
    # of its semantic kind. This rejects arbitrary patch input (e.g. JSON-patch
    # ``op``/``path``/``value`` shapes) that does not describe a real object.
    required = _KIND_DISCRIMINATORS[target_kind]
    if target_kind == "source":
        # A data source is fully described by a connector, a schema, and a
        # grain. Require the complete set of fields needed by a strict
        # ``SemanticLayer`` load so a source after-state that only carries
        # ``grain`` (or grain without a schema) is rejected here rather than
        # producing a candidate that fails to load.
        _validate_source_after_shape(after)
    else:
        missing = required - set(after)
        if missing:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="incomplete after state",
            )
    # Reject JSON-patch-style input explicitly.
    if "op" in after and "path" in after:
        raise ProposalError(
            CODE_PROPOSAL_OPERATION_INVALID, safe_detail="arbitrary patch input"
        )
    # A deprecate operation must set status=deprecated in its after state.
    if kind is OperationKind.CATALOG_DEPRECATE:
        status = after.get("status")
        if status != "deprecated":
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="deprecate without status",
            )


def _changed_fields(
    before: Mapping[str, object] | None,
    after: Mapping[str, object],
) -> tuple[str, ...]:
    """Derive the sorted changed-field set from normalized before/after."""

    if before is None:
        # An add carries a complete new object; every declared field is added.
        return tuple(sorted(after.keys()))
    changed: set[str] = set()
    for key, value in after.items():
        if key not in before or before[key] != value:
            changed.add(key)
    for key in before:
        if key not in after:
            changed.add(key)
    return tuple(sorted(changed))


def _derived_impacts(
    kind: OperationKind,
    target_kind: str,
    before: Mapping[str, object] | None,
    after: Mapping[str, object],
    changed: Sequence[str],
) -> tuple[str, ...]:
    """Derive impact flags from normalized before/after (never from an agent)."""

    impacts: list[str] = []
    if kind is OperationKind.CATALOG_ADD:
        impacts.append(_IMPACT_OBJECT_ADDED)
        # An added object with a grain/schema contributes structural impacts.
        if target_kind == "source":
            impacts.extend((_IMPACT_SOURCE_CHANGED, _IMPACT_SCHEMA_CHANGED))
        elif target_kind == "relationship":
            impacts.append(_IMPACT_RELATIONSHIP_CHANGED)
    elif kind is OperationKind.CATALOG_DEPRECATE:
        impacts.append(_IMPACT_ID_DEPRECATED)
        impacts.append(_IMPACT_OBJECT_EDITED)
    else:
        impacts.append(_IMPACT_OBJECT_EDITED)
    changed_set = set(changed)
    for field_name, flag in _FIELD_IMPACTS.items():
        if field_name in changed_set and flag not in impacts:
            impacts.append(flag)
    # De-duplicate while preserving the deterministic insertion order.
    seen: set[str] = set()
    ordered: list[str] = []
    for flag in impacts:
        if flag not in seen:
            seen.add(flag)
            ordered.append(flag)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class Operation:
    """An immutable, validated typed catalog or knowledge operation.

    Attributes:
        operation_id: stable operation identifier.
        kind: one of the seven normalized operation kinds.
        target_id: fully-qualified catalog target id (``dimension.foo``) or a
            knowledge relative path (``dimensions/foo.md``).
        before: normalized before state (a catalog object mapping, a knowledge
            document text, or ``None`` for create/add).
        before_hash: SHA-256 fingerprint of the normalized before state, or
            :data:`GENESIS_HASH` for create/add.
        after: normalized after state (a catalog object mapping or a knowledge
            document text).
        claim_ids: evidence claim ids cited by this operation.
        group_ids: dependency-group ids this operation belongs to.
        changed_fields: sorted changed-field set derived from before/after.
        impacts: derived impact flags (never agent-supplied).
    """

    operation_id: str
    kind: OperationKind
    target_id: str
    before: object | None
    before_hash: str
    after: object
    claim_ids: tuple[str, ...]
    group_ids: tuple[str, ...]
    changed_fields: tuple[str, ...]
    impacts: tuple[str, ...]

    @classmethod
    def from_mapping(cls, data: object) -> Operation:
        """Validate a caller-supplied mapping and return an :class:`Operation`."""

        if not isinstance(data, Mapping):
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="operation must be a mapping",
            )
        allowed = {
            "operation_id",
            "kind",
            "target_id",
            "before",
            "before_hash",
            "after",
            "claim_ids",
            "group_ids",
            # The companion derives ``impacts`` and ``changed_fields`` from the
            # normalized before/after state; an agent (or a round-tripped
            # record) may supply them, but the companion ignores them entirely
            # and re-derives them so a stale or hostile list can never reach
            # verification or apply authority.
            "impacts",
            "changed_fields",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID, safe_detail="unknown operation key"
            )
        required = allowed - {"impacts", "changed_fields"}
        missing = required - set(data)
        if missing:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID, safe_detail="missing operation field"
            )
        operation_id = _validate_id(
            data["operation_id"],
            regex=_NODE_ID_RE,
            detail="invalid operation id",
        )
        raw_kind = data["kind"]
        try:
            kind = OperationKind(raw_kind)
        except ValueError:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID, safe_detail="unknown operation kind"
            ) from None
        target_id_raw = data["target_id"]
        if type(target_id_raw) is not str or not target_id_raw:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID, safe_detail="invalid target id"
            )
        before_raw = data["before"]
        after_raw = data["after"]
        before_hash_raw = data["before_hash"]
        if type(before_hash_raw) is not str or _HEX64_RE.match(before_hash_raw) is None:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID, safe_detail="invalid before hash"
            )
        claim_ids = _validate_id_list(
            data["claim_ids"], regex=_CLAIM_ID_RE, detail="invalid claim id"
        )
        group_ids = _validate_id_list(
            data["group_ids"], regex=_NODE_ID_RE, detail="invalid group id"
        )
        return cls._construct(
            operation_id=operation_id,
            kind=kind,
            target_id=target_id_raw,
            before_raw=before_raw,
            after_raw=after_raw,
            before_hash=before_hash_raw,
            claim_ids=claim_ids,
            group_ids=group_ids,
        )

    @classmethod
    def _construct(
        cls,
        *,
        operation_id: str,
        kind: OperationKind,
        target_id: str,
        before_raw: object,
        after_raw: object,
        before_hash: str,
        claim_ids: tuple[str, ...],
        group_ids: tuple[str, ...],
    ) -> Operation:
        is_create = _is_create(kind)
        if is_create:
            if before_raw is not None:
                raise ProposalError(
                    CODE_PROPOSAL_OPERATION_INVALID,
                    safe_detail="create with before state",
                )
            if before_hash != GENESIS_HASH:
                raise ProposalError(
                    CODE_PROPOSAL_OPERATION_INVALID,
                    safe_detail="create with non-genesis hash",
                )
        else:
            if before_raw is None:
                raise ProposalError(
                    CODE_PROPOSAL_OPERATION_INVALID,
                    safe_detail="missing before state",
                )
            if before_hash == GENESIS_HASH:
                raise ProposalError(
                    CODE_PROPOSAL_OPERATION_INVALID,
                    safe_detail="edit with genesis hash",
                )

        if kind in CATALOG_KINDS:
            return cls._construct_catalog(
                operation_id=operation_id,
                kind=kind,
                target_id=target_id,
                before_raw=before_raw,
                after_raw=after_raw,
                before_hash=before_hash,
                claim_ids=claim_ids,
                group_ids=group_ids,
            )
        return cls._construct_knowledge(
            operation_id=operation_id,
            kind=kind,
            target_id=target_id,
            before_raw=before_raw,
            after_raw=after_raw,
            before_hash=before_hash,
            claim_ids=claim_ids,
            group_ids=group_ids,
        )

    @classmethod
    def _construct_catalog(
        cls,
        *,
        operation_id: str,
        kind: OperationKind,
        target_id: str,
        before_raw: object,
        after_raw: object,
        before_hash: str,
        claim_ids: tuple[str, ...],
        group_ids: tuple[str, ...],
    ) -> Operation:
        match = _CATALOG_TARGET_RE.match(target_id)
        if match is None:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID, safe_detail="invalid catalog target"
            )
        target_kind, object_name = match.group(1), match.group(2)
        after = _normalize_catalog_state(after_raw)
        _validate_catalog_state_text(after)
        _validate_catalog_after_shape(kind, target_kind, after)
        before_norm: dict[str, object] | None
        if before_raw is None:
            before_norm = None
        else:
            before_norm = _normalize_catalog_state(before_raw)
            _validate_catalog_state_text(before_norm)
            # The before state must belong to the same semantic kind family
            # (reject a target-kind change).
            if not _same_kind_family(target_kind, before_norm):
                raise ProposalError(
                    CODE_PROPOSAL_OPERATION_INVALID,
                    safe_detail="target kind change",
                )
            # Reject a rename: the object name is fixed by the target id and
            # the before/after shapes must remain the same object.
            if not _same_kind_family(target_kind, after):
                raise ProposalError(
                    CODE_PROPOSAL_OPERATION_INVALID,
                    safe_detail="target kind change",
                )
            # The before-hash must match the normalized before state.
            if canonical.fingerprint(before_norm) != before_hash:
                raise ProposalError(
                    CODE_PROPOSAL_OPERATION_INVALID,
                    safe_detail="before hash mismatch",
                )
            # Reject an edit that changes the object's identity-defining column
            # (a rename in disguise) for dimensions/facts/relationships.
            _reject_identity_rename(target_kind, before_norm, after)
        changed = _changed_fields(before_norm, after)
        impacts = _derived_impacts(kind, target_kind, before_norm, after, changed)
        _ = object_name  # name is validated by the regex; reconstruction uses it
        return cls(
            operation_id=operation_id,
            kind=kind,
            target_id=target_id,
            before=before_norm,
            before_hash=before_hash,
            after=after,
            claim_ids=claim_ids,
            group_ids=group_ids,
            changed_fields=changed,
            impacts=impacts,
        )

    @classmethod
    def _construct_knowledge(
        cls,
        *,
        operation_id: str,
        kind: OperationKind,
        target_id: str,
        before_raw: object,
        after_raw: object,
        before_hash: str,
        claim_ids: tuple[str, ...],
        group_ids: tuple[str, ...],
    ) -> Operation:
        subject = _knowledge_subject(kind)
        target_path = _parse_knowledge_target(target_id, subject)
        if type(after_raw) is not str or not after_raw:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID,
                safe_detail="invalid knowledge after state",
            )
        if len(after_raw) > MAX_TEXT_LENGTH:
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID, safe_detail="oversized prose"
            )
        before_text: str | None
        if before_raw is None:
            before_text = None
        else:
            if type(before_raw) is not str:
                raise ProposalError(
                    CODE_PROPOSAL_OPERATION_INVALID,
                    safe_detail="invalid knowledge before state",
                )
            before_text = before_raw
            if canonical.fingerprint(before_text) != before_hash:
                raise ProposalError(
                    CODE_PROPOSAL_OPERATION_INVALID,
                    safe_detail="before hash mismatch",
                )
        if subject is KnowledgeSubject.OVERLAY:
            _validate_overlay_document(after_raw)
        impacts: tuple[str, ...]
        if subject is KnowledgeSubject.REFERENCE:
            impacts = (_IMPACT_REFERENCE_CHANGED,)
        else:
            impacts = (_IMPACT_OVERLAY_CHANGED,)
        changed = (
            () if before_text is None else _text_changed_fields(before_text, after_raw)
        )
        return cls(
            operation_id=operation_id,
            kind=kind,
            target_id=target_path,
            before=before_text,
            before_hash=before_hash,
            after=after_raw,
            claim_ids=claim_ids,
            group_ids=group_ids,
            changed_fields=changed,
            impacts=impacts,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping (before/after are returned verbatim)."""

        return {
            "operation_id": self.operation_id,
            "kind": self.kind.value,
            "target_id": self.target_id,
            "before": self.before,
            "before_hash": self.before_hash,
            "after": self.after,
            "claim_ids": list(self.claim_ids),
            "group_ids": list(self.group_ids),
            "changed_fields": list(self.changed_fields),
            "impacts": list(self.impacts),
        }


def _same_kind_family(target_kind: str, state: Mapping[str, object]) -> bool:
    """Return whether ``state`` satisfies the discriminator of ``target_kind``."""

    required = _KIND_DISCRIMINATORS[target_kind]
    if target_kind == "source":
        # Sources are identified by ``grain`` plus a connector/schema.
        return "grain" in state and ("type" in state or "schema" in state)
    return required.issubset(state.keys())


def _reject_identity_rename(
    target_kind: str,
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> None:
    """Reject a rename disguised as an edit.

    For dimensions and relationships, the column/join columns are the object's
    identity. Changing them is a rename and is rejected. (A genuine edit may
    still change ``description``, ``data_type``, ``status``, etc.)
    """

    identity_fields = {
        "dimension": ("source", "column"),
        "relationship": ("source", "target", "source_column", "target_column"),
        "source": ("name",),
        "fact": ("source",),
        "measure": ("fact",),
        "metric": (),
    }.get(target_kind, ())
    for field_name in identity_fields:
        if before.get(field_name) != after.get(field_name):
            raise ProposalError(
                CODE_PROPOSAL_OPERATION_INVALID, safe_detail="rename rejected"
            )


def _text_changed_fields(before: str, after: str) -> tuple[str, ...]:
    """Return a coarse changed-field marker for knowledge text edits."""

    return ("content",) if before != after else ()


# --------------------------------------------------------------------------- #
# Dependency group                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DependencyGroup:
    """An atomic, acyclic dependency group of typed operations."""

    group_id: str
    title: str
    rationale: str
    dependencies: tuple[str, ...]
    supporting_claim_ids: tuple[str, ...]
    inferred_claim_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    affecting_gates: tuple[str, ...]
    query_cases: tuple[QueryCase, ...]
    operations: tuple[Operation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "title": self.title,
            "rationale": self.rationale,
            "dependencies": list(self.dependencies),
            "supporting_claim_ids": list(self.supporting_claim_ids),
            "inferred_claim_ids": list(self.inferred_claim_ids),
            "conflict_ids": list(self.conflict_ids),
            "affecting_gates": list(self.affecting_gates),
            "query_cases": [
                {"case_id": c.case_id, "kind": c.kind, "description": c.description}
                for c in self.query_cases
            ],
            "operations": [op.to_dict() for op in self.operations],
        }


def _validate_text(value: object, *, detail: str) -> str:
    if type(value) is not str or not value.strip():
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail=detail)
    if len(value) > MAX_TEXT_LENGTH:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail=detail)
    return value


def _build_group(data: Mapping[str, object]) -> DependencyGroup:
    allowed = {
        "group_id",
        "title",
        "rationale",
        "dependencies",
        "supporting_claim_ids",
        "inferred_claim_ids",
        "conflict_ids",
        "affecting_gates",
        "query_cases",
        "operations",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="unknown group key")
    missing = allowed - set(data)
    if missing:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="missing group field")
    group_id = _validate_id(
        data["group_id"], regex=_NODE_ID_RE, detail="invalid group id"
    )
    title = _validate_text(data["title"], detail="invalid group title")
    rationale = _validate_text(data["rationale"], detail="invalid rationale")
    dependencies = _validate_id_list(
        data["dependencies"], regex=_NODE_ID_RE, detail="invalid dependency"
    )
    supporting = _validate_id_list(
        data["supporting_claim_ids"],
        regex=_CLAIM_ID_RE,
        detail="invalid claim id",
    )
    inferred = _validate_id_list(
        data["inferred_claim_ids"],
        regex=_CLAIM_ID_RE,
        detail="invalid claim id",
    )
    conflicts = _validate_id_list(
        data["conflict_ids"], regex=_CONFLICT_ID_RE, detail="invalid conflict id"
    )
    gates = _validate_id_list(
        data["affecting_gates"], regex=_GATE_ID_RE, detail="invalid gate id"
    )
    raw_cases = data["query_cases"]
    if isinstance(raw_cases, str) or not isinstance(raw_cases, Sequence):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid query cases")
    if len(raw_cases) > _MAX_QUERY_CASES:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid query cases")
    cases = tuple(_build_case(case) for case in raw_cases)
    raw_ops = data["operations"]
    if isinstance(raw_ops, str) or not isinstance(raw_ops, Sequence):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid operations")
    if not raw_ops:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="empty operations")
    if len(raw_ops) > _MAX_OPERATIONS:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid operations")
    operations = tuple(Operation.from_mapping(op) for op in raw_ops)
    return DependencyGroup(
        group_id=group_id,
        title=title,
        rationale=rationale,
        dependencies=dependencies,
        supporting_claim_ids=supporting,
        inferred_claim_ids=inferred,
        conflict_ids=conflicts,
        affecting_gates=gates,
        query_cases=cases,
        operations=operations,
    )


def _build_case(data: object) -> QueryCase:
    if not isinstance(data, Mapping):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid query case")
    unknown = set(data) - _CASE_ALLOWED_KEYS
    if unknown:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="unknown case key")
    missing = {"case_id", "kind", "description"} - set(data)
    if missing:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="missing case field")
    metrics = _validate_id_list(
        data.get("metrics", ()), regex=_NODE_ID_RE, detail="invalid case metric"
    )
    dimensions = _validate_id_list(
        data.get("dimensions", ()), regex=_NODE_ID_RE, detail="invalid case dimension"
    )
    raw_filters = data.get("filters", ())
    if isinstance(raw_filters, str) or not isinstance(raw_filters, Sequence):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case filter")
    filters = tuple(_build_case_filter(item) for item in raw_filters)
    raw_assertions = data.get("assertions", ())
    if isinstance(raw_assertions, str) or not isinstance(raw_assertions, Sequence):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid case assertion")
    assertions = tuple(_build_case_assertion(item) for item in raw_assertions)
    expected = data.get("expected_rejection_code", "")
    if type(expected) is not str:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid rejection code")
    return QueryCase(
        case_id=_validate_id(
            data["case_id"], regex=_NODE_ID_RE, detail="invalid case id"
        ),
        kind=cast("str", data["kind"]),
        description=cast("str", data["description"]),
        metrics=metrics,
        dimensions=dimensions,
        filters=filters,
        expected_rejection_code=expected,
        assertions=assertions,
    )


# --------------------------------------------------------------------------- #
# Proposal                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Proposal:
    """A versioned, deterministically-ordered collection of dependency groups."""

    proposal_id: str
    title: str
    groups: tuple[DependencyGroup, ...]
    operations: tuple[Operation, ...]
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "title": self.title,
            "groups": [group.to_dict() for group in self.groups],
            "operations": [op.to_dict() for op in self.operations],
            "fingerprint": self.fingerprint,
        }


def _reject_dependency_cycles(groups: Sequence[DependencyGroup]) -> None:
    """Reject self-dependencies, unknown dependencies, and cycles."""

    by_id = {group.group_id: group for group in groups}
    for group in groups:
        if group.group_id in group.dependencies:
            raise ProposalError(CODE_PROPOSAL_CYCLE, safe_detail="self dependency")
        for dep in group.dependencies:
            if dep not in by_id:
                raise ProposalError(
                    CODE_PROPOSAL_INVALID, safe_detail="unknown dependency"
                )
    # Depth-first cycle detection over the group dependency graph.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {gid: WHITE for gid in by_id}

    def visit(node: str) -> None:
        color[node] = GRAY
        for dep in by_id[node].dependencies:
            if color[dep] == GRAY:
                raise ProposalError(CODE_PROPOSAL_CYCLE, safe_detail="dependency cycle")
            if color[dep] == WHITE:
                visit(dep)
        color[node] = BLACK

    for gid in by_id:
        if color[gid] == WHITE:
            visit(gid)


def _reject_overlapping_targets(groups: Sequence[DependencyGroup]) -> None:
    """Reject two operations across the proposal that target the same object.

    Overlapping operations must be combined into one atomic dependency group.
    """

    seen: set[tuple[str, str]] = set()
    for group in groups:
        for op in group.operations:
            key: tuple[str, str] = (op.kind.value, op.target_id)
            if key in seen:
                raise ProposalError(
                    CODE_PROPOSAL_OVERLAPPING_TARGET,
                    safe_detail="overlapping target",
                )
            seen.add(key)


def build_proposal(data: object) -> Proposal:
    """Validate a caller-supplied mapping and return a :class:`Proposal`."""

    if not isinstance(data, Mapping):
        raise ProposalError(
            CODE_PROPOSAL_INVALID, safe_detail="proposal must be a mapping"
        )
    allowed = {"proposal_id", "title", "groups"}
    unknown = set(data) - allowed
    if unknown:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="unknown proposal key")
    missing = allowed - set(data)
    if missing:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="missing proposal field")
    proposal_id = _validate_id(
        data["proposal_id"], regex=_NODE_ID_RE, detail="invalid proposal id"
    )
    title = _validate_text(data["title"], detail="invalid proposal title")
    raw_groups = data["groups"]
    if isinstance(raw_groups, str) or not isinstance(raw_groups, Sequence):
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid groups")
    if not raw_groups:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="empty groups")
    if len(raw_groups) > _MAX_GROUPS:
        raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="invalid groups")
    groups = tuple(_build_group(group) for group in raw_groups)
    _reject_dependency_cycles(groups)
    _reject_overlapping_targets(groups)
    operations = tuple(
        sorted(
            (op for group in groups for op in group.operations),
            key=lambda op: (op.operation_id, op.target_id),
        )
    )
    fingerprint = canonical.fingerprint(
        {
            "proposal_id": proposal_id,
            "title": title,
            "groups": [group.to_dict() for group in groups],
        }
    )
    return Proposal(
        proposal_id=proposal_id,
        title=title,
        groups=groups,
        operations=operations,
        fingerprint=fingerprint,
    )


# --------------------------------------------------------------------------- #
# Candidate reconstruction                                                    #
# --------------------------------------------------------------------------- #


def _round_trip_yaml(*, line_break: str = "\n") -> YAML:
    """Return a ``ruamel.yaml`` round-trip parser (preserves formatting).

    ``line_break`` threads the authored newline style (``"\r\n"`` for CRLF,
    ``"\n"`` otherwise) through load and dump so a candidate never silently
    normalizes authored line endings.
    """

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    # ``line_break`` accepts ``"\r\n"`` / ``"\n"`` at runtime; the ruamel
    # type stubs declare it ``None``, so assign through a cast.
    yaml.line_break = cast("Any", line_break)
    return yaml


@dataclass(frozen=True, slots=True)
class Candidate:
    """A reconstructed candidate from a proposal applied to a base.

    The candidate is immutable and never touches the filesystem. Callers that
    need to load the reconstructed catalog through
    :meth:`SemanticLayer.load` write it with :func:`write_candidate` first.
    """

    catalog_text: str
    catalog_fingerprint: str
    references: tuple[tuple[str, str], ...]
    overlays: tuple[tuple[str, str], ...]
    fingerprint: str


def _apply_catalog_operation(
    data: Any,
    op: Operation,
    *,
    line_break: str = "\n",
) -> None:
    """Apply one catalog operation to a round-trip ``ruamel.yaml`` mapping."""

    match = _CATALOG_TARGET_RE.match(op.target_id)
    assert match is not None  # validated at construction
    target_kind, object_name = match.group(1), match.group(2)
    collection_key = _CATALOG_COLLECTION_BY_PREFIX[target_kind]
    collection = data.get(collection_key)
    if collection is None:
        collection = data[collection_key] = {}
    if op.kind is OperationKind.CATALOG_ADD:
        if object_name in collection:
            raise ProposalError(
                CODE_PROPOSAL_RECONSTRUCTION_FAILED,
                safe_detail="add target exists",
            )
        collection[object_name] = _to_ruamel(op.after, line_break=line_break)
        return
    if object_name not in collection:
        raise ProposalError(
            CODE_PROPOSAL_RECONSTRUCTION_FAILED,
            safe_detail="edit target missing",
        )
    existing = collection[object_name]
    if op.kind is OperationKind.CATALOG_DEPRECATE:
        # Deprecation sets status/replaced_by on the existing object while
        # preserving untouched keys and comments where possible.
        after = op.after
        assert isinstance(after, Mapping)
        for key, value in after.items():
            existing[key] = _to_ruamel(value, line_break=line_break)
        return
    # catalog.edit: replace the object wholesale with the normalized after
    # state, preserving the collection's key order by re-inserting in place.
    collection[object_name] = _to_ruamel(op.after, line_break=line_break)


def _to_ruamel(value: object, *, line_break: str = "\n") -> Any:
    """Round-trip a JSON-native value through ``ruamel.yaml`` for in-place use."""

    if isinstance(value, Mapping):
        result: Any = _round_trip_yaml(line_break=line_break).map()
        for key, item in value.items():
            result[key] = _to_ruamel(item, line_break=line_break)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        seq = _round_trip_yaml(line_break=line_break).seq()
        for item in value:
            seq.append(_to_ruamel(item, line_break=line_break))
        return seq
    return value


def _dump_yaml(data: Any, *, line_break: str = "\n") -> str:
    buf = io.StringIO()
    _round_trip_yaml(line_break=line_break).dump(data, buf)
    return buf.getvalue()


def _apply_knowledge_operations(
    base: Mapping[str, str],
    operations: Sequence[Operation],
    *,
    subject: KnowledgeSubject,
) -> tuple[tuple[str, str], ...]:
    """Return the reconstructed knowledge files for one subject.

    Only operations whose :func:`_knowledge_subject` equals ``subject`` are
    applied. A reference operation therefore never appears in the overlay base
    and vice versa, regardless of which base it is applied to.
    """

    result: dict[str, str] = dict(base)
    for op in operations:
        if op.kind not in KNOWLEDGE_KINDS:
            continue
        if _knowledge_subject(op.kind) is not subject:
            continue
        if op.kind in (
            OperationKind.REFERENCE_CREATE,
            OperationKind.OVERLAY_CREATE,
        ):
            if op.target_id in result:
                raise ProposalError(
                    CODE_PROPOSAL_RECONSTRUCTION_FAILED,
                    safe_detail="create target exists",
                )
            result[op.target_id] = cast("str", op.after)
        else:
            if op.target_id not in result:
                raise ProposalError(
                    CODE_PROPOSAL_RECONSTRUCTION_FAILED,
                    safe_detail="update target missing",
                )
            result[op.target_id] = cast("str", op.after)
    return tuple(sorted(result.items()))


def reconstruct_candidate(
    *,
    base_catalog_text: str,
    base_references: Mapping[str, str],
    base_overlays: Mapping[str, str],
    operations: Sequence[Operation],
) -> Candidate:
    """Reconstruct a candidate by applying typed operations to a base.

    Catalog operations round-trip the base catalog through ``ruamel.yaml`` so
    comments, key order outside changed objects, quoting, and newline style are
    preserved. Knowledge operations replace or add whole document texts. The
    reconstructed catalog text is expected to load to a valid
    :class:`~selayer.model.SemanticLayer`; reconstruction itself never invokes
    the loader (verification owns that, in Task 17).
    """

    yaml = _round_trip_yaml()
    try:
        data = yaml.load(base_catalog_text)
    except YAMLError:
        raise ProposalError(
            CODE_PROPOSAL_RECONSTRUCTION_FAILED,
            safe_detail="base catalog parse failed",
        ) from None
    if not isinstance(data, Mapping):
        raise ProposalError(
            CODE_PROPOSAL_RECONSTRUCTION_FAILED,
            safe_detail="base catalog root not a mapping",
        ) from None
    catalog_ops = [op for op in operations if op.kind in CATALOG_KINDS]
    # Preserve the authored newline style (CRLF vs LF) so a candidate never
    # silently normalizes line endings.
    line_break = "\r\n" if "\r\n" in base_catalog_text else "\n"
    for op in catalog_ops:
        _apply_catalog_operation(data, op, line_break=line_break)
    catalog_text = _dump_yaml(data, line_break=line_break)
    catalog_fingerprint = canonical.fingerprint(catalog_text)
    references = _apply_knowledge_operations(
        base_references, operations, subject=KnowledgeSubject.REFERENCE
    )
    overlays = _apply_knowledge_operations(
        base_overlays, operations, subject=KnowledgeSubject.OVERLAY
    )
    fingerprint = canonical.fingerprint(
        {
            "catalog": catalog_fingerprint,
            "references": {
                path: canonical.fingerprint(text) for path, text in references
            },
            "overlays": {path: canonical.fingerprint(text) for path, text in overlays},
        }
    )
    return Candidate(
        catalog_text=catalog_text,
        catalog_fingerprint=catalog_fingerprint,
        references=references,
        overlays=overlays,
        fingerprint=fingerprint,
    )


def write_candidate(candidate: Candidate, root: Path) -> Path:
    """Write a reconstructed candidate's catalog to ``root`` and return its path.

    The candidate is immutable; this helper materializes its catalog so callers
    can load it through :meth:`SemanticLayer.load`. Knowledge files are not
    written here because they are previews; apply (Task 20) owns writing them.
    """

    root.mkdir(parents=True, exist_ok=True)
    catalog_path = root / "candidate-catalog.yaml"
    catalog_path.write_text(candidate.catalog_text, encoding="utf-8")
    return catalog_path


# --------------------------------------------------------------------------- #
# Review preview                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReviewPreview:
    """A deterministic review preview: catalog patch and knowledge diffs.

    Previews are renderings only. They are never parsed during verify or apply;
    typed operations remain the sole apply authority.
    """

    catalog_patch: str
    reference_diffs: tuple[tuple[str, str], ...]
    overlay_diffs: tuple[tuple[str, str], ...]
    fingerprint: str


def _unified_diff(before: str, after: str, *, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
        )
    )


def _knowledge_diffs(
    base: Mapping[str, str],
    candidate_files: Sequence[tuple[str, str]],
    *,
    label_dir: str,
) -> tuple[tuple[str, str], ...]:
    base_copy = dict(base)
    diffs: list[tuple[str, str]] = []
    for path, text in candidate_files:
        before = base_copy.get(path, "")
        diff = _unified_diff(before, text, label=f"{label_dir}/{path}")
        diffs.append((path, diff))
    return tuple(diffs)


def render_review_preview(
    *,
    base_catalog_text: str,
    base_references: Mapping[str, str],
    base_overlays: Mapping[str, str],
    candidate: Candidate,
) -> ReviewPreview:
    """Render a deterministic catalog patch and knowledge diff for review."""

    catalog_patch = _unified_diff(
        base_catalog_text, candidate.catalog_text, label="catalog.yaml"
    )
    reference_diffs = _knowledge_diffs(
        base_references, candidate.references, label_dir="references"
    )
    overlay_diffs = _knowledge_diffs(
        base_overlays, candidate.overlays, label_dir="overlays"
    )
    fingerprint = canonical.fingerprint(
        {
            "catalog_patch": catalog_patch,
            "reference_diffs": [d for _, d in reference_diffs],
            "overlay_diffs": [d for _, d in overlay_diffs],
        }
    )
    return ReviewPreview(
        catalog_patch=catalog_patch,
        reference_diffs=reference_diffs,
        overlay_diffs=overlay_diffs,
        fingerprint=fingerprint,
    )


# --------------------------------------------------------------------------- #
# Safe review summary                                                        #
# --------------------------------------------------------------------------- #

#: A knowledge file that did not exist in the authored base (a create).
_STATUS_ADDED: str = "added"
#: A knowledge file that existed in the authored base (an update).
_STATUS_UPDATED: str = "updated"


@dataclass(frozen=True, slots=True)
class KnowledgeSummaryEntry:
    """A safe, body-free summary of one changed knowledge file.

    Carries only a safe relative path, an add/update status, the file
    fingerprint, and added/removed line counts. It never carries raw patch,
    diff, or document-body text.
    """

    path: str
    status: str
    fingerprint: str
    added_lines: int
    removed_lines: int


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    """A safe review summary: fingerprints, line counts, and path/status.

    Never carries raw patch, diff, or document-body text. ``proposal show``
    renders this to operators so a document body or patch never reaches a
    terminal; the full :func:`render_review_preview` API remains for
    deterministic internal use.
    """

    catalog_fingerprint: str
    preview_fingerprint: str
    catalog_added_lines: int
    catalog_removed_lines: int
    references: tuple[KnowledgeSummaryEntry, ...]
    overlays: tuple[KnowledgeSummaryEntry, ...]


def _diff_line_counts(diff: str) -> tuple[int, int]:
    """Return ``(added, removed)`` line counts from a unified diff."""

    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _knowledge_summary(
    base: Mapping[str, str],
    candidate_files: Sequence[tuple[str, str]],
    diffs: Sequence[tuple[str, str]],
) -> tuple[KnowledgeSummaryEntry, ...]:
    diff_by_path = {path: diff for path, diff in diffs}
    text_by_path = {path: text for path, text in candidate_files}
    entries: list[KnowledgeSummaryEntry] = []
    for path in sorted(text_by_path):
        added, removed = _diff_line_counts(diff_by_path.get(path, ""))
        status = _STATUS_UPDATED if path in base else _STATUS_ADDED
        entries.append(
            KnowledgeSummaryEntry(
                path=path,
                status=status,
                fingerprint=canonical.fingerprint(text_by_path[path]),
                added_lines=added,
                removed_lines=removed,
            )
        )
    return tuple(entries)


def render_review_summary(
    *,
    base_references: Mapping[str, str],
    base_overlays: Mapping[str, str],
    candidate: Candidate,
    preview: ReviewPreview,
) -> ReviewSummary:
    """Render a body-free review summary (fingerprints, counts, path/status).

    Unlike :func:`render_review_preview`, this never carries raw patch, diff,
    or document-body text — only fingerprints, line counts, and safe relative
    path/status metadata. It is the safe rendering surface for operator-facing
    output (``proposal show``); the full preview API remains for deterministic
    internal use.
    """

    catalog_added, catalog_removed = _diff_line_counts(preview.catalog_patch)
    references = _knowledge_summary(
        base_references, candidate.references, preview.reference_diffs
    )
    overlays = _knowledge_summary(
        base_overlays, candidate.overlays, preview.overlay_diffs
    )
    return ReviewSummary(
        catalog_fingerprint=candidate.catalog_fingerprint,
        preview_fingerprint=preview.fingerprint,
        catalog_added_lines=catalog_added,
        catalog_removed_lines=catalog_removed,
        references=references,
        overlays=overlays,
    )


# --------------------------------------------------------------------------- #
# Proposal file loading                                                       #
# --------------------------------------------------------------------------- #


def load_proposal_file(path: str | Path) -> Proposal:
    """Load and validate a proposal YAML or JSON file."""

    yaml = YAML(typ="safe")
    raw_path = Path(path)
    try:
        with raw_path.open("r", encoding="utf-8") as handle:
            data = yaml.load(handle)
    except (OSError, YAMLError):
        raise ProposalError(
            CODE_PROPOSAL_INVALID, safe_detail="proposal load failed"
        ) from None
    return build_proposal(data)


# --------------------------------------------------------------------------- #
# Proposal verification (Task 17)                                            #
# --------------------------------------------------------------------------- #


def _union_group_impacts(group: DependencyGroup) -> tuple[str, ...]:
    """Return the sorted union of every operation's derived impacts."""

    flags: set[str] = set()
    for op in group.operations:
        flags.update(op.impacts)
    return tuple(sorted(flags))


def _group_cites_data(group: DependencyGroup, claim_store: ClaimStore | None) -> bool:
    """Return whether a group cites observed data evidence.

    A group cites data when it carries a bounded execution-assertion query
    case (which runs the live planner/engine over data) or when one of its
    supporting claims is a direct source observation (evidence class
    ``observed``). Asserted or inferred-only claims do not count: they do not
    cite data, so they do not widen the required evidence.
    """

    for case in group.query_cases:
        if case.kind == "execution_assertion":
            return True
    if claim_store is not None:
        for claim_id in group.supporting_claim_ids:
            try:
                claim = claim_store.get_claim(claim_id)
            except Exception:  # noqa: BLE001, S112
                continue
            if claim.evidence_class == "observed":
                return True
    return False


def _group_check_kinds(
    group: DependencyGroup, claim_store: ClaimStore | None
) -> frozenset[str]:
    """Return the mandatory check kinds for one dependency group.

    The pure impacts-only :func:`mandatory_check_kinds` is the baseline and
    stays unchanged. When the group has a type/expression impact *and* its
    normalized data cites observed evidence (an observed supporting claim or a
    bounded execution-assertion query case), a reopenable physical data audit
    is added. The conditional is never derived from an agent-supplied impact
    list; it depends only on the derived impacts and the bound typed data.
    """

    impacts = _union_group_impacts(group)
    kinds = mandatory_check_kinds(impacts)
    if _TYPE_EXPRESSION_IMPACTS.intersection(impacts) and _group_cites_data(
        group, claim_store
    ):
        kinds = kinds | {MandatoryCheckKind.PHYSICAL.value}
    return kinds


def _extract_selectors(
    group: DependencyGroup,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract metric and dimension selector names from catalog operations."""

    metrics: list[str] = []
    dimensions: list[str] = []
    for op in group.operations:
        match = _CATALOG_TARGET_RE.match(op.target_id)
        if match is None:
            continue
        kind, name = match.group(1), match.group(2)
        if kind == "metric":
            metrics.append(name)
        elif kind == "dimension":
            dimensions.append(name)
    return tuple(metrics), tuple(dimensions)


def _report_status(report: object) -> str:
    """Map a core ``VerificationReport`` to a safe check status."""

    # ``VerificationReport`` exposes ``passed`` and ``complete`` properties.
    if getattr(report, "passed", False):
        return "passed"
    if not getattr(report, "complete", True):
        return "unavailable"
    return "failed"


def _first_error_code(report: object) -> str:
    """Return the first error-severity diagnostic code, or empty."""

    for diag in getattr(report, "diagnostics", ()):
        if getattr(diag, "severity", None) == "error":
            return getattr(diag, "code", "")
    return ""


def _outcome_signature(report: object) -> tuple[object, ...]:
    """Return a safe, deterministic signature of a report's outcomes."""

    return tuple(
        sorted(
            (getattr(o, "check_id", ""), getattr(o, "status", ""))
            for o in getattr(report, "outcomes", ())
        )
    )


def _run_static_check(layer: SemanticLayer, candidate: Candidate) -> MandatoryCheck:
    """Run the static catalog check via the core public verification API."""

    from selayer.verification import verify_static

    report = verify_static(layer)
    digest = canonical.fingerprint(
        {
            "catalog": candidate.catalog_fingerprint,
            "outcomes": _outcome_signature(report),
        }
    )
    return MandatoryCheck(
        kind=MandatoryCheckKind.STATIC.value,
        status=_report_status(report),
        code=_first_error_code(report),
        digest=digest,
    )


def _run_physical_check(layer: SemanticLayer, candidate: Candidate) -> MandatoryCheck:
    """Run the exact source/grain/relationship physical audit.

    A missing or unreadable source makes the core audit produce
    ``unavailable`` outcomes and an incomplete report; that is faithfully
    reflected as an ``unavailable`` status so readiness can never bypass a
    physical-audit failure.
    """

    from selayer.verification import PhysicalCheck, verify_physical

    try:
        report = verify_physical(layer, PhysicalCheck())
    except Exception:  # noqa: BLE001
        # Any unexpected failure during the physical audit (a connector or
        # driver error not sanitized by the core) is treated as unavailable so
        # readiness refuses rather than crashing.
        digest = canonical.fingerprint(
            {"catalog": candidate.catalog_fingerprint, "outcomes": ()}
        )
        return MandatoryCheck(
            kind=MandatoryCheckKind.PHYSICAL.value,
            status="unavailable",
            code="",
            digest=digest,
        )
    digest = canonical.fingerprint(
        {
            "catalog": candidate.catalog_fingerprint,
            "outcomes": _outcome_signature(report),
        }
    )
    return MandatoryCheck(
        kind=MandatoryCheckKind.PHYSICAL.value,
        status=_report_status(report),
        code=_first_error_code(report),
        digest=digest,
    )


def _run_compatibility_check(
    layer: SemanticLayer, group: DependencyGroup, candidate: Candidate
) -> MandatoryCheck:
    """Run planner-parity compatibility verification for the group's selectors."""

    from selayer.verification import CompatibilityCheck, verify_compatibility

    metrics, dimensions = _extract_selectors(group)
    check = CompatibilityCheck(
        metrics=metrics or None,
        dimensions=dimensions or None,
    )
    report = verify_compatibility(layer, check)
    digest = canonical.fingerprint(
        {
            "catalog": candidate.catalog_fingerprint,
            "selectors": {"metrics": sorted(metrics), "dimensions": sorted(dimensions)},
            "outcomes": _outcome_signature(report),
        }
    )
    return MandatoryCheck(
        kind=MandatoryCheckKind.COMPATIBILITY.value,
        status=_report_status(report),
        code=_first_error_code(report),
        digest=digest,
    )


def _case_filters(case: QueryCase) -> dict[str, Any]:
    """Convert a query case's typed filters to planner filter inputs."""

    filters: dict[str, Any] = {}
    for f in case.filters:
        if f.operator == "equals":
            filters[f.dimension_id] = f.value
        elif f.operator == "in":
            filters[f.dimension_id] = (
                list(f.value) if isinstance(f.value, tuple) else f.value
            )
        elif f.operator == "between" and isinstance(f.value, tuple):
            filters[f.dimension_id] = tuple(f.value)
    return filters


def _assertion_passes(assertion: CaseAssertion, row_count: int) -> bool:
    """Evaluate one bounded row-count assertion."""

    value = assertion.value
    if assertion.operator == "row_count_max":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and row_count <= value
        )
    if assertion.operator == "row_count_min":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and row_count >= value
        )
    if assertion.operator == "non_empty":
        return row_count > 0
    return False


def _run_acceptance_check(
    layer: SemanticLayer, group: DependencyGroup, candidate: Candidate
) -> MandatoryCheck:
    """Run the group's semantic query cases through the planner and engine.

    ``compatible_plan`` and ``planner_rejection`` cases exercise the core
    public planner (:func:`plan_query`) without execution; ``execution_assertion``
    cases execute through the core public :class:`QueryEngine` and apply only
    bounded row-count assertions. No SQL, callable, or unrestricted row
    capture can reach this point (rejected at :class:`QueryCase` construction).
    """

    from selayer.planning import QueryPlanningError, QueryRequest, plan_query

    cases = group.query_cases
    has_execution = any(c.kind == "execution_assertion" for c in cases)
    status = "passed"
    code = ""
    engine: object | None = None
    try:
        if has_execution:
            from selayer.query import QueryEngine

            engine = QueryEngine(layer)
        for case in cases:
            request = QueryRequest(
                metrics=tuple(case.metrics),
                dimensions=tuple(case.dimensions),
                filters=_case_filters(case),
            )
            if case.kind == "compatible_plan":
                try:
                    plan_query(layer, request)
                except QueryPlanningError:
                    status, code = "failed", "acceptance_plan_failed"
                    break
            elif case.kind == "planner_rejection":
                try:
                    plan_query(layer, request)
                except QueryPlanningError as error:
                    if error.code != case.expected_rejection_code:
                        status, code = "failed", "acceptance_rejection_mismatch"
                        break
                else:
                    status, code = "failed", "acceptance_rejection_expected"
                    break
            elif case.kind == "execution_assertion":
                assert engine is not None
                try:
                    result = engine.query(  # type: ignore[attr-defined]
                        list(case.metrics),
                        list(case.dimensions),
                        _case_filters(case),
                    )
                except QueryPlanningError:
                    status, code = "failed", "acceptance_plan_failed"
                    break
                except Exception:  # noqa: BLE001
                    status, code = "unavailable", "acceptance_unavailable"
                    break
                row_count = int(result.height)  # type: ignore[union-attr]
                if not all(_assertion_passes(a, row_count) for a in case.assertions):
                    status, code = "failed", "acceptance_assertion_failed"
                    break
    except Exception:  # noqa: BLE001
        status, code = "unavailable", "acceptance_unavailable"
    finally:
        if engine is not None:
            close = getattr(engine, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001, S110
                    pass
    digest = canonical.fingerprint(
        {
            "catalog": candidate.catalog_fingerprint,
            "cases": tuple((c.case_id, c.kind) for c in cases),
        }
    )
    return MandatoryCheck(
        kind=MandatoryCheckKind.ACCEPTANCE.value,
        status=status,
        code=code,
        digest=digest,
    )


def _write_knowledge_files(root: Path, files: Sequence[tuple[str, str]]) -> None:
    """Write ``(relative_path, text)`` knowledge files under ``root``."""

    root.mkdir(parents=True, exist_ok=True)
    for rel_path, text in files:
        target = root / PurePosixPath(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _run_okf_check(
    layer: SemanticLayer, candidate: Candidate, okf_output_dir: str | Path
) -> MandatoryCheck:
    """Build a fresh OKF bundle with strict integrity and curated overlays.

    The candidate's authored Reference and overlay documents are written to
    sibling staging directories, composed with the generated projection, and
    validated with strict integrity (:meth:`OkfBundle.build`). Any validation
    failure (an unresolvable overlay ``selayer_id``, a broken link, a policy
    violation) makes the check ``failed``; the staging directories are cleaned
    up in a ``finally``.
    """

    from selayer.okf import OkfBundle

    output = Path(okf_output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        if output.exists():
            shutil.rmtree(output, ignore_errors=True)
    except OSError:
        pass
    ref_dir = output.parent / (output.name + "-references")
    ovl_dir = output.parent / (output.name + "-overlays")
    references_dir: str | Path | None = None
    overlays_dir: str | Path | None = None
    try:
        if candidate.references:
            shutil.rmtree(ref_dir, ignore_errors=True)
            _write_knowledge_files(ref_dir, candidate.references)
            references_dir = ref_dir
        if candidate.overlays:
            shutil.rmtree(ovl_dir, ignore_errors=True)
            _write_knowledge_files(ovl_dir, candidate.overlays)
            overlays_dir = ovl_dir
        try:
            OkfBundle.build(
                layer,
                output,
                references_dir=references_dir,
                overlays_dir=overlays_dir,
            )
            status = "passed"
            okf_code = ""
        except Exception:  # noqa: BLE001
            status = "failed"
            okf_code = "okf_integrity_failed"
    finally:
        shutil.rmtree(ref_dir, ignore_errors=True)
        shutil.rmtree(ovl_dir, ignore_errors=True)
    digest = canonical.fingerprint(
        {
            "catalog": candidate.catalog_fingerprint,
            "references": {
                path: canonical.fingerprint(text) for path, text in candidate.references
            },
            "overlays": {
                path: canonical.fingerprint(text) for path, text in candidate.overlays
            },
        }
    )
    return MandatoryCheck(
        kind=MandatoryCheckKind.OKF.value,
        status=status,
        code=okf_code,
        digest=digest,
    )


def _run_group_checks(
    group: DependencyGroup,
    candidate: Candidate,
    layer: SemanticLayer,
    okf_output_dir: str | Path,
    claim_store: ClaimStore | None,
) -> tuple[MandatoryCheck, ...]:
    """Derive and run every mandatory check for one dependency group."""

    kinds = sorted(_group_check_kinds(group, claim_store))
    checks: list[MandatoryCheck] = []
    for kind in kinds:
        if kind == MandatoryCheckKind.STATIC.value:
            checks.append(_run_static_check(layer, candidate))
        elif kind == MandatoryCheckKind.PHYSICAL.value:
            checks.append(_run_physical_check(layer, candidate))
        elif kind == MandatoryCheckKind.COMPATIBILITY.value:
            checks.append(_run_compatibility_check(layer, group, candidate))
        elif kind == MandatoryCheckKind.ACCEPTANCE.value:
            checks.append(_run_acceptance_check(layer, group, candidate))
        elif kind == MandatoryCheckKind.OKF.value:
            group_output = Path(okf_output_dir) / group.group_id
            checks.append(_run_okf_check(layer, candidate, group_output))
        else:  # pragma: no cover - exhaustive over the matrix
            raise ProposalError(CODE_PROPOSAL_INVALID, safe_detail="unknown check kind")
    return tuple(checks)


def _topological_groups(
    groups: Sequence[DependencyGroup],
) -> tuple[DependencyGroup, ...]:
    """Return groups in dependency-first (topological) order."""

    by_id = {group.group_id: group for group in groups}
    visited: set[str] = set()
    ordered: list[DependencyGroup] = []

    def visit(gid: str) -> None:
        if gid in visited:
            return
        visited.add(gid)
        for dep in by_id[gid].dependencies:
            visit(dep)
        ordered.append(by_id[gid])

    for group in groups:
        visit(group.group_id)
    return tuple(ordered)


def _supporting_claim_records(
    group: DependencyGroup, claim_store: ClaimStore
) -> list[ClaimRecord]:
    """Return the current supporting-claim records, skipping unknown ids."""

    records: list[ClaimRecord] = []
    for claim_id in group.supporting_claim_ids:
        try:
            records.append(claim_store.get_claim(claim_id))
        except Exception:  # noqa: BLE001, S112
            continue
    return records


def _selectors_current(
    claims: Sequence[ClaimRecord], evidence_store: EvidenceStore
) -> bool:
    """Return whether every retained selector is still bound to its revision.

    Each selector is revalidated against the current evidence revision via the
    core :meth:`EvidenceStore.validate_selector` API. A stale, missing, or
    malformed selector returns ``False`` so readiness can block with a stable
    code. No body or value is read or surfaced.
    """

    for claim in claims:
        for selector in claim.selectors:
            try:
                evidence_store.validate_selector(selector)
            except Exception:  # noqa: BLE001
                return False
    return True


def _snapshots_reopenable(
    claims: Sequence[ClaimRecord], evidence_store: EvidenceStore
) -> bool:
    """Return whether every referenced snapshot content exists and reopens.

    Reopens each retained selector's content-addressed snapshot via
    :meth:`EvidenceStore.reopen_snapshot`, which verifies the on-disk bytes
    are a regular file within the configured size bound and that the re-hash
    matches the recorded content hash. A missing, unreadable, oversized, or
    tampered snapshot fails closed. This is a genuine reopenability check, not
    a path-existence probe, yet it never reads or surfaces a body or value and
    never duplicates the physical audit.
    """

    for claim in claims:
        for selector in claim.selectors:
            try:
                evidence_store.reopen_snapshot(selector.content_hash)
            except Exception:  # noqa: BLE001
                return False
    return True


def _claims_declared_selectors_present(
    claims: Sequence[ClaimRecord],
) -> bool:
    """Return whether every current non-inferred claim carries its selectors.

    A legacy or tampered journal payload may declare ``selector_kinds`` while
    persisting no typed ``selectors``. Such a record cannot be revalidated
    against the current evidence revision, so readiness must fail closed
    rather than vacuously passing the selector gates over an empty tuple.
    """

    for claim in claims:
        if (
            claim.state == "current"
            and claim.evidence_class != "inferred"
            and claim.selector_kinds
            and not claim.selectors
        ):
            return False
    return True


def _group_readiness(
    group: DependencyGroup,
    checks: tuple[MandatoryCheck, ...],
    *,
    proposal: Proposal,
    readiness_by_group: Mapping[str, GroupReadiness],
    interview_store: InterviewStore | None,
    claim_store: ClaimStore | None,
    evidence_store: EvidenceStore | None,
) -> GroupReadiness:
    """Compute the review-readiness verdict for one dependency group.

    Readiness requires: disposed affecting gates, current non-inferred
    claims with selectors still bound to their evidence revision, reopenable
    snapshot content for groups requiring physical evidence, no affected
    unresolved conflicts, ready dependencies, and every mandatory check
    passed. Gate, claim, evidence, and conflict gates are evaluated only when
    the corresponding store is provided; the mandatory-check and dependency
    gates always apply.
    """

    blockers: list[str] = []
    if interview_store is not None and not interview_store.group_gate_ready(
        group.affecting_gates
    ):
        blockers.append("gate_open")
    requires_physical = any(
        check.kind == MandatoryCheckKind.PHYSICAL.value for check in checks
    )
    if claim_store is not None:
        supporting = _supporting_claim_records(group, claim_store)
        has_current_non_inferred = any(
            claim.state == "current" and claim.evidence_class != "inferred"
            for claim in supporting
        )
        if not has_current_non_inferred:
            blockers.append("claim_inferred_only")
        # A current non-inferred claim that declares selector kinds but
        # carries no persisted selectors (a legacy/tampered journal payload)
        # cannot be revalidated against the evidence revision: fail closed
        # with the stable selector code rather than vacuously passing the
        # selector gates over an empty tuple.
        if not _claims_declared_selectors_present(supporting):
            blockers.append("evidence_selector_stale")
        # Revalidate every retained supporting selector against the current
        # evidence revision: a stale or missing selector blocks readiness.
        if evidence_store is not None and not _selectors_current(
            supporting, evidence_store
        ):
            blockers.append("evidence_selector_stale")
        # Groups requiring reopenable (physical) evidence must reference
        # snapshot content that still exists and reopens on disk.
        if (
            requires_physical
            and evidence_store is not None
            and not _snapshots_reopenable(supporting, evidence_store)
        ):
            blockers.append("evidence_not_reopenable")
        for conflict in claim_store.conflicts():
            if (
                group.group_id in conflict.affected_group_ids
                and conflict.state == "unresolved"
            ):
                blockers.append("conflict_unresolved")
                break
    for dep_id in group.dependencies:
        if not readiness_by_group[dep_id].ready:
            blockers.append("dependency_not_ready")
            break
    for check in checks:
        if check.status != "passed":
            blockers.append("check_failed")
            break
    ready = not blockers
    return GroupReadiness(ready=ready, blockers=tuple(blockers))


def verify_proposal(
    *,
    proposal: Proposal,
    candidate: Candidate,
    candidate_layer: SemanticLayer,
    okf_output_dir: str | Path,
    interview_store: InterviewStore | None = None,
    claim_store: ClaimStore | None = None,
    evidence_store: EvidenceStore | None = None,
) -> VerificationBundle:
    """Verify a proposal's review readiness and return an immutable bundle.

    For every dependency group the mandatory checks are derived solely from
    the normalized before/after impacts (never agent-supplied) and executed
    through the core public verification, planner, :class:`QueryEngine`, and
    :meth:`OkfBundle.build` APIs. Each outcome is converted to a safe
    ``status``/``code``/``digest`` record — never raw bodies, values, errors,
    or SQL.

    Readiness is computed in dependency-first order so a group's verdict can
    gate on its dependencies. The bundle's ``fingerprint`` is a pure function
    of the proposal, candidate, and check outcomes, so a second run with
    unchanged inputs produces an identical fingerprint.

    Args:
        proposal: the typed proposal.
        candidate: the reconstructed candidate.
        candidate_layer: the candidate layer loaded from the candidate catalog.
        okf_output_dir: a writable directory for OKF build artifacts.
        interview_store: optional interview store for gate readiness.
        claim_store: optional claim/conflict store for evidence readiness.
        evidence_store: optional evidence store for selector/snapshot readiness.

    Returns:
        An immutable :class:`VerificationBundle` bound to all input hashes.
    """

    input_hashes: dict[str, str] = {
        "proposal": proposal.fingerprint,
        "candidate": candidate.fingerprint,
        "catalog": candidate.catalog_fingerprint,
    }
    group_checks: dict[str, tuple[MandatoryCheck, ...]] = {}
    for group in proposal.groups:
        group_checks[group.group_id] = _run_group_checks(
            group, candidate, candidate_layer, okf_output_dir, claim_store
        )
    readiness_by_group: dict[str, GroupReadiness] = {}
    for group in _topological_groups(proposal.groups):
        readiness_by_group[group.group_id] = _group_readiness(
            group,
            group_checks[group.group_id],
            proposal=proposal,
            readiness_by_group=readiness_by_group,
            interview_store=interview_store,
            claim_store=claim_store,
            evidence_store=evidence_store,
        )
    groups = tuple(
        GroupVerification(
            group_id=group.group_id,
            checks=group_checks[group.group_id],
            readiness=readiness_by_group[group.group_id],
        )
        for group in proposal.groups
    )
    fingerprint = canonical.fingerprint(
        {
            "proposal_fingerprint": proposal.fingerprint,
            "candidate_fingerprint": candidate.fingerprint,
            "groups": [gv.to_dict() for gv in groups],
        }
    )
    return VerificationBundle(
        proposal_id=proposal.proposal_id,
        proposal_fingerprint=proposal.fingerprint,
        candidate_fingerprint=candidate.fingerprint,
        input_hashes=input_hashes,
        groups=groups,
        fingerprint=fingerprint,
    )
