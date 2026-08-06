"""Adaptive interview gates, append-only answers, corrections, and dispositions.

This module owns the adaptive interview foundation for the discovery companion
package. It mirrors the secrecy and append-only discipline of the evidence
store (:mod:`selayer_discovery.evidence`) and the session store
(:mod:`selayer_discovery.session`):

* :class:`InterviewStore` — an append-only interview journal
  (``interview/events.jsonl``) that carries full bounded interview payloads
  (questions, answers, corrections, gate dispositions). It holds a reference to
  the session :class:`~selayer_discovery.session.SessionStore` and binds every
  mutation to a session artifact node (``answer-<gate>``, ``gate-<gate>``,
  ``question-<seq>``) via :meth:`SessionStore.record_artifact` so transitive
  stale dependents propagate through the session dependency index. The session
  journal remains the authority for artifact bindings and stale state; the
  interview journal is the authority for interview content and history.
* Fifteen stable gate IDs gate discovery readiness; every atomic dependency
  group declares which gates affect it. A group is not ready while any
  affecting gate is undisposed or blocked.
* At most one question may be open at a time. A question cites exactly one
  gate, motivating evidence ids, and in-charter affected subjects.
* Answers dispose their gate as ``answered`` and close the open question.
* Typed corrections cite one current answer, preserve full history (the old
  answer line stays, marked superseded), supersede the old answer, and — by
  revising the answer artifact node — emit stale targets for every dependent
  claim, conflict, proposal, report, and attestation.
* Gate dispositions support ``answered``, ``not_applicable`` (requires a
  reason), and ``blocked`` (requires conflict ids and affected group ids).

Design rules (see the approved discovery design and global plan constraints):

* The interview journal is append-only; mutations append one canonical JSON
  line, flush, and ``fsync`` before releasing a ``filelock``.
* Diagnostics never echo question text, answer text, reasons, evidence bodies,
  credentials, paths, or raw exception causes — only stable codes, constant
  generic details, and validated safe identifiers.
* No CLI or LLM logic lives here; commands are pure structured-file imports.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError  # noqa: F401  # reserved for future replay guards
from pathlib import Path

from filelock import FileLock, Timeout

from selayer_discovery import canonical
from selayer_discovery.model import (
    MAX_TEXT_LENGTH,
    GateDisposition,
    normalize_actor_identity,
)
from selayer_discovery.session import SessionStore

__all__ = [
    "CODE_INTERVIEW_ANSWER_NOT_CURRENT",
    "CODE_INTERVIEW_DISPOSITION_INVALID",
    "CODE_INTERVIEW_EVIDENCE_BODY",
    "CODE_INTERVIEW_GATE_UNKNOWN",
    "CODE_INTERVIEW_INVALID_INPUT",
    "CODE_INTERVIEW_NOT_INITIALIZED",
    "CODE_INTERVIEW_QUESTION_OPEN",
    "CODE_INTERVIEW_STORE_CORRUPT",
    "CODE_INTERVIEW_SUBJECT_CHARTER",
    "CODE_INTERVIEW_TEXT_TOO_LARGE",
    "GATES",
    "GATE_GROUPS",
    "GATE_SET",
    "AnswerRecord",
    "AnswerResult",
    "CorrectionRecord",
    "CorrectionResult",
    "GateDispositionRecord",
    "InterviewError",
    "InterviewStore",
    "QuestionRecord",
]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Fifteen stable interview gate IDs. Every group declares a non-empty subset.
GATES: tuple[str, ...] = (
    "gate-business-objective",
    "gate-terms-synonyms",
    "gate-process-lifecycle",
    "gate-grains",
    "gate-identities",
    "gate-relationships",
    "gate-time",
    "gate-kpi",
    "gate-aggregation",
    "gate-business-rules",
    "gate-authority",
    "gate-privacy",
    "gate-acceptance",
    "gate-migration",
    "gate-conflicts",
)

#: Frozen set for membership checks.
GATE_SET: frozenset[str] = frozenset(GATES)

#: Atomic dependency groups and the gates that affect each one. Every group
#: declares at least one affecting gate; the union covers all fifteen gates.
GATE_GROUPS: Mapping[str, frozenset[str]] = {
    "group-business-objective": frozenset({"gate-business-objective"}),
    "group-vocabulary": frozenset({"gate-terms-synonyms", "gate-process-lifecycle"}),
    "group-semantic-model": frozenset(
        {
            "gate-grains",
            "gate-identities",
            "gate-relationships",
            "gate-time",
        }
    ),
    "group-measures": frozenset(
        {"gate-kpi", "gate-aggregation", "gate-business-rules"}
    ),
    "group-governance": frozenset(
        {"gate-authority", "gate-privacy", "gate-acceptance"}
    ),
    "group-delivery": frozenset({"gate-migration", "gate-conflicts"}),
}

# Interview store layout (within the Git-ignored session workspace).
_INTERVIEW_DIR = "interview"
_JOURNAL_NAME = "events.jsonl"
_LOCK_NAME = "interview.lock"

#: Default ``filelock`` acquisition timeout in seconds.
_DEFAULT_LOCK_TIMEOUT: float = 30.0

#: Stable identifier shape for interview-safe ids, evidence ids, group ids,
#: and conflict ids. Mirrors the session node-id grammar.
_SAFE_ID_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")

#: A run of 64 hexadecimal characters — the signature of a pasted content hash
#: or raw evidence body. Rejecting it in question/answer text prevents evidence
#: interpolation.
_HASH_RUN_RE: re.Pattern[str] = re.compile(r"[0-9a-fA-F]{64}")

# Stable interview diagnostic codes (rendered; never leak raw causes).
CODE_INTERVIEW_GATE_UNKNOWN: str = "discovery.interview.gate_unknown"
CODE_INTERVIEW_QUESTION_OPEN: str = "discovery.interview.question_open"
CODE_INTERVIEW_INVALID_INPUT: str = "discovery.interview.invalid_input"
CODE_INTERVIEW_TEXT_TOO_LARGE: str = "discovery.interview.text_too_large"
CODE_INTERVIEW_EVIDENCE_BODY: str = "discovery.interview.evidence_body"
CODE_INTERVIEW_SUBJECT_CHARTER: str = "discovery.interview.subject_charter"
CODE_INTERVIEW_ANSWER_NOT_CURRENT: str = "discovery.interview.answer_not_current"
CODE_INTERVIEW_DISPOSITION_INVALID: str = "discovery.interview.disposition_invalid"
CODE_INTERVIEW_NOT_INITIALIZED: str = "discovery.interview.not_initialized"
CODE_INTERVIEW_STORE_CORRUPT: str = "discovery.interview.store_corrupt"

# Journal event type labels (constants, not enum).
_EVT_QUESTION_ASKED: str = "question_asked"
_EVT_ANSWER_RECORDED: str = "answer_recorded"
_EVT_CORRECTION_RECORDED: str = "correction_recorded"
_EVT_GATE_DISPOSITION: str = "gate_disposition"

# Question / answer status labels (constants).
_STATUS_OPEN: str = "open"
_STATUS_CLOSED: str = "closed"
_STATUS_CURRENT: str = "current"
_STATUS_SUPERSEDED: str = "superseded"


# --------------------------------------------------------------------------- #
# Sanitized interview diagnostics                                             #
# --------------------------------------------------------------------------- #


def _safe_id(value: object) -> str:
    """Return ``value`` only if it is a stable identifier-shaped string."""

    if type(value) is str and _SAFE_ID_RE.match(value) is not None:
        return value
    return "<id>"


class InterviewError(Exception):
    """Sanitized interview diagnostic exception.

    Mirrors the secrecy discipline of
    :class:`selayer_discovery.evidence.EvidenceError` and
    :class:`selayer_discovery.session.SessionError`: only a stable ``code``, an
    optional constant ``safe_detail``, and validated ``safe_ids`` are ever
    rendered by ``__str__``, ``__repr__``, or :meth:`to_dict`. Raw causes,
    question text, answer text, and evidence bodies are never chained or
    surfaced (``from None`` at every raise site).
    """

    def __init__(
        self,
        code: str,
        *,
        safe_detail: str | None = None,
        safe_ids: Sequence[str] = (),
    ) -> None:
        self.code: str = code
        self.safe_detail: str | None = safe_detail
        validated: list[str] = []
        for item in safe_ids:
            validated.append(_safe_id(item))
            if len(validated) >= 16:
                break
        self.safe_ids: tuple[str, ...] = tuple(validated)
        super().__init__(self._render())

    def _render(self) -> str:
        parts: list[str] = [self.code]
        if self.safe_detail is not None:
            parts.append(self.safe_detail)
        if self.safe_ids:
            parts.append("ids=" + ",".join(self.safe_ids))
        return " ".join(parts)

    def __str__(self) -> str:
        return self._render()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"safe_detail={self.safe_detail!r}, safe_ids={self.safe_ids!r})"
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping containing only safe fields."""

        result: dict[str, object] = {"code": self.code}
        if self.safe_detail is not None:
            result["safe_detail"] = self.safe_detail
        if self.safe_ids:
            result["safe_ids"] = list(self.safe_ids)
        return result


# --------------------------------------------------------------------------- #
# Immutable interview records                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QuestionRecord:
    """Immutable view of an interview question.

    Carries only safe, derived metadata and identifiers. The question text is
    held for journal reconstruction and canonical hashing but is never echoed
    by :meth:`safe_dict` or CLI output.
    """

    question_id: str
    gate: str
    text: str
    evidence_ids: tuple[str, ...]
    subjects: tuple[str, ...]
    author: str
    timestamp: str
    status: str
    answered_by: str | None

    def safe_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping without free-text content."""

        payload: dict[str, object] = {
            "question_id": self.question_id,
            "gate": self.gate,
            "evidence_ids": list(self.evidence_ids),
            "subjects": list(self.subjects),
            "status": self.status,
        }
        if self.answered_by is not None:
            payload["answered_by"] = self.answered_by
        return payload


@dataclass(frozen=True, slots=True)
class AnswerRecord:
    """Immutable view of an interview answer at a point in history."""

    answer_id: str
    gate: str
    text: str
    author: str
    timestamp: str
    question_id: str | None
    status: str
    superseded_by: str | None

    def safe_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping without free-text content."""

        payload: dict[str, object] = {
            "answer_id": self.answer_id,
            "gate": self.gate,
            "status": self.status,
        }
        if self.question_id is not None:
            payload["question_id"] = self.question_id
        if self.superseded_by is not None:
            payload["superseded_by"] = self.superseded_by
        return payload


@dataclass(frozen=True, slots=True)
class CorrectionRecord:
    """Immutable view of a typed correction."""

    correction_id: str
    answer_id: str
    gate: str
    reason: str
    replacement_answer_id: str
    author: str
    timestamp: str

    def safe_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping without free-text content."""

        return {
            "correction_id": self.correction_id,
            "answer_id": self.answer_id,
            "gate": self.gate,
            "replacement_answer_id": self.replacement_answer_id,
        }


@dataclass(frozen=True, slots=True)
class GateDispositionRecord:
    """Immutable view of a terminal gate disposition."""

    gate: str
    disposition: str
    reason: str | None
    conflict_ids: tuple[str, ...]
    affected_group_ids: tuple[str, ...]
    author: str
    timestamp: str

    def safe_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping without free-text content."""

        payload: dict[str, object] = {
            "gate": self.gate,
            "disposition": self.disposition,
            "conflict_ids": list(self.conflict_ids),
            "affected_group_ids": list(self.affected_group_ids),
        }
        return payload


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """Result of recording an answer: the answer plus stale dependents."""

    answer: AnswerRecord
    stale_targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    """Result of recording a correction: records plus stale dependents."""

    correction: CorrectionRecord
    new_answer: AnswerRecord
    stale_targets: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Reconstructed state                                                         #
# --------------------------------------------------------------------------- #


@dataclass
class _Index:
    """In-memory projection rebuilt from the journal on every load/mutation."""

    questions: dict[str, QuestionRecord] = field(default_factory=dict)
    answers: dict[str, AnswerRecord] = field(default_factory=dict)
    corrections: list[CorrectionRecord] = field(default_factory=list)
    dispositions: dict[str, GateDispositionRecord] = field(default_factory=dict)
    current_answer_by_gate: dict[str, str] = field(default_factory=dict)
    open_question_id: str | None = None


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    """Return the current UTC time as a microsecond ISO-8601 string."""

    return datetime.now(UTC).isoformat(timespec="microseconds")


def _validate_id(value: object, *, code: str = CODE_INTERVIEW_INVALID_INPUT) -> str:
    """Validate and return a stable identifier-shaped string."""

    if type(value) is not str or _SAFE_ID_RE.match(value) is None:
        raise InterviewError(code) from None
    return value


def _validate_gate(gate: object) -> str:
    """Validate that ``gate`` is one of the fifteen known gate IDs."""

    if type(gate) is str and gate in GATE_SET:
        return gate
    safe = gate if type(gate) is str else "<id>"
    raise InterviewError(CODE_INTERVIEW_GATE_UNKNOWN, safe_ids=(safe,)) from None


def _validate_text(value: object) -> str:
    """Validate that ``value`` is a bounded text string."""

    if type(value) is not str:
        raise InterviewError(CODE_INTERVIEW_INVALID_INPUT) from None
    if len(value) > MAX_TEXT_LENGTH:
        raise InterviewError(CODE_INTERVIEW_TEXT_TOO_LARGE) from None
    return value


def _reject_evidence_body(text: str) -> None:
    """Reject text containing a content-hash run (evidence-body interpolation)."""

    if _HASH_RUN_RE.search(text) is not None:
        raise InterviewError(CODE_INTERVIEW_EVIDENCE_BODY) from None


def _validate_id_list(
    values: object,
    *,
    min_items: int = 0,
    code: str = CODE_INTERVIEW_INVALID_INPUT,
) -> tuple[str, ...]:
    """Validate a sequence of stable identifiers (deduplicated, sorted)."""

    if isinstance(values, str) or not isinstance(values, Sequence):
        raise InterviewError(code) from None
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        identifier = _validate_id(item, code=code)
        if identifier not in seen:
            seen.add(identifier)
            result.append(identifier)
    if len(result) < min_items:
        raise InterviewError(code) from None
    return tuple(result)


def _in_scope(subject: str, scopes: Iterable[str]) -> bool:
    """Return True when ``subject`` equals or is a sub-path of a scope."""

    for scope in scopes:
        if subject == scope or subject.startswith(scope + "."):
            return True
    return False


def _validate_subjects(
    subjects: object,
    *,
    inclusions: tuple[str, ...],
    exclusions: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate that every subject is in-charter (within inclusions)."""

    items = _validate_id_list(subjects, min_items=1)
    for subject in items:
        if not _in_scope(subject, inclusions) or _in_scope(subject, exclusions):
            raise InterviewError(
                CODE_INTERVIEW_SUBJECT_CHARTER, safe_ids=(subject,)
            ) from None
    return items


def _answer_artifact_id(gate: str) -> str:
    """Return the session artifact id for a gate's current answer."""

    return f"answer-{gate}"


def _answer_content_hash(gate: str, text: str, author: str) -> str:
    """Return the canonical fingerprint of an answer's content."""

    return canonical.fingerprint({"gate": gate, "text": text, "author": author})


def _question_artifact_id(seq: int) -> str:
    return f"question-{seq}"


# --------------------------------------------------------------------------- #
# InterviewStore                                                              #
# --------------------------------------------------------------------------- #


class InterviewStore:
    """Append-only interview journal with session-artifact stale propagation.

    The journal (``interview/events.jsonl``) is the single authority for
    interview content and history. Every mutation also binds a session artifact
    node via :meth:`SessionStore.record_artifact` so that corrections (which
    revise the answer node's content hash) emit transitive stale dependents
    through the session dependency index.

    Construct with :meth:`create` (idempotent: creates the layout and loads any
    existing journal) or :meth:`open` (existing journal only).
    """

    def __init__(
        self,
        session_store: SessionStore,
        *,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._session = session_store
        self._root = session_store.root / _INTERVIEW_DIR
        self._lock_timeout = lock_timeout
        self._journal = self._root / _JOURNAL_NAME
        self._lock_path = self._root / _LOCK_NAME
        self._lock = FileLock(str(self._lock_path))
        self._index = _Index()

    # -- construction ------------------------------------------------------- #

    @classmethod
    def create(
        cls,
        session_store: SessionStore,
        *,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> InterviewStore:
        """Create the interview layout (idempotent) and load any journal."""

        store = cls(session_store, lock_timeout=lock_timeout)
        store._ensure_layout()
        with store._locked():
            store._index = store._reconstruct()
        return store

    @classmethod
    def open(
        cls,
        session_store: SessionStore,
        *,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> InterviewStore:
        """Open an existing interview store and rebuild state from its journal."""

        store = cls(session_store, lock_timeout=lock_timeout)
        if not store._journal.exists():
            raise InterviewError(CODE_INTERVIEW_NOT_INITIALIZED) from None
        with store._locked():
            store._index = store._reconstruct()
        return store

    def _ensure_layout(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(self._root, 0o700)
            except OSError:
                pass

    # -- properties --------------------------------------------------------- #

    @property
    def root(self) -> Path:
        return self._root

    def questions(self) -> tuple[QuestionRecord, ...]:
        """Return all questions sorted by id."""

        return tuple(
            sorted(self._index.questions.values(), key=lambda q: q.question_id)
        )

    def answers(self) -> tuple[AnswerRecord, ...]:
        """Return all answers (history preserved) sorted by id."""

        return tuple(sorted(self._index.answers.values(), key=lambda a: a.answer_id))

    def corrections(self) -> tuple[CorrectionRecord, ...]:
        """Return all corrections sorted by id."""

        return tuple(self._index.corrections)

    def dispositions(self) -> dict[str, GateDispositionRecord]:
        """Return the latest disposition per gate (shallow copy)."""

        return dict(self._index.dispositions)

    def open_question(self) -> QuestionRecord | None:
        """Return the single open question, or ``None``."""

        qid = self._index.open_question_id
        if qid is None:
            return None
        return self._index.questions.get(qid)

    def current_answer(self, gate: str) -> AnswerRecord:
        """Return the current (non-superseded) answer for ``gate``."""

        answer_id = self._index.current_answer_by_gate.get(gate)
        if answer_id is None:
            raise InterviewError(
                CODE_INTERVIEW_ANSWER_NOT_CURRENT, safe_ids=(gate,)
            ) from None
        return self._index.answers[answer_id]

    # -- gate readiness ----------------------------------------------------- #

    def group_gate_ready(self, affecting_gates: Iterable[str]) -> bool:
        """Return True when every affecting gate permits readiness.

        A gate permits readiness when it has an ``answered`` or
        ``not_applicable`` disposition. An undisposed or ``blocked`` gate
        prevents readiness.
        """

        for gate in affecting_gates:
            disposition = self._index.dispositions.get(gate)
            if disposition is None:
                return False
            if disposition.disposition == GateDisposition.BLOCKED.value:
                return False
        return True

    # -- mutations: ask ----------------------------------------------------- #

    def ask(
        self,
        *,
        gate: str,
        text: str,
        evidence_ids: Sequence[str],
        subjects: Sequence[str],
        actor: str,
    ) -> QuestionRecord:
        """Open a question citing one gate, evidence ids, and in-charter subjects.

        Rejects a second open question, oversized text, evidence-body
        interpolation (a pasted content hash), unknown gates, invalid evidence
        ids, and out-of-charter subjects.
        """

        gate_id = _validate_gate(gate)
        question_text = _validate_text(text)
        _reject_evidence_body(question_text)
        evidence = _validate_id_list(evidence_ids, min_items=1)
        charter = self._session.charter
        affected = _validate_subjects(
            subjects,
            inclusions=charter.inclusions,
            exclusions=charter.exclusions,
        )
        author = normalize_actor_identity(actor)
        with self._locked():
            if self._index.open_question_id is not None:
                raise InterviewError(
                    CODE_INTERVIEW_QUESTION_OPEN,
                    safe_ids=(self._index.open_question_id,),
                ) from None
            seq = len(self._index.questions) + 1
            question_id = _question_artifact_id(seq)
            timestamp = _utc_now_iso()
            record = QuestionRecord(
                question_id=question_id,
                gate=gate_id,
                text=question_text,
                evidence_ids=evidence,
                subjects=affected,
                author=author,
                timestamp=timestamp,
                status=_STATUS_OPEN,
                answered_by=None,
            )
            payload = self._question_payload(record)
            self._append(_EVT_QUESTION_ASKED, payload)
            self._index.questions[question_id] = record
            self._index.open_question_id = question_id
            # Bind a session artifact node so the question participates in the
            # dependency graph. The content hash is the canonical fingerprint
            # of the safe payload (no free text beyond the already-bounded
            # question text, which is part of the authoritative transcript).
            self._session.record_artifact(
                question_id,
                content_hash=canonical.fingerprint(payload),
                depends_on=(),
                actor=author,
            )
            return record

    # -- mutations: answer -------------------------------------------------- #

    def answer(
        self,
        *,
        gate: str,
        text: str,
        actor: str,
    ) -> AnswerResult:
        """Record an answer to ``gate``, close any open question, dispose as answered.

        Registers (or revises) the ``answer-<gate>`` session artifact node so
        corrections can emit stale dependents later.
        """

        gate_id = _validate_gate(gate)
        answer_text = _validate_text(text)
        author = normalize_actor_identity(actor)
        with self._locked():
            seq = len(self._index.answers) + 1
            answer_id = f"answer-{seq}"
            timestamp = _utc_now_iso()
            # Close the open question (at most one by invariant).
            open_q = self._index.open_question_id
            question_id: str | None = None
            if open_q is not None:
                question = self._index.questions.get(open_q)
                if question is None or question.gate != gate_id:
                    raise InterviewError(
                        CODE_INTERVIEW_INVALID_INPUT,
                        safe_detail="answer_gate_mismatch",
                    ) from None
                question_id = open_q
                self._close_question(open_q, answer_id)
            record = AnswerRecord(
                answer_id=answer_id,
                gate=gate_id,
                text=answer_text,
                author=author,
                timestamp=timestamp,
                question_id=question_id,
                status=_STATUS_CURRENT,
                superseded_by=None,
            )
            self._append(_EVT_ANSWER_RECORDED, self._answer_payload(record))
            self._index.answers[answer_id] = record
            self._index.current_answer_by_gate[gate_id] = answer_id
            # Dispose the gate as answered.
            self._set_disposition_internal(
                gate=gate_id,
                disposition=GateDisposition.ANSWERED,
                reason=None,
                conflict_ids=(),
                affected_group_ids=(),
                author=author,
                timestamp=timestamp,
            )
            artifact = self._session.record_artifact(
                _answer_artifact_id(gate_id),
                content_hash=_answer_content_hash(gate_id, answer_text, author),
                depends_on=(question_id,) if question_id is not None else (),
                actor=author,
            )
            return AnswerResult(record, artifact.stale_targets)

    # -- mutations: correct ------------------------------------------------- #

    def correct(
        self,
        *,
        answer_id: str,
        reason: str,
        replacement: str,
        actor: str,
    ) -> CorrectionResult:
        """Supersede a current answer with a typed correction.

        The old answer stays in history (marked superseded); a new current
        answer is appended. Revising the ``answer-<gate>`` session artifact node
        emits transitive stale dependents (claims, conflicts, proposals,
        reports, attestations registered as dependents).
        """

        cited = _validate_id(answer_id)
        reason_text = _validate_text(reason)
        replacement_text = _validate_text(replacement)
        author = normalize_actor_identity(actor)
        with self._locked():
            old = self._index.answers.get(cited)
            if old is None or old.status != _STATUS_CURRENT:
                raise InterviewError(
                    CODE_INTERVIEW_ANSWER_NOT_CURRENT, safe_ids=(cited,)
                ) from None
            gate_id = old.gate
            seq = len(self._index.answers) + 1
            new_answer_id = f"answer-{seq}"
            correction_seq = len(self._index.corrections) + 1
            correction_id = f"correction-{correction_seq}"
            timestamp = _utc_now_iso()
            # Mark old answer superseded.
            superseded = AnswerRecord(
                answer_id=old.answer_id,
                gate=old.gate,
                text=old.text,
                author=old.author,
                timestamp=old.timestamp,
                question_id=old.question_id,
                status=_STATUS_SUPERSEDED,
                superseded_by=new_answer_id,
            )
            self._index.answers[old.answer_id] = superseded
            # Create the new current answer.
            new_answer = AnswerRecord(
                answer_id=new_answer_id,
                gate=gate_id,
                text=replacement_text,
                author=author,
                timestamp=timestamp,
                question_id=None,
                status=_STATUS_CURRENT,
                superseded_by=None,
            )
            correction = CorrectionRecord(
                correction_id=correction_id,
                answer_id=cited,
                gate=gate_id,
                reason=reason_text,
                replacement_answer_id=new_answer_id,
                author=author,
                timestamp=timestamp,
            )
            self._append(
                _EVT_CORRECTION_RECORDED,
                self._correction_payload(correction, new_answer, superseded),
            )
            self._index.answers[new_answer_id] = new_answer
            self._index.current_answer_by_gate[gate_id] = new_answer_id
            self._index.corrections.append(correction)
            # Revise the answer artifact node: the replacement text yields a new
            # content hash, so record_artifact emits transitive stale targets.
            artifact = self._session.record_artifact(
                _answer_artifact_id(gate_id),
                content_hash=_answer_content_hash(gate_id, replacement_text, author),
                depends_on=(),
                actor=author,
            )
            return CorrectionResult(correction, new_answer, artifact.stale_targets)

    # -- mutations: set_gate ----------------------------------------------- #

    def set_gate(
        self,
        *,
        gate: str,
        disposition: str | GateDisposition,
        reason: str | None = None,
        conflict_ids: Sequence[str] = (),
        affected_group_ids: Sequence[str] = (),
        actor: str,
    ) -> GateDispositionRecord:
        """Record a terminal gate disposition.

        ``not_applicable`` requires a reason; ``blocked`` requires at least one
        conflict id and one affected group id.
        """

        gate_id = _validate_gate(gate)
        try:
            disp = GateDisposition(disposition)
        except ValueError:
            raise InterviewError(CODE_INTERVIEW_DISPOSITION_INVALID) from None
        author = normalize_actor_identity(actor)
        with self._locked():
            return self._set_disposition_internal(
                gate=gate_id,
                disposition=disp,
                reason=reason,
                conflict_ids=conflict_ids,
                affected_group_ids=affected_group_ids,
                author=author,
                timestamp=_utc_now_iso(),
            )

    def _set_disposition_internal(
        self,
        *,
        gate: str,
        disposition: GateDisposition,
        reason: str | None,
        conflict_ids: Sequence[str],
        affected_group_ids: Sequence[str],
        author: str,
        timestamp: str,
    ) -> GateDispositionRecord:
        """Validate and append a gate disposition event (no lock)."""

        validated_conflicts: tuple[str, ...] = ()
        validated_groups: tuple[str, ...] = ()
        reason_text: str | None = None
        if disposition is GateDisposition.NOT_APPLICABLE:
            reason_text = _validate_text(reason) if reason is not None else None
            if reason_text is None or not reason_text.strip():
                raise InterviewError(CODE_INTERVIEW_DISPOSITION_INVALID) from None
        elif disposition is GateDisposition.BLOCKED:
            validated_conflicts = _validate_id_list(
                conflict_ids, min_items=1, code=CODE_INTERVIEW_DISPOSITION_INVALID
            )
            validated_groups = _validate_id_list(
                affected_group_ids,
                min_items=1,
                code=CODE_INTERVIEW_DISPOSITION_INVALID,
            )
            reason_text = _validate_text(reason) if reason is not None else None
        else:
            validated_conflicts = _validate_id_list(
                conflict_ids, code=CODE_INTERVIEW_DISPOSITION_INVALID
            )
            validated_groups = _validate_id_list(
                affected_group_ids, code=CODE_INTERVIEW_DISPOSITION_INVALID
            )
            reason_text = _validate_text(reason) if reason is not None else None

        record = GateDispositionRecord(
            gate=gate,
            disposition=disposition.value,
            reason=reason_text,
            conflict_ids=validated_conflicts,
            affected_group_ids=validated_groups,
            author=author,
            timestamp=timestamp,
        )
        self._append(_EVT_GATE_DISPOSITION, self._disposition_payload(record))
        self._index.dispositions[gate] = record
        # Bind the gate disposition as a session artifact node.
        self._session.record_artifact(
            gate,
            content_hash=canonical.fingerprint(self._disposition_payload(record)),
            depends_on=(_answer_artifact_id(gate),),
            actor=author,
        )
        return record

    # -- internal: question closing ----------------------------------------- #

    def _close_question(self, question_id: str, answer_id: str) -> None:
        """Mark the open question as closed by ``answer_id`` (no lock)."""

        current = self._index.questions.get(question_id)
        if current is None:
            return
        closed = QuestionRecord(
            question_id=current.question_id,
            gate=current.gate,
            text=current.text,
            evidence_ids=current.evidence_ids,
            subjects=current.subjects,
            author=current.author,
            timestamp=current.timestamp,
            status=_STATUS_CLOSED,
            answered_by=answer_id,
        )
        self._index.questions[question_id] = closed
        self._index.open_question_id = None

    # -- payload builders --------------------------------------------------- #

    @staticmethod
    def _question_payload(record: QuestionRecord) -> dict[str, object]:
        return {
            "question_id": record.question_id,
            "gate": record.gate,
            "text": record.text,
            "evidence_ids": list(record.evidence_ids),
            "subjects": list(record.subjects),
            "author": record.author,
            "timestamp": record.timestamp,
            "status": record.status,
        }

    @staticmethod
    def _answer_payload(record: AnswerRecord) -> dict[str, object]:
        payload: dict[str, object] = {
            "answer_id": record.answer_id,
            "gate": record.gate,
            "text": record.text,
            "author": record.author,
            "timestamp": record.timestamp,
            "status": record.status,
        }
        if record.question_id is not None:
            payload["question_id"] = record.question_id
        if record.superseded_by is not None:
            payload["superseded_by"] = record.superseded_by
        return payload

    @staticmethod
    def _correction_payload(
        correction: CorrectionRecord,
        new_answer: AnswerRecord,
        superseded: AnswerRecord,
    ) -> dict[str, object]:
        return {
            "correction_id": correction.correction_id,
            "answer_id": correction.answer_id,
            "gate": correction.gate,
            "reason": correction.reason,
            "replacement_answer_id": correction.replacement_answer_id,
            "replacement_text": new_answer.text,
            "author": correction.author,
            "timestamp": correction.timestamp,
            "superseded_status": superseded.status,
            "superseded_by": superseded.superseded_by,
        }

    @staticmethod
    def _disposition_payload(record: GateDispositionRecord) -> dict[str, object]:
        payload: dict[str, object] = {
            "gate": record.gate,
            "disposition": record.disposition,
            "conflict_ids": list(record.conflict_ids),
            "affected_group_ids": list(record.affected_group_ids),
            "author": record.author,
            "timestamp": record.timestamp,
        }
        if record.reason is not None:
            payload["reason"] = record.reason
        return payload

    # -- append + durability ------------------------------------------------ #

    def _append(self, event_type: str, payload: Mapping[str, object]) -> None:
        """Append one canonical JSON line, flush, and fsync the journal."""

        event = {
            "event_id": uuid.uuid4().hex,
            "type": event_type,
            "actor": payload.get("author", ""),
            "timestamp": payload.get("timestamp", _utc_now_iso()),
            "payload": dict(payload),
        }
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        try:
            descriptor = os.open(self._journal, flags, 0o600)
        except OSError:
            raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
        try:
            with os.fdopen(descriptor, "a", closefd=False) as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if os.name == "posix":
            try:
                os.chmod(self._journal, 0o600)
            except OSError:
                pass

    # -- reconstruction ----------------------------------------------------- #

    def _reconstruct(self) -> _Index:
        index = _Index()
        if not self._journal.exists():
            return index
        with self._journal.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.rstrip("\n")
                if line == "":
                    continue
                self._apply_event(index, self._parse_event(line))
        return index

    def _parse_event(self, line: str) -> dict[str, object]:
        try:
            obj = json.loads(line)
        except ValueError:
            raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
        if not isinstance(obj, dict):
            raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
        event_type = obj.get("type")
        payload = obj.get("payload")
        if type(event_type) is not str or not isinstance(payload, dict):
            raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
        return obj

    def _apply_event(self, index: _Index, event: dict[str, object]) -> None:
        event_type = event["type"]
        payload = event["payload"]
        if not isinstance(payload, dict):
            raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
        if event_type == _EVT_QUESTION_ASKED:
            self._apply_question(index, payload)
        elif event_type == _EVT_ANSWER_RECORDED:
            self._apply_answer(index, payload)
        elif event_type == _EVT_CORRECTION_RECORDED:
            self._apply_correction(index, payload)
        elif event_type == _EVT_GATE_DISPOSITION:
            self._apply_disposition(index, payload)
        else:
            raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None

    def _apply_question(self, index: _Index, p: Mapping[str, object]) -> None:
        question_id = _req_str(p, "question_id")
        record = QuestionRecord(
            question_id=question_id,
            gate=_req_str(p, "gate"),
            text=_req_str(p, "text"),
            evidence_ids=_tuple_str(p.get("evidence_ids", ())),
            subjects=_tuple_str(p.get("subjects", ())),
            author=_req_str(p, "author"),
            timestamp=_validate_timestamp(p.get("timestamp")),
            status=_req_str(p, "status"),
            answered_by=_opt_str(p.get("answered_by")),
        )
        index.questions[question_id] = record
        if record.status == _STATUS_OPEN:
            index.open_question_id = question_id

    def _apply_answer(self, index: _Index, p: Mapping[str, object]) -> None:
        answer_id = _req_str(p, "answer_id")
        record = AnswerRecord(
            answer_id=answer_id,
            gate=_req_str(p, "gate"),
            text=_req_str(p, "text"),
            author=_req_str(p, "author"),
            timestamp=_validate_timestamp(p.get("timestamp")),
            question_id=_opt_str(p.get("question_id")),
            status=_req_str(p, "status"),
            superseded_by=_opt_str(p.get("superseded_by")),
        )
        index.answers[answer_id] = record
        if record.status == _STATUS_CURRENT:
            index.current_answer_by_gate[record.gate] = answer_id
            # Close the question this answer resolves (mirrors the live
            # mutation: the answer event carries the question_id it closes).
            qid = record.question_id
            if qid is not None:
                open_q = index.questions.get(qid)
                if open_q is not None and open_q.status == _STATUS_OPEN:
                    index.questions[qid] = QuestionRecord(
                        question_id=open_q.question_id,
                        gate=open_q.gate,
                        text=open_q.text,
                        evidence_ids=open_q.evidence_ids,
                        subjects=open_q.subjects,
                        author=open_q.author,
                        timestamp=open_q.timestamp,
                        status=_STATUS_CLOSED,
                        answered_by=answer_id,
                    )
                    index.open_question_id = None

    def _apply_correction(self, index: _Index, p: Mapping[str, object]) -> None:
        # Mark old answer superseded.
        old_id = _req_str(p, "answer_id")
        old = index.answers.get(old_id)
        if old is not None:
            index.answers[old_id] = AnswerRecord(
                answer_id=old.answer_id,
                gate=old.gate,
                text=old.text,
                author=old.author,
                timestamp=old.timestamp,
                question_id=old.question_id,
                status=_STATUS_SUPERSEDED,
                superseded_by=_opt_str(p.get("replacement_answer_id")),
            )
        # Create new current answer.
        new_id = _req_str(p, "replacement_answer_id")
        new_answer = AnswerRecord(
            answer_id=new_id,
            gate=_req_str(p, "gate"),
            text=_req_str(p, "replacement_text"),
            author=_req_str(p, "author"),
            timestamp=_validate_timestamp(p.get("timestamp")),
            question_id=None,
            status=_STATUS_CURRENT,
            superseded_by=None,
        )
        index.answers[new_id] = new_answer
        index.current_answer_by_gate[new_answer.gate] = new_id
        correction = CorrectionRecord(
            correction_id=_req_str(p, "correction_id"),
            answer_id=old_id,
            gate=_req_str(p, "gate"),
            reason=_req_str(p, "reason"),
            replacement_answer_id=new_id,
            author=_req_str(p, "author"),
            timestamp=_validate_timestamp(p.get("timestamp")),
        )
        index.corrections.append(correction)

    def _apply_disposition(self, index: _Index, p: Mapping[str, object]) -> None:
        gate = _req_str(p, "gate")
        record = GateDispositionRecord(
            gate=gate,
            disposition=_req_str(p, "disposition"),
            reason=_opt_str(p.get("reason")),
            conflict_ids=_tuple_str(p.get("conflict_ids", ())),
            affected_group_ids=_tuple_str(p.get("affected_group_ids", ())),
            author=_req_str(p, "author"),
            timestamp=_validate_timestamp(p.get("timestamp")),
        )
        index.dispositions[gate] = record

    # -- locking ------------------------------------------------------------ #

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Acquire the interview lock, refresh from journal, then release."""

        try:
            self._lock.acquire(timeout=self._lock_timeout)
        except Timeout:
            raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
        try:
            self._index = self._reconstruct()
            yield
        finally:
            if self._lock.lock_counter > 0:
                self._lock.release()


# --------------------------------------------------------------------------- #
# Payload parsing helpers (strict on replay)                                  #
# --------------------------------------------------------------------------- #


def _req_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if type(value) is not str:
        raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
    return value


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is str:
        return value
    raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None


def _tuple_str(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
    return tuple(str(item) for item in value)


def _validate_timestamp(value: object) -> str:
    """Validate that ``value`` is an ISO-8601 UTC string."""

    if type(value) is not str:
        raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
    offset = parsed.tzinfo.utcoffset(parsed) if parsed.tzinfo is not None else None
    if offset != timedelta(0):
        raise InterviewError(CODE_INTERVIEW_STORE_CORRUPT) from None
    return value
