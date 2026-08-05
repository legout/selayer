"""Adaptive interview gates, append-only answers, corrections, and dispositions.

Tests for Task 13 of the discovery companion package:

* :class:`~selayer_discovery.interview.InterviewStore` owns an append-only
  interview journal (``interview/events.jsonl``) and binds every mutation to a
  :class:`~selayer_discovery.session.SessionStore` artifact node so transitive
  stale dependents propagate through the session dependency index.
* Fifteen stable gate IDs gate discovery readiness; every atomic dependency
  group declares which gates affect it and no group becomes ready while an
  affecting gate is undisposed or blocked.
* At most one question may be open at a time; a question cites exactly one
  gate, motivating evidence ids, and in-charter affected subjects.
* Answers dispose their gate as ``answered`` and close the open question.
* Typed corrections cite one current answer, preserve full history, supersede
  the old answer, and emit stale targets for every dependent claim, conflict,
  proposal, report, and attestation.
* Gate dispositions support ``answered``, ``not_applicable`` (requires a
  reason), and ``blocked`` (requires conflict ids and affected group ids).
* Diagnostics never echo question text, answer text, evidence bodies, or raw
  causes.
"""

from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from selayer_discovery.cli import main
from selayer_discovery.interview import (
    CODE_INTERVIEW_ANSWER_NOT_CURRENT,
    CODE_INTERVIEW_DISPOSITION_INVALID,
    CODE_INTERVIEW_EVIDENCE_BODY,
    CODE_INTERVIEW_GATE_UNKNOWN,
    CODE_INTERVIEW_INVALID_INPUT,
    CODE_INTERVIEW_QUESTION_OPEN,
    CODE_INTERVIEW_SUBJECT_CHARTER,
    CODE_INTERVIEW_TEXT_TOO_LARGE,
    GATE_GROUPS,
    GATE_SET,
    GATES,
    InterviewError,
    InterviewStore,
)
from selayer_discovery.model import MAX_TEXT_LENGTH
from selayer_discovery.session import SessionStore

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_session(session_root: Path, charter, actor: str) -> SessionStore:  # type: ignore[no-untyped-def]
    return SessionStore.create(session_root, charter=charter, actor=actor)


def _interview(store: SessionStore) -> InterviewStore:
    return InterviewStore.create(store)


def _ask_grains(interview: InterviewStore, *, actor: str) -> None:
    interview.ask(
        gate="gate-grains",
        text="Is the grain one row per confirmed order?",
        evidence_ids=("document-abcdef0123456789",),
        subjects=("source.shopfloor.orders",),
        actor=actor,
    )


# --------------------------------------------------------------------------- #
# Gate tests (Step 1)                                                         #
# --------------------------------------------------------------------------- #


def test_fifteen_gate_ids_are_stable_and_prefixed() -> None:
    assert len(GATES) == 15
    assert len(GATE_SET) == 15
    for gate in GATES:
        assert gate.startswith("gate-")
        assert gate in GATE_SET


def test_every_group_declares_affecting_gates() -> None:
    assert len(GATE_GROUPS) > 0
    for group, gates in GATE_GROUPS.items():
        assert len(gates) > 0, group
        for gate in gates:
            assert gate in GATE_SET, (group, gate)


def test_all_gates_are_referenced_by_at_least_one_group() -> None:
    referenced: set[str] = set()
    for gates in GATE_GROUPS.values():
        referenced |= set(gates)
    assert referenced == GATE_SET


def test_group_not_ready_with_no_dispositions(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    group = next(iter(GATE_GROUPS))
    assert interview.group_gate_ready(GATE_GROUPS[group]) is False


def test_group_ready_when_all_affecting_gates_answered(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    group = "group-semantic-model"
    for gate in GATE_GROUPS[group]:
        interview.set_gate(
            gate=gate,
            disposition="answered",
            actor=actor,
        )
    assert interview.group_gate_ready(GATE_GROUPS[group]) is True


def test_group_ready_when_all_gates_not_applicable(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    group = next(iter(GATE_GROUPS))
    for gate in GATE_GROUPS[group]:
        interview.set_gate(
            gate=gate,
            disposition="not_applicable",
            reason="Out of scope for this session.",
            actor=actor,
        )
    assert interview.group_gate_ready(GATE_GROUPS[group]) is True


def test_group_not_ready_with_one_blocked_gate(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    group = next(iter(GATE_GROUPS))
    gates = list(GATE_GROUPS[group])
    for gate in gates:
        interview.set_gate(gate=gate, disposition="answered", actor=actor)
    assert interview.group_gate_ready(GATE_GROUPS[group]) is True
    # Block one gate.
    interview.set_gate(
        gate=gates[0],
        disposition="blocked",
        conflict_ids=("conflict-c1",),
        affected_group_ids=(group,),
        actor=actor,
    )
    assert interview.group_gate_ready(GATE_GROUPS[group]) is False


# --------------------------------------------------------------------------- #
# Question tests (Step 2)                                                     #
# --------------------------------------------------------------------------- #


def test_ask_records_question_with_gate_evidence_subjects(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    question = interview.ask(
        gate="gate-grains",
        text="Is the grain one row per confirmed order?",
        evidence_ids=("document-abcdef0123456789",),
        subjects=("source.shopfloor.orders",),
        actor=actor,
    )
    assert question.question_id == "question-1"
    assert question.gate == "gate-grains"
    assert question.status == "open"
    assert question.evidence_ids == ("document-abcdef0123456789",)
    assert question.subjects == ("source.shopfloor.orders",)
    assert interview.open_question() is not None
    open_q = interview.open_question()
    assert open_q is not None
    assert open_q.question_id == "question-1"


def test_ask_rejects_second_open_question(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    _ask_grains(interview, actor=actor)
    with pytest.raises(InterviewError) as exc:
        _ask_grains(interview, actor=actor)
    assert exc.value.code == CODE_INTERVIEW_QUESTION_OPEN


def test_ask_rejects_oversized_text(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    with pytest.raises(InterviewError) as exc:
        interview.ask(
            gate="gate-grains",
            text="x" * (MAX_TEXT_LENGTH + 1),
            evidence_ids=("document-abcdef0123456789",),
            subjects=("source.shopfloor.orders",),
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_TEXT_TOO_LARGE


def test_ask_rejects_evidence_body_interpolation(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    content_hash = "a" * 64
    with pytest.raises(InterviewError) as exc:
        interview.ask(
            gate="gate-grains",
            text=f"What about evidence {content_hash} pasted inline?",
            evidence_ids=("document-abcdef0123456789",),
            subjects=("source.shopfloor.orders",),
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_EVIDENCE_BODY


def test_ask_rejects_out_of_charter_subject(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    with pytest.raises(InterviewError) as exc:
        interview.ask(
            gate="gate-grains",
            text="Is the grain correct?",
            evidence_ids=("document-abcdef0123456789",),
            subjects=("domain.finance",),  # excluded
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_SUBJECT_CHARTER


def test_ask_rejects_subject_outside_inclusions(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    with pytest.raises(InterviewError) as exc:
        interview.ask(
            gate="gate-grains",
            text="Is the grain correct?",
            evidence_ids=("document-abcdef0123456789",),
            subjects=("source.other.unscoped",),
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_SUBJECT_CHARTER


def test_ask_accepts_subpath_of_inclusion(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    question = interview.ask(
        gate="gate-grains",
        text="Is the grain correct?",
        evidence_ids=("document-abcdef0123456789",),
        subjects=("source.shopfloor.orders.status",),
        actor=actor,
    )
    assert question.subjects == ("source.shopfloor.orders.status",)


def test_ask_rejects_unknown_gate(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    with pytest.raises(InterviewError) as exc:
        interview.ask(
            gate="gate-nonexistent",
            text="?",
            evidence_ids=("document-abcdef0123456789",),
            subjects=("source.shopfloor.orders",),
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_GATE_UNKNOWN


def test_ask_rejects_invalid_evidence_id(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    with pytest.raises(InterviewError) as exc:
        interview.ask(
            gate="gate-grains",
            text="?",
            evidence_ids=("UPPERCASE",),
            subjects=("source.shopfloor.orders",),
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_INVALID_INPUT


def test_ask_requires_at_least_one_evidence_and_subject(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    with pytest.raises(InterviewError) as exc:
        interview.ask(
            gate="gate-grains",
            text="?",
            evidence_ids=(),
            subjects=("source.shopfloor.orders",),
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_INVALID_INPUT
    with pytest.raises(InterviewError) as exc2:
        interview.ask(
            gate="gate-grains",
            text="?",
            evidence_ids=("document-abc",),
            subjects=(),
            actor=actor,
        )
    assert exc2.value.code == CODE_INTERVIEW_INVALID_INPUT


# --------------------------------------------------------------------------- #
# Answer tests                                                                #
# --------------------------------------------------------------------------- #


def test_answer_closes_open_question_and_disposes_gate(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    _ask_grains(interview, actor=actor)
    result = interview.answer(
        gate="gate-grains",
        text="Yes, one row per confirmed order.",
        actor=actor,
    )
    assert result.answer.answer_id == "answer-1"
    assert result.answer.gate == "gate-grains"
    assert result.answer.status == "current"
    # The open question is now closed.
    assert interview.open_question() is None
    questions = interview.questions()
    assert questions[0].status == "closed"
    assert questions[0].answered_by == "answer-1"
    # The gate is disposed as answered.
    disp = interview.dispositions()
    assert disp["gate-grains"].disposition == "answered"


def test_answer_without_open_question_still_works(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    result = interview.answer(
        gate="gate-grains",
        text="One row per order.",
        actor=actor,
    )
    assert result.answer.status == "current"
    assert interview.dispositions()["gate-grains"].disposition == "answered"


def test_answer_registers_session_artifact(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    interview.answer(gate="gate-grains", text="One row.", actor=actor)
    snapshot = store.reconstruct()
    assert "answer-gate-grains" in snapshot.artifact_hashes


# --------------------------------------------------------------------------- #
# Correction tests (Step 3)                                                   #
# --------------------------------------------------------------------------- #


def _seed_dependents(
    store: SessionStore, answer_artifact: str, actor: str, hash_factory
) -> None:  # type: ignore[no-untyped-def]
    """Register dependent artifacts on the answer node."""

    for node, idx in (
        ("claim-c1", 1),
        ("conflict-c1", 2),
        ("proposal-g1", 3),
        ("report-r1", 4),
        ("attestation-a1", 5),
    ):
        store.record_artifact(
            node,
            content_hash=hash_factory(idx),
            depends_on=(answer_artifact,),
            actor=actor,
        )


def test_correct_preserves_history_and_supersedes(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    interview.answer(gate="gate-grains", text="Original grain.", actor=actor)
    old_answer_id = interview.current_answer("gate-grains").answer_id

    result = interview.correct(
        answer_id=old_answer_id,
        reason="Grain missed cancelled orders.",
        replacement="Corrected grain including cancellations.",
        actor=actor,
    )
    # History preserved: old answer still in the record, now superseded.
    all_answers = interview.answers()
    assert len(all_answers) == 2
    old = next(a for a in all_answers if a.answer_id == old_answer_id)
    assert old.status == "superseded"
    assert old.superseded_by == result.new_answer.answer_id
    # New answer is current.
    assert result.new_answer.status == "current"
    assert (
        interview.current_answer("gate-grains").answer_id == result.new_answer.answer_id
    )


def test_correct_emits_stale_targets_for_dependents(
    session_root: Path,
    charter,  # type: ignore[no-untyped-def]
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    interview.answer(gate="gate-grains", text="Original grain.", actor=actor)
    answer_artifact = "answer-gate-grains"
    _seed_dependents(store, answer_artifact, actor, hash_factory)

    old_answer_id = interview.current_answer("gate-grains").answer_id
    result = interview.correct(
        answer_id=old_answer_id,
        reason="Grain was wrong.",
        replacement="Corrected grain definition.",
        actor=actor,
    )
    stale = set(result.stale_targets)
    assert {
        "claim-c1",
        "conflict-c1",
        "proposal-g1",
        "report-r1",
        "attestation-a1",
    } <= stale


def test_correct_rejects_superseded_answer(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    interview.answer(gate="gate-grains", text="Original.", actor=actor)
    first = interview.current_answer("gate-grains").answer_id
    interview.correct(
        answer_id=first,
        reason="Wrong.",
        replacement="Corrected.",
        actor=actor,
    )
    # Correcting the already-superseded answer fails.
    with pytest.raises(InterviewError) as exc:
        interview.correct(
            answer_id=first,
            reason="Again.",
            replacement="Again.",
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_ANSWER_NOT_CURRENT


def test_correct_rejects_unknown_answer(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    with pytest.raises(InterviewError) as exc:
        interview.correct(
            answer_id="answer-999",
            reason="No such answer.",
            replacement="Replacement.",
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_ANSWER_NOT_CURRENT


# --------------------------------------------------------------------------- #
# Disposition tests (Step 4)                                                  #
# --------------------------------------------------------------------------- #


def test_set_gate_answered(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    record = interview.set_gate(gate="gate-grains", disposition="answered", actor=actor)
    assert record.gate == "gate-grains"
    assert record.disposition == "answered"
    assert interview.dispositions()["gate-grains"].disposition == "answered"


def test_set_gate_not_applicable_requires_reason(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    with pytest.raises(InterviewError) as exc:
        interview.set_gate(gate="gate-kpi", disposition="not_applicable", actor=actor)
    assert exc.value.code == CODE_INTERVIEW_DISPOSITION_INVALID
    # With a reason it succeeds.
    record = interview.set_gate(
        gate="gate-kpi",
        disposition="not_applicable",
        reason="No KPIs in scope.",
        actor=actor,
    )
    assert record.disposition == "not_applicable"


def test_set_gate_blocked_requires_conflicts_and_groups(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    # Missing conflict ids.
    with pytest.raises(InterviewError) as exc:
        interview.set_gate(
            gate="gate-conflicts",
            disposition="blocked",
            affected_group_ids=("group-delivery",),
            actor=actor,
        )
    assert exc.value.code == CODE_INTERVIEW_DISPOSITION_INVALID
    # Missing affected group ids.
    with pytest.raises(InterviewError) as exc2:
        interview.set_gate(
            gate="gate-conflicts",
            disposition="blocked",
            conflict_ids=("conflict-c1",),
            actor=actor,
        )
    assert exc2.value.code == CODE_INTERVIEW_DISPOSITION_INVALID
    # With both it succeeds.
    record = interview.set_gate(
        gate="gate-conflicts",
        disposition="blocked",
        conflict_ids=("conflict-c1",),
        affected_group_ids=("group-delivery",),
        actor=actor,
    )
    assert record.disposition == "blocked"
    assert record.conflict_ids == ("conflict-c1",)


def test_set_gate_rejects_unknown_gate(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    with pytest.raises(InterviewError) as exc:
        interview.set_gate(gate="gate-bogus", disposition="answered", actor=actor)
    assert exc.value.code == CODE_INTERVIEW_GATE_UNKNOWN


# --------------------------------------------------------------------------- #
# Persistence / journal tests                                                 #
# --------------------------------------------------------------------------- #


def test_state_survives_reopen(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    _ask_grains(interview, actor=actor)
    interview.answer(gate="gate-grains", text="Yes.", actor=actor)
    interview.set_gate(
        gate="gate-kpi",
        disposition="not_applicable",
        reason="No KPIs.",
        actor=actor,
    )

    reopened_store = SessionStore.open(session_root)
    reopened = InterviewStore.open(reopened_store)
    assert len(reopened.questions()) == 1
    assert reopened.questions()[0].status == "closed"
    assert len(reopened.answers()) == 1
    assert reopened.dispositions()["gate-grains"].disposition == "answered"
    assert reopened.dispositions()["gate-kpi"].disposition == "not_applicable"


def test_interview_journal_is_append_only(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    _ask_grains(interview, actor=actor)
    journal = (store.root / "interview" / "events.jsonl").read_text(encoding="utf-8")
    lines = [line for line in journal.splitlines() if line]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "question_asked"


# --------------------------------------------------------------------------- #
# Safety: diagnostics never leak text                                         #
# --------------------------------------------------------------------------- #


def test_diagnostics_never_leak_question_text(
    session_root: Path,
    charter,
    actor: str,  # type: ignore[no-untyped-def]
) -> None:
    store = _make_session(session_root, charter, actor)
    interview = _interview(store)
    _ask_grains(interview, actor=actor)
    secret = "super-secret-evidence-content"
    try:
        interview.ask(
            gate="gate-grains",
            text=secret,
            evidence_ids=("document-abc",),
            subjects=("source.shopfloor.orders",),
            actor=actor,
        )
    except InterviewError as exc:
        rendered = str(exc) + repr(exc) + str(exc.to_dict())
        assert secret not in rendered
    else:
        pytest.fail("expected InterviewError")


# --------------------------------------------------------------------------- #
# CLI integration tests (Step 5)                                             #
# --------------------------------------------------------------------------- #

_CHARTER_YAML: dict[str, Any] = {
    "business_question": "Is the order_facts grain one row per confirmed order?",
    "approver": "Dr. Alice Okonkwo",
    "catalog_fingerprint": "a" * 64,
    "inclusions": ["source.shopfloor.orders"],
    "exclusions": ["domain.finance"],
    "acceptance_questions": ["Does the corrected grain pass the uniqueness audit?"],
}


def _dump_yaml(data: dict[str, Any]) -> str:
    buf = StringIO()
    YAML().dump(data, buf)
    return buf.getvalue()


def _init_cli_session(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    session_id: str = "session-cli-001",
) -> None:
    """Init a git repo, write a charter, and create a discovery session via CLI."""

    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    (project / ".gitignore").write_text(
        ".selayer/discovery/sessions/\n.selayer/discovery/transactions/\n",
        encoding="utf-8",
    )
    (project / "charter.yaml").write_text(_dump_yaml(_CHARTER_YAML), encoding="utf-8")
    (project / "catalogs").mkdir(exist_ok=True)
    (project / "catalogs" / "shopfloor.yaml").write_text("", encoding="utf-8")
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(project / "charter.yaml"),
            "--project",
            str(project),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
            "--session-id",
            session_id,
        ]
    )
    assert code == 0
    capsys.readouterr()  # consume init output


def _write_json(project: Path, name: str, data: dict[str, Any]) -> Path:
    path = project / name
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return path


def test_cli_interview_ask(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _init_cli_session(tmp_path, capsys)
    question_path = _write_json(
        tmp_path,
        "question.json",
        {
            "gate": "gate-grains",
            "text": "Is the grain one row per confirmed order?",
            "evidence_ids": ["document-abcdef0123456789"],
            "subjects": ["source.shopfloor.orders"],
        },
    )
    code = main(
        [
            "interview",
            "ask",
            "--session-id",
            "session-cli-001",
            "--project",
            str(tmp_path),
            "--question",
            str(question_path),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["question_id"] == "question-1"
    assert out["gate"] == "gate-grains"
    assert out["status"] == "open"
    assert out["evidence_ids"] == ["document-abcdef0123456789"]
    assert out["subjects"] == ["source.shopfloor.orders"]


def test_cli_interview_answer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_cli_session(tmp_path, capsys)
    question_path = _write_json(
        tmp_path,
        "question.json",
        {
            "gate": "gate-grains",
            "text": "Is the grain one row per confirmed order?",
            "evidence_ids": ["document-abcdef0123456789"],
            "subjects": ["source.shopfloor.orders"],
        },
    )
    main(
        [
            "interview",
            "ask",
            "--session-id",
            "session-cli-001",
            "--project",
            str(tmp_path),
            "--question",
            str(question_path),
        ]
    )
    capsys.readouterr()
    answer_path = _write_json(
        tmp_path,
        "answer.json",
        {"gate": "gate-grains", "text": "Yes, one row per confirmed order."},
    )
    code = main(
        [
            "interview",
            "answer",
            "--session-id",
            "session-cli-001",
            "--project",
            str(tmp_path),
            "--answer",
            str(answer_path),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["answer_id"] == "answer-1"
    assert out["gate"] == "gate-grains"
    assert out["status"] == "current"
    assert out["stale_targets"] == []


def test_cli_interview_correct(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_cli_session(tmp_path, capsys)
    answer_path = _write_json(
        tmp_path,
        "answer.json",
        {"gate": "gate-grains", "text": "Original grain answer."},
    )
    main(
        [
            "interview",
            "answer",
            "--session-id",
            "session-cli-001",
            "--project",
            str(tmp_path),
            "--answer",
            str(answer_path),
        ]
    )
    capsys.readouterr()
    correction_path = _write_json(
        tmp_path,
        "correction.json",
        {
            "answer_id": "answer-1",
            "reason": "Grain missed cancelled orders.",
            "replacement": "Corrected grain including cancellations.",
        },
    )
    code = main(
        [
            "interview",
            "correct",
            "--session-id",
            "session-cli-001",
            "--project",
            str(tmp_path),
            "--correction",
            str(correction_path),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["correction_id"] == "correction-1"
    assert out["answer_id"] == "answer-1"
    assert out["replacement_answer_id"] == "answer-2"


def test_cli_interview_set_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_cli_session(tmp_path, capsys)
    disp_path = _write_json(
        tmp_path,
        "disposition.json",
        {"disposition": "not_applicable", "reason": "No KPIs in scope."},
    )
    code = main(
        [
            "interview",
            "set-gate",
            "--session-id",
            "session-cli-001",
            "--project",
            str(tmp_path),
            "--gate",
            "gate-kpi",
            "--disposition",
            str(disp_path),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["gate"] == "gate-kpi"
    assert out["disposition"] == "not_applicable"


def test_cli_interview_output_is_deterministic_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON stdout keys are sorted (deterministic output)."""

    _init_cli_session(tmp_path, capsys)
    disp_path = _write_json(
        tmp_path,
        "disposition.json",
        {"disposition": "answered"},
    )
    main(
        [
            "interview",
            "set-gate",
            "--session-id",
            "session-cli-001",
            "--project",
            str(tmp_path),
            "--gate",
            "gate-grains",
            "--disposition",
            str(disp_path),
        ]
    )
    raw = capsys.readouterr().out
    assert raw == json.dumps(json.loads(raw), sort_keys=True) + "\n"


def test_cli_interview_never_leaks_answer_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Success output carries no free-text answer content."""

    _init_cli_session(tmp_path, capsys)
    secret = "confidential-business-rule-detail"
    answer_path = _write_json(
        tmp_path,
        "answer.json",
        {"gate": "gate-business-rules", "text": secret},
    )
    main(
        [
            "interview",
            "answer",
            "--session-id",
            "session-cli-001",
            "--project",
            str(tmp_path),
            "--answer",
            str(answer_path),
        ]
    )
    raw = capsys.readouterr().out
    assert secret not in raw


def test_cli_interview_commands_are_deterministic_no_llm(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Running the same command twice yields identical output (no LLM nondeterminism)."""

    outputs: list[str] = []
    for i in range(2):
        sid = f"session-det-{i + 1:03d}"
        _init_cli_session(tmp_path, capsys, session_id=sid)
        disp_path = _write_json(
            tmp_path,
            f"disp-{i}.json",
            {"disposition": "answered"},
        )
        main(
            [
                "interview",
                "set-gate",
                "--session-id",
                sid,
                "--project",
                str(tmp_path),
                "--gate",
                "gate-grains",
                "--disposition",
                str(disp_path),
            ]
        )
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1]
