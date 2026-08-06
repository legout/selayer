"""Durable, recoverable file transactions for approved discovery changes."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

from filelock import FileLock, Timeout

__all__ = [
    "ApplyJournal",
    "FailureInjector",
    "ProjectLock",
    "RecoveryConflict",
    "TargetRecord",
    "TransactionError",
    "recover",
]

_HEX64 = 64
_MAX_TRANSACTION_ID = 128


class TransactionError(Exception):
    """Sanitized transaction failure with a stable code."""

    def __init__(self, code: str = "discovery.transaction.failed") -> None:
        self.code = code
        super().__init__(code)


class RecoveryConflict(TransactionError):
    """The target changed outside the transaction and cannot be guessed."""

    def __init__(self) -> None:
        super().__init__("discovery.transaction.recovery_conflict")


class FailureInjector:
    """Deterministic failure injector used by durability tests."""

    def __init__(self, failures: set[str] | Sequence[str] = ()) -> None:
        self.failures = set(failures)

    def __call__(self, point: str, phase: str) -> None:
        if point in self.failures or f"{phase}_{point}" in self.failures:
            raise TransactionError("discovery.transaction.injected_failure")


@dataclass(frozen=True, slots=True)
class TargetRecord:
    path: str
    old_hash: str | None
    old_absent: bool
    backup_path: str
    staged_path: str
    new_hash: str
    state: str = "prepared"

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "old_hash": self.old_hash,
            "old_absent": self.old_absent,
            "backup_path": self.backup_path,
            "staged_path": self.staged_path,
            "new_hash": self.new_hash,
            "state": self.state,
        }

    @classmethod
    def from_mapping(cls, value: object) -> TargetRecord:
        if not isinstance(value, Mapping):
            raise TransactionError("discovery.transaction.invalid_journal")
        required = {
            "path", "old_hash", "old_absent", "backup_path", "staged_path",
            "new_hash", "state",
        }
        if set(value) != required:
            raise TransactionError("discovery.transaction.invalid_journal")
        if not isinstance(value["path"], str) or not isinstance(value["backup_path"], str):
            raise TransactionError("discovery.transaction.invalid_journal")
        if not isinstance(value["staged_path"], str) or not isinstance(value["new_hash"], str):
            raise TransactionError("discovery.transaction.invalid_journal")
        if not isinstance(value["state"], str) or type(value["old_absent"]) is not bool:
            raise TransactionError("discovery.transaction.invalid_journal")
        old_hash = value["old_hash"]
        if old_hash is not None and not isinstance(old_hash, str):
            raise TransactionError("discovery.transaction.invalid_journal")
        return cls(
            path=value["path"],
            old_hash=old_hash,
            old_absent=value["old_absent"],
            backup_path=value["backup_path"],
            staged_path=value["staged_path"],
            new_hash=value["new_hash"],
            state=value["state"],
        )


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        raise TransactionError("discovery.transaction.target_unreadable") from None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise TransactionError("discovery.transaction.target_unreadable") from None
    return digest.hexdigest()


def _fsync_file(path: Path, injector: Callable[[str, str], None] | None, point: str) -> None:
    if injector is not None:
        injector(point, "before")
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise TransactionError("discovery.transaction.durability_failed") from None
    if injector is not None:
        injector(point, "after")


def _safe_file(path: Path) -> Path:
    """Reject symlinks and non-regular artifact paths."""

    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return path
    except OSError:
        raise TransactionError("discovery.transaction.target_unreadable") from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise TransactionError("discovery.transaction.path_not_contained")
    return path


def _atomic_json_write(
    path: Path,
    value: Mapping[str, object],
    injector: Callable[[str, str], None] | None,
    point: str,
) -> None:
    """Write a JSON marker without following pre-existing symlinks."""

    _safe_file(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    _safe_file(temporary)
    try:
        if temporary.exists():
            temporary.unlink()
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, json.dumps(dict(value), sort_keys=True).encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if injector is not None:
            injector(point, "after")
        os.replace(temporary, path)
    except FileExistsError:
        raise TransactionError("discovery.transaction.path_not_contained") from None
    except OSError:
        raise TransactionError("discovery.transaction.durability_failed") from None
    _fsync_file(path, injector, point)
    _fsync_directory(path.parent, injector, "marker_directory_fsync")


def _write_bytes(path: Path, content: bytes) -> None:
    try:
        path.write_bytes(content)
        os.chmod(path, 0o600)
    except OSError:
        raise TransactionError("discovery.transaction.durability_failed") from None


def _fsync_directory(
    path: Path,
    injector: Callable[[str, str], None] | None = None,
    point: str = "directory_fsync",
) -> None:
    if injector is not None:
        injector(point, "before")
    if os.name != "posix":
        if injector is not None:
            injector(point, "after")
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise TransactionError("discovery.transaction.durability_failed") from None
    if injector is not None:
        injector(point, "after")


def _safe_rel(value: str) -> str:
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise TransactionError("discovery.transaction.path_not_contained")
    return pure.as_posix()


def _safe_target_path(project_root: Path, relative: str) -> Path:
    rel = _safe_rel(relative)
    raw = project_root / rel
    cursor = project_root
    try:
        for component in PurePosixPath(rel).parts:
            cursor = cursor / component
            try:
                mode = os.lstat(cursor).st_mode
            except FileNotFoundError:
                break
            if stat.S_ISLNK(mode):
                raise TransactionError("discovery.transaction.path_not_contained")
        resolved = raw.resolve(strict=False)
        resolved.relative_to(project_root.resolve())
    except FileNotFoundError:
        resolved = raw.resolve(strict=False)
        try:
            resolved.relative_to(project_root.resolve())
        except ValueError:
            raise TransactionError("discovery.transaction.path_not_contained") from None
    except OSError:
        raise TransactionError("discovery.transaction.target_unreadable") from None
    except ValueError:
        raise TransactionError("discovery.transaction.path_not_contained") from None
    return resolved


class ProjectLock:
    """Project-wide OS lock with safe owner metadata."""

    def __init__(self, project_root: Path, transaction_root: Path, transaction_id: str, actor: str) -> None:
        self.project_root = project_root.resolve()
        try:
            self.root = transaction_root.resolve()
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise TransactionError("discovery.transaction.durability_failed") from None
        self.path = self.root / ".project.lock"
        self.owner_path = self.root / ".project-owner.json"
        self.transaction_id = transaction_id
        self.actor = actor.strip()[:128]
        self._lock = FileLock(str(self.path))

    def __enter__(self) -> Self:
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            raise TransactionError("discovery.transaction.locked") from None
        try:
            _atomic_json_write(
                self.owner_path,
                {"transaction_id": self.transaction_id, "actor": self.actor},
                None,
                "owner_fsync",
            )
        except TransactionError:
            self._lock.release()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.owner_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._lock.release()


class ApplyJournal:
    """A journal-backed transaction whose target writes are recoverable."""

    def __init__(
        self,
        *,
        project_root: Path,
        root: Path,
        transaction_id: str,
        actor: str,
        records: tuple[TargetRecord, ...],
        state: str,
        next_target: int = 0,
        injector: Callable[[str, str], None] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.root = root.resolve()
        self.transaction_id = transaction_id
        self.actor = actor
        self._records = records
        self._state = state
        self._next_target = next_target
        self._injector = injector

    @property
    def journal_path(self) -> Path:
        return self.root / "journal.json"

    @property
    def records(self) -> tuple[TargetRecord, ...]:
        return self._records

    @property
    def state(self) -> str:
        return self._state

    @classmethod
    def create(
        cls,
        *,
        project_root: Path,
        transaction_root: Path,
        transaction_id: str,
        actor: str,
        files: Mapping[str, bytes],
        injector: Callable[[str, str], None] | None = None,
    ) -> ApplyJournal:
        project = project_root.resolve()
        try:
            transaction_root.resolve().relative_to(project)
        except ValueError:
            raise TransactionError("discovery.transaction.path_not_contained") from None
        with ProjectLock(project, transaction_root, transaction_id, actor):
            _recover_unlocked(transaction_root, project_root=project)
            return cls._create_unlocked(
                project_root=project,
                transaction_root=transaction_root,
                transaction_id=transaction_id,
                actor=actor,
                files=files,
                injector=injector,
            )

    @classmethod
    def _create_unlocked(
        cls,
        *,
        project_root: Path,
        transaction_root: Path,
        transaction_id: str,
        actor: str,
        files: Mapping[str, bytes],
        injector: Callable[[str, str], None] | None = None,
    ) -> ApplyJournal:
        if not isinstance(transaction_id, str) or not transaction_id or len(transaction_id) > _MAX_TRANSACTION_ID:
            raise TransactionError("discovery.transaction.invalid_transaction")
        project = project_root.resolve()
        root = (transaction_root / transaction_id).resolve()
        try:
            root.relative_to(transaction_root.resolve())
        except ValueError:
            raise TransactionError("discovery.transaction.path_not_contained") from None
        if _exists(root):
            raise TransactionError("discovery.transaction.exists")
        try:
            root.mkdir(parents=True)
            (root / "staged").mkdir()
            (root / "backups").mkdir()
        except OSError:
            raise TransactionError("discovery.transaction.durability_failed") from None
        records: list[TargetRecord] = []
        for index, (raw_path, content) in enumerate(sorted(files.items())):
            if not isinstance(content, bytes):
                raise TransactionError("discovery.transaction.invalid_target")
            rel = _safe_rel(raw_path)
            target = _safe_target_path(project, rel)
            if _exists(target):
                try:
                    mode = os.lstat(target).st_mode
                except OSError:
                    raise TransactionError("discovery.transaction.target_unreadable") from None
                if not stat.S_ISREG(mode):
                    raise TransactionError("discovery.transaction.target_not_regular")
            old_absent = not _exists(target)
            old_hash = None if old_absent else _hash_file(target)
            staged_rel = f"staged/{index:04d}.bin"
            backup_rel = f"backups/{index:04d}.bak"
            staged = root / staged_rel
            _write_bytes(staged, content)
            _fsync_file(staged, injector, "staged_file_fsync")
            if not old_absent:
                backup = root / backup_rel
                try:
                    old_content = target.read_bytes()
                except OSError:
                    raise TransactionError("discovery.transaction.target_unreadable") from None
                _write_bytes(backup, old_content)
                _fsync_file(backup, injector, "backup_write_fsync")
            records.append(
                TargetRecord(
                    path=rel,
                    old_hash=old_hash,
                    old_absent=old_absent,
                    backup_path=backup_rel,
                    staged_path=staged_rel,
                    new_hash=_hash_bytes(content),
                )
            )
        journal = cls(
            project_root=project,
            root=root,
            transaction_id=transaction_id,
            actor=actor,
            records=tuple(records),
            state="prepared",
            injector=injector,
        )
        journal._write_journal("initial_journal_fsync")
        return journal

    @classmethod
    def open(cls, journal_path: Path, *, project_root: Path, injector: Callable[[str, str], None] | None = None) -> ApplyJournal:
        _safe_file(journal_path)
        try:
            data = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise TransactionError("discovery.transaction.invalid_journal") from None
        if not isinstance(data, Mapping):
            raise TransactionError("discovery.transaction.invalid_journal")
        records_raw = data.get("targets")
        if not isinstance(records_raw, Sequence) or isinstance(records_raw, (str, bytes)):
            raise TransactionError("discovery.transaction.invalid_journal")
        tx = data.get("transaction_id")
        actor = data.get("actor")
        state = data.get("state")
        if not isinstance(tx, str) or not isinstance(actor, str) or not isinstance(state, str):
            raise TransactionError("discovery.transaction.invalid_journal")
        next_target = data.get("next_target", 0)
        if type(next_target) is not int or next_target < 0:
            raise TransactionError("discovery.transaction.invalid_journal")
        return cls(
            project_root=project_root,
            root=journal_path.parent,
            transaction_id=tx,
            actor=actor,
            records=tuple(TargetRecord.from_mapping(item) for item in records_raw),
            state=state,
            next_target=next_target,
            injector=injector,
        )

    def _write_journal(self, point: str | None = None) -> None:
        data = {
            "schema_version": 1,
            "transaction_id": self.transaction_id,
            "actor": self.actor,
            "state": self._state,
            "next_target": self._next_target,
            "targets": [record.to_dict() for record in self._records],
        }
        _atomic_json_write(
            self.journal_path,
            data,
            self._injector,
            point or "journal_fsync",
        )
        _fsync_directory(self.root, self._injector, "journal_directory_fsync")

    def _target(self, record: TargetRecord) -> Path:
        return _safe_target_path(self.project_root, record.path)

    def _artifact(self, relative: str) -> Path:
        safe = _safe_rel(relative)
        cursor = self.root
        for component in PurePosixPath(safe).parts:
            cursor = cursor / component
            try:
                mode = os.lstat(cursor).st_mode
            except FileNotFoundError:
                break
            except OSError:
                raise TransactionError("discovery.transaction.target_unreadable") from None
            if stat.S_ISLNK(mode):
                raise TransactionError("discovery.transaction.path_not_contained")
        try:
            path = (self.root / safe).resolve(strict=False)
            path.relative_to(self.root)
        except OSError:
            raise TransactionError("discovery.transaction.target_unreadable") from None
        except ValueError:
            raise TransactionError("discovery.transaction.path_not_contained") from None
        return path

    def apply(self) -> None:
        if self._state in {"completed", "rolled_back"}:
            return
        with ProjectLock(self.project_root, self.root.parent, self.transaction_id, self.actor):
            self._state = "applying"
            self._write_journal("next_target_fsync")
            for index, record in enumerate(self._records):
                self._next_target = index
                self._write_journal("next_target_fsync")
                target = self._target(record)
                if not _exists(target) and not record.old_absent:
                    raise RecoveryConflict()
                if _exists(target) and _hash_file(target) not in {record.old_hash, record.new_hash}:
                    raise RecoveryConflict()
                if _exists(target) and _hash_file(target) == record.new_hash:
                    self._records = tuple(
                        replace(item, state="replaced") if item.path == record.path else item
                        for item in self._records
                    )
                    continue
                self._inject("replace", "before")
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(self._artifact(record.staged_path), target)
                except OSError:
                    raise TransactionError("discovery.transaction.target_unreadable") from None
                self._inject("replace", "after")
                _fsync_file(target, self._injector, "target_directory_fsync")
                _fsync_directory(target.parent, self._injector, "target_directory_fsync")
                self._records = tuple(
                    replace(item, state="replaced") if item.path == record.path else item
                    for item in self._records
                )
                self._write_journal("replaced_fsync")
            success = self.root / "success.json"
            _atomic_json_write(
                success,
                {"transaction_id": self.transaction_id, "new_hashes": [r.new_hash for r in self._records]},
                self._injector,
                "success_marker_fsync",
            )
            self._state = "success_marked"
            self._write_journal("applied_event_fsync")
            applied = self.root / "applied.json"
            _atomic_json_write(
                applied,
                {"transaction_id": self.transaction_id},
                self._injector,
                "applied_event_fsync",
            )
            self._state = "completed"
            self._write_journal("applied_event_fsync")

    def _inject(self, point: str, phase: str) -> None:
        if self._injector is not None:
            self._injector(point, phase)

    def rollback(self) -> None:
        if self._state == "rolled_back":
            return
        with ProjectLock(self.project_root, self.root.parent, self.transaction_id, self.actor):
            self._rollback_unlocked()

    def _rollback_unlocked(self) -> None:
        if self._state == "rolled_back":
            return
        # Preflight every target so an ambiguity never causes a partial rollback.
        for record in self._records:
            target = self._target(record)
            current = None if not _exists(target) else _hash_file(target)
            allowed = {record.old_hash, record.new_hash}
            if record.old_absent:
                allowed.add(None)
            if current not in allowed:
                raise RecoveryConflict()
        for record in reversed(self._records):
            target = self._target(record)
            current = None if not _exists(target) else _hash_file(target)
            if current == record.new_hash:
                if record.old_absent:
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        raise TransactionError("discovery.transaction.target_unreadable") from None
                else:
                    backup = self._artifact(record.backup_path)
                    try:
                        restored = backup.read_bytes()
                    except OSError:
                        raise TransactionError("discovery.transaction.backup_missing") from None
                    if _hash_bytes(restored) != record.old_hash:
                        raise RecoveryConflict()
                    _write_bytes(target, restored)
                    if _hash_bytes(restored) != record.old_hash:
                        raise RecoveryConflict()
                    _fsync_file(target, self._injector, "target_directory_fsync")
                _fsync_directory(target.parent, self._injector, "target_directory_fsync")
        self._state = "rolled_back"
        self._write_journal("rollback_fsync")


def recover(transaction_root: Path, *, project_root: Path) -> tuple[str, ...]:
    """Recover every pending transaction under the project-wide lock."""

    project = project_root.resolve()
    try:
        transaction_root.resolve().relative_to(project)
    except ValueError:
        raise TransactionError("discovery.transaction.path_not_contained") from None
    with ProjectLock(project, transaction_root, "recovery", "recovery"):
        return _recover_unlocked(transaction_root, project_root=project)


def _recover_unlocked(transaction_root: Path, *, project_root: Path) -> tuple[str, ...]:
    """Recover transactions while the caller owns the project lock."""

    try:
        if stat.S_ISLNK(os.lstat(transaction_root).st_mode):
            raise TransactionError("discovery.transaction.path_not_contained")
    except FileNotFoundError:
        return ()
    except OSError:
        raise TransactionError("discovery.transaction.target_unreadable") from None
    try:
        root = transaction_root.resolve()
    except OSError:
        raise TransactionError("discovery.transaction.target_unreadable") from None
    if not _exists(root):
        return ()
    recovered: list[str] = []
    directories: list[Path] = []
    try:
        children = tuple(root.iterdir())
    except OSError:
        raise TransactionError("discovery.transaction.target_unreadable") from None
    for path in children:
        if path.name.startswith("."):
            continue
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            raise TransactionError("discovery.transaction.target_unreadable") from None
        if stat.S_ISLNK(mode):
            raise TransactionError("discovery.transaction.path_not_contained")
        if stat.S_ISDIR(mode):
            directories.append(path)
    for directory in sorted(directories):
        journal_path = _safe_file(directory / "journal.json")
        if not _exists(journal_path):
            continue
        journal = ApplyJournal.open(journal_path, project_root=project_root)
        applied = _safe_file(directory / "applied.json")
        success = _safe_file(directory / "success.json")
        if journal.state == "rolled_back":
            continue
        valid_success = False
        try:
            marker = json.loads(success.read_text(encoding="utf-8")) if _exists(success) else None
            valid_success = (
                isinstance(marker, Mapping)
                and marker.get("transaction_id") == journal.transaction_id
                and marker.get("new_hashes") == [record.new_hash for record in journal.records]
                and all(_hash_file(journal._target(record)) == record.new_hash for record in journal.records)
            )
        except (OSError, ValueError, TransactionError):
            valid_success = False
        applied_valid = False
        if _exists(applied):
            try:
                applied_data = json.loads(applied.read_text(encoding="utf-8"))
                applied_valid = isinstance(applied_data, Mapping) and applied_data.get("transaction_id") == journal.transaction_id
            except (OSError, ValueError):
                applied_valid = False
        if valid_success and journal.state == "completed" and applied_valid:
            continue
        if valid_success:
            if not applied_valid:
                _atomic_json_write(applied, {"transaction_id": journal.transaction_id}, None, "applied_event_fsync")
            journal._state = "completed"
            journal._write_journal("applied_event_fsync")
        else:
            journal._rollback_unlocked()
        recovered.append(journal.transaction_id)
    return tuple(recovered)
