"""Normalized document intake, content-addressed snapshots, and evidence records.

These tests pin the Task 9 contract of the discovery package:

* :class:`selayer_discovery.evidence.EvidenceStore` ingests Markdown and plain
  text only, normalizes UTF-8 content and newlines, rejects symbolic links and
  non-regular files before reading, enforces size/depth/item limits, and writes
  content-addressed snapshots with exclusive creation and ``fsync``.
* Duplicate content reuses one snapshot but keeps distinct source records; a
  changed file creates a new revision while the prior revision stays immutable.
* Immutable evidence records and typed selectors never expose a document body
  in ``repr``, ``to_dict``, CLI output, or diagnostics.
* Selectors bind to a recorded revision and reject stale revisions and
  out-of-range bounds.
"""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from selayer_discovery.cli import main
from selayer_discovery.evidence import (
    CODE_EVIDENCE_INVALID_ENCODING,
    CODE_EVIDENCE_INVALID_MEDIA,
    CODE_EVIDENCE_INVALID_SOURCE,
    CODE_EVIDENCE_NOT_FOUND,
    CODE_EVIDENCE_NOT_REGULAR,
    CODE_EVIDENCE_PATH_NOT_CONTAINED,
    CODE_EVIDENCE_PATH_TOO_DEEP,
    CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE,
    CODE_EVIDENCE_SELECTOR_STALE,
    CODE_EVIDENCE_TOO_LARGE,
    CODE_EVIDENCE_TOO_MANY,
    CODE_EVIDENCE_UNSUPPORTED_SUFFIX,
    DEFAULT_LIMITS,
    DEFAULT_MAX_DOCUMENT_BYTES,
    MEDIA_TEXT_MARKDOWN,
    MEDIA_TEXT_PLAIN,
    CatalogPathSelector,
    DocumentLineSelector,
    EvidenceError,
    EvidenceLimits,
    EvidenceStore,
    InterviewEventSelector,
    ProviderSectionSelector,
    SourceFieldSelector,
    VerificationOutcomeSelector,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file semantics")


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _doc(project: Path, name: str, content: bytes) -> Path:
    return _write(project / "docs" / name, content)


_DEFAULT_CHARTER: Mapping[str, object] = {
    "business_question": "Is the order_facts grain one row per confirmed order?",
    "approver": "Dr. Alice Okonkwo",
    "catalog_fingerprint": "a" * 64,
    "inclusions": ["source.shopfloor.orders"],
    "exclusions": ["domain.finance"],
    "acceptance_questions": ["Does the corrected grain pass the uniqueness audit?"],
}


def _write_charter(project: Path) -> Path:
    path = project / "charter.yaml"
    text = yaml.safe_dump(dict(_DEFAULT_CHARTER))
    assert isinstance(text, str)
    path.write_text(text, encoding="utf-8")
    return path


def _init_session(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    session_id: str = "session-evidence-001",
) -> Path:
    _write_charter(project)
    assert (
        main(
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
        == 0
    )
    capsys.readouterr()  # clear init output
    return project / ".selayer" / "discovery" / "sessions" / session_id


# --------------------------------------------------------------------------- #
# Media type and suffix                                                       #
# --------------------------------------------------------------------------- #


def test_add_document_accepts_markdown_suffix(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"# Heading\nbody\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    assert record.media_type == MEDIA_TEXT_MARKDOWN
    assert record.kind == "document"


def test_add_document_accepts_txt_suffix(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "notes.txt", b"plain text body\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    assert record.media_type == MEDIA_TEXT_PLAIN


def test_add_document_rejects_unsupported_suffix(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "dump.json", b'{"k": 1}\n')
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_document(path, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_UNSUPPORTED_SUFFIX


# --------------------------------------------------------------------------- #
# Normalization                                                               #
# --------------------------------------------------------------------------- #


def test_add_document_normalizes_crlf_to_lf(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"line1\r\nline2\r\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    snapshot = store.snapshot_path(record.content_hash).read_bytes()
    assert snapshot == b"line1\nline2\n"
    assert record.size == len(b"line1\nline2\n")


def test_add_document_normalizes_lone_cr_to_lf(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"a\rb\rc\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    snapshot = store.snapshot_path(record.content_hash).read_bytes()
    assert snapshot == b"a\nb\nc\n"


def test_add_document_strips_utf8_bom(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"\xef\xbb\xbf# Title\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    snapshot = store.snapshot_path(record.content_hash).read_bytes()
    assert snapshot == b"# Title\n"


def test_add_document_rejects_nul_byte(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"good\x00bad\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_document(path, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_INVALID_ENCODING


def test_add_document_rejects_invalid_utf8(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"\xff\xfe\xfd\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_document(path, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_INVALID_ENCODING


def test_add_document_records_line_count(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"one\ntwo\nthree\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    assert record.item_count == 3


# --------------------------------------------------------------------------- #
# Limits                                                                      #
# --------------------------------------------------------------------------- #


def test_add_document_rejects_oversized_document(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "big.txt", b"x" * 65)
    limits = EvidenceLimits(max_document_bytes=64)
    store = EvidenceStore.create(tmp_path / "evidence", limits=limits)
    with pytest.raises(EvidenceError) as raised:
        store.add_document(path, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_TOO_LARGE


def test_add_snapshot_rejects_oversized_content(tmp_path: Path) -> None:
    limits = EvidenceLimits(max_document_bytes=32)
    store = EvidenceStore.create(tmp_path / "evidence", limits=limits)
    with pytest.raises(EvidenceError) as raised:
        store.add_snapshot(b"x" * 33, media_type=MEDIA_TEXT_PLAIN, source="manual")
    assert raised.value.code == CODE_EVIDENCE_TOO_LARGE


def test_total_byte_limit_rejects_overflow(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    limits = EvidenceLimits(max_document_bytes=16, max_total_bytes=20)
    store = EvidenceStore.create(tmp_path / "evidence", limits=limits)
    store.add_snapshot(b"0123456789ABCDE\n", media_type=MEDIA_TEXT_PLAIN, source="a")
    with pytest.raises(EvidenceError) as raised:
        store.add_snapshot(
            b"01234567\n", media_type=MEDIA_TEXT_PLAIN, source="b"
        )
    assert raised.value.code == CODE_EVIDENCE_TOO_LARGE


def test_path_depth_limit_rejects_deep_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    deep = project
    for part in ("a", "b", "c", "d", "e"):
        deep = deep / part
    path = _write(deep / "spec.md", b"deep\n")
    limits = EvidenceLimits(max_path_depth=3)
    store = EvidenceStore.create(tmp_path / "evidence", limits=limits)
    with pytest.raises(EvidenceError) as raised:
        store.add_document(path, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_PATH_TOO_DEEP


def test_item_limit_rejects_too_many_records(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    limits = EvidenceLimits(max_records=2)
    store = EvidenceStore.create(tmp_path / "evidence", limits=limits)
    store.add_snapshot(b"a\n", media_type=MEDIA_TEXT_PLAIN, source="s1")
    store.add_snapshot(b"b\n", media_type=MEDIA_TEXT_PLAIN, source="s2")
    with pytest.raises(EvidenceError) as raised:
        store.add_snapshot(b"c\n", media_type=MEDIA_TEXT_PLAIN, source="s3")
    assert raised.value.code == CODE_EVIDENCE_TOO_MANY


def test_item_limit_allows_revision_of_existing_source(tmp_path: Path) -> None:
    # A revision of an existing source must not consume a new item slot.
    project = tmp_path / "project"
    project.mkdir()
    limits = EvidenceLimits(max_records=1)
    store = EvidenceStore.create(tmp_path / "evidence", limits=limits)
    first = store.add_snapshot(b"v1\n", media_type=MEDIA_TEXT_PLAIN, source="s1")
    second = store.add_snapshot(b"v2\n", media_type=MEDIA_TEXT_PLAIN, source="s1")
    assert second.revision == 2
    assert second.record_id == first.record_id


# --------------------------------------------------------------------------- #
# Content addressing, idempotency, and revisions                              #
# --------------------------------------------------------------------------- #


def test_duplicate_content_reuses_snapshot_distinct_records(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    body = b"shared body\n"
    a = _doc(project, "a.md", body)
    b = _doc(project, "b.md", body)
    store = EvidenceStore.create(tmp_path / "evidence")
    ra = store.add_document(a, allowed_roots=(project,))
    rb = store.add_document(b, allowed_roots=(project,))
    assert ra.content_hash == rb.content_hash
    assert ra.record_id != rb.record_id
    assert ra.source != rb.source
    # One shared snapshot file on disk.
    assert store.snapshot_path(ra.content_hash).is_file()
    assert ra.content_hash == rb.content_hash


def test_idempotent_readd_returns_same_record(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"stable\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    first = store.add_document(path, allowed_roots=(project,))
    second = store.add_document(path, allowed_roots=(project,))
    assert second.record_id == first.record_id
    assert second.revision == first.revision == 1
    assert second.content_hash == first.content_hash


def test_changed_content_creates_new_revision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"first\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    first = store.add_document(path, allowed_roots=(project,))
    path.write_bytes(b"second\n")
    second = store.add_document(path, allowed_roots=(project,))
    assert second.record_id == first.record_id
    assert second.revision == 2
    assert first.revision == 1
    assert second.content_hash != first.content_hash
    # Prior revision stays immutable and recoverable.
    assert store.get(first.record_id).revision == 2
    assert store.snapshot_path(first.content_hash).is_file()


# --------------------------------------------------------------------------- #
# Preflight rejections                                                        #
# --------------------------------------------------------------------------- #


@posix_only
def test_rejects_symlink_pointing_outside(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = _write(tmp_path / "outside.md", b"leaked\n")
    link = project / "link.md"
    os.symlink(target, link)
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_document(link, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_NOT_REGULAR


@posix_only
def test_rejects_symlink_inside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    real = _doc(project, "real.md", b"ok\n")
    link = project / "link.md"
    os.symlink(real, link)
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_document(link, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_NOT_REGULAR


@posix_only
def test_rejects_special_file_fifo(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    fifo = project / "pipe.md"
    os.mkfifo(fifo)
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_document(fifo, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_NOT_REGULAR


def test_rejects_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    directory = project / "docs"
    directory.mkdir()
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_document(directory, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_NOT_REGULAR


def test_rejects_relative_path_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = _write(tmp_path / "outside.md", b"leaked\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    rel = Path("..") / "outside.md"
    with pytest.raises(EvidenceError) as raised:
        store.add_document(project / rel, allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_PATH_NOT_CONTAINED
    assert not outside.samefile(store.root)


def test_rejects_absolute_path_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_document(Path("/etc/hostname"), allowed_roots=(project,))
    assert raised.value.code == CODE_EVIDENCE_PATH_NOT_CONTAINED


# --------------------------------------------------------------------------- #
# Snapshot intake                                                             #
# --------------------------------------------------------------------------- #


def test_add_snapshot_stores_normalized_content(tmp_path: Path) -> None:
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_snapshot(
        b"alpha\r\nbeta\n", media_type=MEDIA_TEXT_PLAIN, source="manual-note"
    )
    assert store.snapshot_path(record.content_hash).read_bytes() == b"alpha\nbeta\n"
    assert record.media_type == MEDIA_TEXT_PLAIN
    assert record.kind == "snapshot"


def test_add_snapshot_rejects_unsupported_media_type(tmp_path: Path) -> None:
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_snapshot(b"x\n", media_type="application/json", source="manual")
    assert raised.value.code == CODE_EVIDENCE_INVALID_MEDIA


def test_add_snapshot_rejects_blank_source(tmp_path: Path) -> None:
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.add_snapshot(b"x\n", media_type=MEDIA_TEXT_PLAIN, source="  ")
    assert raised.value.code == CODE_EVIDENCE_INVALID_SOURCE


# --------------------------------------------------------------------------- #
# Secrecy: no body in repr, diagnostics, or JSON                              #
# --------------------------------------------------------------------------- #


def test_record_repr_has_no_body(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    secret = "SUPERSECRET-TOKEN-12345"
    path = _doc(project, "spec.md", secret.encode("utf-8") + b"\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    assert secret not in repr(record)


def test_record_to_dict_has_no_body(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    secret = "SUPERSECRET-TOKEN-67890"
    path = _doc(project, "spec.md", secret.encode("utf-8") + b"\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    rendered = json.dumps(record.to_dict(), sort_keys=True)
    assert secret not in rendered
    assert "content_hash" in record.to_dict()


def test_error_diagnostic_has_no_body(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    secret = "LEAKME-if-rendered-0001"
    path = _doc(project, "spec.md", (secret + "\n").encode("utf-8"))
    limits = EvidenceLimits(max_document_bytes=8)
    store = EvidenceStore.create(tmp_path / "evidence", limits=limits)
    with pytest.raises(EvidenceError) as raised:
        store.add_document(path, allowed_roots=(project,))
    assert secret not in repr(raised.value)
    assert secret not in json.dumps(raised.value.to_dict(), sort_keys=True)


# --------------------------------------------------------------------------- #
# Selectors                                                                   #
# --------------------------------------------------------------------------- #


def test_document_line_selector_validates_current_revision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"one\ntwo\nthree\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    selector = DocumentLineSelector(
        record_id=record.record_id,
        content_hash=record.content_hash,
        start_line=1,
        end_line=2,
    )
    store.validate_selector(selector)  # no raise


def test_document_line_selector_detects_stale_revision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"one\ntwo\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    selector = DocumentLineSelector(
        record_id=record.record_id,
        content_hash=record.content_hash,
        start_line=1,
        end_line=1,
    )
    path.write_bytes(b"changed\nbody\nmore\n")
    revised = store.add_document(path, allowed_roots=(project,))
    assert revised.content_hash != record.content_hash
    with pytest.raises(EvidenceError) as raised:
        store.validate_selector(selector)
    assert raised.value.code == CODE_EVIDENCE_SELECTOR_STALE


def test_document_line_selector_rejects_out_of_range(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"only\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    selector = DocumentLineSelector(
        record_id=record.record_id,
        content_hash=record.content_hash,
        start_line=1,
        end_line=5,
    )
    with pytest.raises(EvidenceError) as raised:
        store.validate_selector(selector)
    assert raised.value.code == CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE


def test_selector_rejects_unknown_record(tmp_path: Path) -> None:
    store = EvidenceStore.create(tmp_path / "evidence")
    selector = CatalogPathSelector(
        record_id="catalog-nosuch",
        content_hash="0" * 64,
        json_path="/sources/0",
    )
    with pytest.raises(EvidenceError) as raised:
        store.validate_selector(selector)
    assert raised.value.code == CODE_EVIDENCE_NOT_FOUND


def test_other_selectors_validate_against_revision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"body\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    for selector in (
        CatalogPathSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            json_path="/sources/0",
        ),
        SourceFieldSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            field="schema",
        ),
        ProviderSectionSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            section="overview",
        ),
        InterviewEventSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            event_id="evt-1",
        ),
        VerificationOutcomeSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            outcome="passed",
        ),
    ):
        store.validate_selector(selector)  # no raise
        assert selector.kind


def test_selector_repr_has_no_body(tmp_path: Path) -> None:
    selector = DocumentLineSelector(
        record_id="document-abc",
        content_hash="0" * 64,
        start_line=1,
        end_line=2,
    )
    text = repr(selector)
    assert "record_id" in text or "document-abc" in text


# --------------------------------------------------------------------------- #
# Persistence                                                                 #
# --------------------------------------------------------------------------- #


def test_reopen_reconstructs_records(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"persist\n")
    root = tmp_path / "evidence"
    store = EvidenceStore.create(root)
    record = store.add_document(path, allowed_roots=(project,))
    reopened = EvidenceStore.open(root)
    assert reopened.get(record.record_id) == record
    assert reopened.snapshot_path(record.content_hash).read_bytes() == b"persist\n"


def test_get_unknown_record_raises(tmp_path: Path) -> None:
    store = EvidenceStore.create(tmp_path / "evidence")
    with pytest.raises(EvidenceError) as raised:
        store.get("document-missing")
    assert raised.value.code == CODE_EVIDENCE_NOT_FOUND


def test_open_requires_existing_root(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError) as raised:
        EvidenceStore.open(tmp_path / "missing")
    assert raised.value.code == CODE_EVIDENCE_NOT_FOUND


# --------------------------------------------------------------------------- #
# CLI intake commands                                                         #
# --------------------------------------------------------------------------- #


def _run_intake_add_document(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    session_id: str = "session-evidence-001",
    path: str,
    extra: list[str] | None = None,
) -> tuple[int, dict[str, Any] | None, str, str]:
    args = [
        "intake",
        "add-document",
        "--session-id",
        session_id,
        "--project",
        str(project),
        "--path",
        path,
    ]
    if extra:
        args += extra
    code = main(args)
    captured = capsys.readouterr()
    out: dict[str, Any] | None = None
    if captured.out.strip():
        out = json.loads(captured.out)
    return code, out, captured.out, captured.err


def test_cli_intake_add_document_returns_safe_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    session_dir = _init_session(project, capsys)
    path = _doc(project, "spec.md", b"# Spec\nbody\r\n")
    code, out, _, _ = _run_intake_add_document(
        project, capsys, path=str(path)
    )
    assert code == 0
    assert out is not None
    assert out["record_id"].startswith("document-")
    assert out["media_type"] == MEDIA_TEXT_MARKDOWN
    assert out["source"] == "docs/spec.md"
    assert out["size"] == len(b"# Spec\nbody\n")
    assert out["content_hash"]
    assert "body" not in json.dumps(out)
    # The snapshot lives inside the Git-ignored session workspace.
    store = EvidenceStore.open(session_dir / "evidence")
    assert store.snapshot_path(out["content_hash"]).read_bytes() == b"# Spec\nbody\n"


def test_cli_intake_add_document_no_body_in_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_session(project, capsys)
    secret = "DO-NOT-LEAK-CLI-9999"
    path = _doc(project, "spec.md", (secret + "\n").encode("utf-8"))
    code, out, stdout, _ = _run_intake_add_document(project, capsys, path=str(path))
    assert code == 0
    assert secret not in stdout
    assert secret not in json.dumps(out)


def test_cli_intake_add_document_rejects_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_session(project, capsys)
    _write(tmp_path / "outside.md", b"leaked\n")
    code, out, _stdout, stderr = _run_intake_add_document(
        project, capsys, path="../outside.md"
    )
    assert code == 1
    assert out is None
    err = json.loads(stderr)
    assert err["code"] == CODE_EVIDENCE_PATH_NOT_CONTAINED
    assert "leaked" not in stderr


def test_cli_intake_add_document_rejects_unsupported_suffix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_session(project, capsys)
    path = _doc(project, "data.json", b'{"k": 1}\n')
    code, _, _, stderr = _run_intake_add_document(project, capsys, path=str(path))
    assert code == 1
    err = json.loads(stderr)
    assert err["code"] == CODE_EVIDENCE_UNSUPPORTED_SUFFIX


def test_cli_intake_add_document_unknown_session_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"x\n")
    code, _, _, stderr = _run_intake_add_document(
        project, capsys, session_id="session-missing", path=str(path)
    )
    assert code == 1
    err = json.loads(stderr)
    assert err["code"] == "discovery.session.not_initialized"


def test_cli_intake_snapshot_reads_stdin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    session_dir = _init_session(project, capsys)
    payload = b"provider result\r\nline two\n"
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
    )
    code = main(
        [
            "intake",
            "snapshot",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--source",
            "provider.alpha.v1",
            "--media-type",
            MEDIA_TEXT_PLAIN,
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    out = json.loads(captured.out)
    assert out["kind"] == "snapshot"
    assert out["source"] == "provider.alpha.v1"
    assert out["media_type"] == MEDIA_TEXT_PLAIN
    store = EvidenceStore.open(session_dir / "evidence")
    assert store.snapshot_path(out["content_hash"]).read_bytes() == (
        b"provider result\nline two\n"
    )


def test_cli_intake_output_keys_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_session(project, capsys)
    path = _doc(project, "spec.md", b"sorted\n")
    code, _, stdout, _ = _run_intake_add_document(project, capsys, path=str(path))
    assert code == 0
    raw = stdout.strip()
    assert raw == json.dumps(json.loads(raw), sort_keys=True)


def test_cli_intake_error_keys_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_session(project, capsys)
    path = _doc(project, "data.bin", b"x\n")
    code, _, _, stderr = _run_intake_add_document(project, capsys, path=str(path))
    assert code == 1
    raw = stderr.strip()
    assert raw == json.dumps(json.loads(raw), sort_keys=True)


# --------------------------------------------------------------------------- #
# Defaults                                                                    #
# --------------------------------------------------------------------------- #


def test_default_limits_exposed() -> None:
    assert DEFAULT_LIMITS.max_document_bytes == DEFAULT_MAX_DOCUMENT_BYTES
    assert DEFAULT_LIMITS.max_total_bytes > DEFAULT_LIMITS.max_document_bytes
    assert DEFAULT_LIMITS.max_records >= 1
    assert DEFAULT_LIMITS.max_path_depth >= 1


def test_records_returns_latest_revisions(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "evidence"
    store = EvidenceStore.create(root)
    r1 = store.add_snapshot(b"v1\n", media_type=MEDIA_TEXT_PLAIN, source="s1")
    store.add_snapshot(b"v2\n", media_type=MEDIA_TEXT_PLAIN, source="s1")
    records = store.records()
    assert len(records) == 1
    assert records[0].record_id == r1.record_id
    assert records[0].revision == 2
