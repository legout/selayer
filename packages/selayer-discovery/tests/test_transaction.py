from __future__ import annotations

import json
from pathlib import Path

import pytest
from selayer_discovery.transaction import (
    ApplyJournal,
    FailureInjector,
    RecoveryConflict,
    TransactionError,
    recover,
)


def test_prepare_writes_complete_durable_journal_without_mutating_targets(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "catalog.yaml"
    target.write_text("old", encoding="utf-8")
    transactions = project / "transactions"

    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=transactions,
        transaction_id="tx-001",
        actor="Dr Alice Okonkwo",
        files={"catalog.yaml": b"new"},
    )

    assert target.read_text(encoding="utf-8") == "old"
    record = journal.records[0]
    assert record.path == "catalog.yaml"
    assert record.old_hash
    assert record.old_absent is False
    assert record.backup_path
    assert record.staged_path
    assert record.new_hash
    assert record.state == "prepared"
    journal_data = json.loads(journal.journal_path.read_text(encoding="utf-8"))
    assert journal_data["state"] == "prepared"
    assert journal_data["targets"][0]["old_hash"] == record.old_hash
    assert (journal.root / record.backup_path).exists()
    assert (journal.root / record.staged_path).exists()


def test_apply_and_recover_success_are_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "new.txt"
    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=project / "transactions",
        transaction_id="tx-002",
        actor="Alice",
        files={"new.txt": b"created"},
    )
    journal.apply()
    assert target.read_bytes() == b"created"
    assert recover(project / "transactions", project_root=project) == ()
    assert recover(project / "transactions", project_root=project) == ()
    assert (journal.root / "success.json").exists()
    assert (journal.root / "applied.json").exists()


def test_rollback_restores_existing_and_removes_new_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    existing = project / "old.txt"
    existing.write_bytes(b"old")
    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=project / "transactions",
        transaction_id="tx-003",
        actor="Alice",
        files={"old.txt": b"new", "new.txt": b"created"},
    )
    journal.apply()
    journal.rollback()
    assert existing.read_bytes() == b"old"
    assert not (project / "new.txt").exists()
    assert journal.state == "rolled_back"


def test_failure_before_replace_leaves_recoverable_journal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "file.txt"
    target.write_bytes(b"old")
    injector = FailureInjector({"before_replace"})
    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=project / "transactions",
        transaction_id="tx-004",
        actor="Alice",
        files={"file.txt": b"new"},
        injector=injector,
    )
    with pytest.raises(TransactionError):
        journal.apply()
    assert target.read_bytes() == b"old"
    assert recover(project / "transactions", project_root=project) == ("tx-004",)
    assert target.read_bytes() == b"old"


def test_rollback_stops_on_unexpected_current_hash_and_retains_backup(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "file.txt"
    target.write_bytes(b"old")
    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=project / "transactions",
        transaction_id="tx-005",
        actor="Alice",
        files={"file.txt": b"new"},
    )
    target.write_bytes(b"third-party")
    with pytest.raises(RecoveryConflict):
        journal.rollback()
    assert target.read_bytes() == b"third-party"
    assert (journal.root / journal.records[0].backup_path).exists()


def test_apply_conflicts_when_original_is_deleted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "file.txt"
    target.write_bytes(b"old")
    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=project / "transactions",
        transaction_id="tx-006",
        actor="Alice",
        files={"file.txt": b"new"},
    )
    target.unlink()
    with pytest.raises(RecoveryConflict):
        journal.apply()


def test_symlink_target_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (project / "file.txt").symlink_to(outside)
    with pytest.raises(TransactionError):
        ApplyJournal.create(
            project_root=project,
            transaction_root=project / "transactions",
            transaction_id="tx-007",
            actor="Alice",
            files={"file.txt": b"new"},
        )


@pytest.mark.parametrize(
    "failure",
    (
        "staged_file_fsync",
        "backup_write_fsync",
        "initial_journal_fsync",
    ),
)
def test_prepare_durability_failures_leave_targets_unchanged(
    tmp_path: Path, failure: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "file.txt"
    target.write_bytes(b"old")
    with pytest.raises(TransactionError):
        ApplyJournal.create(
            project_root=project,
            transaction_root=project / "transactions",
            transaction_id=f"tx-{failure.replace('_', '-')}",
            actor="Alice",
            files={"file.txt": b"new"},
            injector=FailureInjector({failure}),
        )
    assert target.read_bytes() == b"old"


@pytest.mark.parametrize(
    "failure",
    (
        "next_target_fsync",
        "replace",
        "target_directory_fsync",
        "replaced_fsync",
        "success_marker_fsync",
        "applied_event_fsync",
    ),
)
def test_apply_durability_failures_are_recoverable(
    tmp_path: Path, failure: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "file.txt"
    target.write_bytes(b"old")
    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=project / "transactions",
        transaction_id=f"tx-{failure.replace('_', '-')}",
        actor="Alice",
        files={"file.txt": b"new"},
        injector=FailureInjector({failure}),
    )
    with pytest.raises(TransactionError):
        journal.apply()
    assert recover(project / "transactions", project_root=project)


def test_recovery_finalizes_missing_applied_marker(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=project / "transactions",
        transaction_id="tx-008",
        actor="Alice",
        files={"file.txt": b"new"},
    )
    journal.apply()
    (journal.root / "applied.json").unlink()
    assert recover(project / "transactions", project_root=project) == ("tx-008",)
    assert (journal.root / "applied.json").exists()


def test_corrupt_backup_causes_recovery_conflict(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "file.txt"
    target.write_bytes(b"old")
    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=project / "transactions",
        transaction_id="tx-009",
        actor="Alice",
        files={"file.txt": b"new"},
    )
    journal.apply()
    (journal.root / journal.records[0].backup_path).write_bytes(b"tampered")
    with pytest.raises(RecoveryConflict):
        journal.rollback()
