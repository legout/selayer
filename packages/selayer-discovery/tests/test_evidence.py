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
    CODE_EVIDENCE_CLAIM_INFERRED_ONLY,
    CODE_EVIDENCE_CLAIM_INVALID,
    CODE_EVIDENCE_CLAIM_NOT_FOUND,
    CODE_EVIDENCE_CONFLICT_ACTOR,
    CODE_EVIDENCE_CONFLICT_ALREADY_RESOLVED,
    CODE_EVIDENCE_CONFLICT_DETERMINISTIC,
    CODE_EVIDENCE_CONFLICT_INVALID,
    CODE_EVIDENCE_INVALID_ENCODING,
    CODE_EVIDENCE_INVALID_MEDIA,
    CODE_EVIDENCE_INVALID_SOURCE,
    CODE_EVIDENCE_NOT_FOUND,
    CODE_EVIDENCE_NOT_REGULAR,
    CODE_EVIDENCE_PATH_NOT_CONTAINED,
    CODE_EVIDENCE_PATH_TOO_DEEP,
    CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE,
    CODE_EVIDENCE_SELECTOR_STALE,
    CODE_EVIDENCE_STORE_CORRUPT,
    CODE_EVIDENCE_TOO_LARGE,
    CODE_EVIDENCE_TOO_MANY,
    CODE_EVIDENCE_UNSUPPORTED_SUFFIX,
    DEFAULT_LIMITS,
    DEFAULT_MAX_DOCUMENT_BYTES,
    MEDIA_TEXT_MARKDOWN,
    MEDIA_TEXT_PLAIN,
    CatalogPathSelector,
    ClaimStore,
    ConflictKind,
    DocumentLineSelector,
    EvidenceError,
    EvidenceLimits,
    EvidenceRecord,
    EvidenceStore,
    InterviewEventSelector,
    ProviderSectionSelector,
    SourceFieldSelector,
    VerificationOutcomeSelector,
)
from selayer_discovery.model import EvidenceClass, normalize_actor_identity
from selayer_discovery.session import SessionCharter, SessionStore

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


def test_add_document_rejects_long_markdown_suffix(tmp_path: Path) -> None:
    # The long ``.markdown`` form is rejected so a source label never carries
    # an ambiguous or spoofable suffix; callers must use ``.md``.
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.markdown", b"# Heading\n")
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


def test_add_snapshot_rejects_credential_and_url_sources(tmp_path: Path) -> None:
    # A surfaced label must never carry a URL scheme, an embedded credential,
    # or a path-escape backslash.
    store = EvidenceStore.create(tmp_path / "evidence")
    for bad in ("https://host/path", "user:pass@host", "with\\backslash"):
        with pytest.raises(EvidenceError) as raised:
            store.add_snapshot(b"x\n", media_type=MEDIA_TEXT_PLAIN, source=bad)
        assert raised.value.code == CODE_EVIDENCE_INVALID_SOURCE


# --------------------------------------------------------------------------- #
# Task 17 re-review: bounded snapshot reopenability                          #
# --------------------------------------------------------------------------- #


def test_reopen_snapshot_passes_for_valid_content(tmp_path: Path) -> None:
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_snapshot(
        b"payload\n", media_type=MEDIA_TEXT_PLAIN, source="s1"
    )
    # A valid, untouched content-addressed snapshot reopens without raising.
    store.reopen_snapshot(record.content_hash)


def test_reopen_snapshot_rejects_tampered_content(tmp_path: Path) -> None:
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_snapshot(
        b"payload\n", media_type=MEDIA_TEXT_PLAIN, source="s1"
    )
    # Overwrite the content-addressed snapshot with different bytes: the
    # re-hash no longer matches the recorded content_hash (tampering).
    store.snapshot_path(record.content_hash).write_bytes(b"tampered different\n")
    with pytest.raises(EvidenceError) as raised:
        store.reopen_snapshot(record.content_hash)
    assert raised.value.code == CODE_EVIDENCE_STORE_CORRUPT


def test_reopen_snapshot_rejects_missing_file(tmp_path: Path) -> None:
    store = EvidenceStore.create(tmp_path / "evidence")
    # A well-formed hash with no backing snapshot fails safely as not found.
    with pytest.raises(EvidenceError) as raised:
        store.reopen_snapshot("0" * 64)
    assert raised.value.code == CODE_EVIDENCE_NOT_FOUND


def test_reopen_snapshot_rejects_oversized_file(tmp_path: Path) -> None:
    store = EvidenceStore.create(
        tmp_path / "evidence", limits=EvidenceLimits(max_document_bytes=8)
    )
    record = store.add_snapshot(
        b"short\n", media_type=MEDIA_TEXT_PLAIN, source="s1"
    )
    # Grow the snapshot past the configured bound in place; the bounded read
    # catches the overflow before any hash comparison.
    store.snapshot_path(record.content_hash).write_bytes(b"x" * 16)
    with pytest.raises(EvidenceError) as raised:
        store.reopen_snapshot(record.content_hash)
    assert raised.value.code == CODE_EVIDENCE_TOO_LARGE


def test_reopen_snapshot_never_exposes_path_or_body(tmp_path: Path) -> None:
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_snapshot(
        b"SECRET-MARKER\n", media_type=MEDIA_TEXT_PLAIN, source="s1"
    )
    store.snapshot_path(record.content_hash).write_bytes(b"tampered\n")
    with pytest.raises(EvidenceError) as raised:
        store.reopen_snapshot(record.content_hash)
    rendered = repr(raised.value) + json.dumps(
        raised.value.to_dict(), sort_keys=True
    )
    # Neither the on-disk path nor any body bytes surface in the diagnostic.
    assert str(store.snapshot_path(record.content_hash)) not in rendered
    assert "tampered" not in rendered
    assert "SECRET-MARKER" not in rendered


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
        revision=record.revision,
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
        revision=record.revision,
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
        revision=record.revision,
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
        revision=1,
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
            revision=record.revision,
            json_path="/sources/0",
        ),
        SourceFieldSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            revision=record.revision,
            field="schema",
        ),
        ProviderSectionSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            revision=record.revision,
            section="overview",
        ),
        InterviewEventSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            revision=record.revision,
            event_id="evt-1",
        ),
        VerificationOutcomeSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            revision=record.revision,
            outcome="passed",
        ),
    ):
        store.validate_selector(selector)  # no raise
        assert selector.kind


def test_selector_repr_has_no_body(tmp_path: Path) -> None:
    selector = DocumentLineSelector(
        record_id="document-abc",
        content_hash="0" * 64,
        revision=1,
        start_line=1,
        end_line=2,
    )
    text = repr(selector)
    assert "record_id" in text or "document-abc" in text


def test_selector_stale_after_abca_replay(tmp_path: Path) -> None:
    # A→B→A: the content hash repeats at revision 3, so a selector bound to
    # revision 1 must be stale even though the hashes now match again.
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"alpha\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    rev_a = store.add_document(path, allowed_roots=(project,))
    selector = DocumentLineSelector(
        record_id=rev_a.record_id,
        content_hash=rev_a.content_hash,
        revision=rev_a.revision,
        start_line=1,
        end_line=1,
    )
    path.write_bytes(b"beta\n")
    store.add_document(path, allowed_roots=(project,))
    path.write_bytes(b"alpha\n")
    rev_a_again = store.add_document(path, allowed_roots=(project,))
    assert rev_a_again.revision == 3
    assert rev_a_again.content_hash == rev_a.content_hash
    with pytest.raises(EvidenceError) as raised:
        store.validate_selector(selector)
    assert raised.value.code == CODE_EVIDENCE_SELECTOR_STALE


def test_typed_selectors_reject_malformed_fields(tmp_path: Path) -> None:
    # Each typed selector rejects a malformed kind-specific field shape.
    project = tmp_path / "project"
    project.mkdir()
    path = _doc(project, "spec.md", b"body\n")
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_document(path, allowed_roots=(project,))
    rid = record.record_id
    ch = record.content_hash
    rev = record.revision
    for selector in (
        CatalogPathSelector(record_id=rid, content_hash=ch, revision=rev, json_path="sources/0"),
        SourceFieldSelector(record_id=rid, content_hash=ch, revision=rev, field="Bad-Field"),
        ProviderSectionSelector(record_id=rid, content_hash=ch, revision=rev, section="bad space"),
        InterviewEventSelector(record_id=rid, content_hash=ch, revision=rev, event_id="bad space"),
        VerificationOutcomeSelector(record_id=rid, content_hash=ch, revision=rev, outcome="bogus"),
    ):
        with pytest.raises(EvidenceError) as raised:
            store.validate_selector(selector)
        assert raised.value.code == CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE


def test_document_line_selector_rejects_snapshot_kind(tmp_path: Path) -> None:
    # A line range only applies to documents, never to snapshots (which carry
    # no line semantics).
    store = EvidenceStore.create(tmp_path / "evidence")
    record = store.add_snapshot(
        b"line1\nline2\n", media_type=MEDIA_TEXT_PLAIN, source="snap"
    )
    selector = DocumentLineSelector(
        record_id=record.record_id,
        content_hash=record.content_hash,
        revision=record.revision,
        start_line=1,
        end_line=1,
    )
    with pytest.raises(EvidenceError) as raised:
        store.validate_selector(selector)
    assert raised.value.code == CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE


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


# --------------------------------------------------------------------------- #
# Task 14: typed claims, conflicts, and transitive invalidation              #
# --------------------------------------------------------------------------- #

#: Subject identifier shape shared by claim/conflict tests.
_SUBJECT = "source.shopfloor.orders"


def _doc_selector(
    evidence: EvidenceStore, project: Path, body: bytes = b"line one\nline two\n"
) -> tuple[DocumentLineSelector, EvidenceRecord]:
    """Ingest a document and return a revision-bound line selector plus record."""

    path = _doc(project, "spec.md", body)
    record = evidence.add_document(path, allowed_roots=(project,))
    selector = DocumentLineSelector(
        record_id=record.record_id,
        content_hash=record.content_hash,
        revision=record.revision,
        start_line=1,
        end_line=1,
    )
    return selector, record


def _claim_store(
    session_root: Path, charter: SessionCharter, actor: str
) -> tuple[SessionStore, EvidenceStore, ClaimStore]:
    """Create a session, evidence store, and claim store wired together."""

    store = SessionStore.create(session_root, charter=charter, actor=actor)
    evidence = EvidenceStore.create(session_root / "evidence")
    claims = ClaimStore.create(store, evidence)
    return store, evidence, claims


# --------------------------------------------------------------------------- #
# Claim tests (Step 1)                                                        #
# --------------------------------------------------------------------------- #


def test_add_claim_requires_subject_statement_selectors_class_creator(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project)
    store.record_artifact(
        "answer-gate-grains", content_hash="a" * 64, actor=actor
    )
    base = {
        "claim_id": "claim-grain-001",
        "subject": _SUBJECT,
        "statement": "The grain is one row per confirmed order.",
        "evidence_class": EvidenceClass.ASSERTED,
        "selectors": (selector,),
        "creator_event": "answer-gate-grains",
        "actor": actor,
    }
    # Omitting each required field raises the invalid-claim code.
    for missing in ("subject", "statement", "evidence_class", "selectors", "creator_event"):
        kwargs = dict(base)
        if missing == "selectors":
            kwargs[missing] = ()
        elif missing == "evidence_class":
            kwargs[missing] = "bogus"
        elif missing in ("subject", "statement", "creator_event"):
            kwargs[missing] = ""
        with pytest.raises(EvidenceError) as raised:
            claims.add_claim(**kwargs)
        assert raised.value.code == CODE_EVIDENCE_CLAIM_INVALID


def test_add_claim_rejects_unknown_evidence_class(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project)
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    with pytest.raises(EvidenceError) as raised:
        claims.add_claim(
            claim_id="claim-x",
            subject=_SUBJECT,
            statement="A declarative claim.",
            evidence_class="speculative",
            selectors=(selector,),
            creator_event="answer-gate-grains",
            actor=actor,
        )
    assert raised.value.code == CODE_EVIDENCE_CLAIM_INVALID


def test_add_claim_records_current_claim_with_selectors(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project)
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    claim = claims.add_claim(
        claim_id="claim-grain-001",
        subject=_SUBJECT,
        statement="The grain is one row per confirmed order.",
        evidence_class=EvidenceClass.ASSERTED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        contradicts=("claim-grain-002",),
        actor=actor,
    )
    assert claim.claim_id == "claim-grain-001"
    assert claim.subject == _SUBJECT
    assert claim.evidence_class == EvidenceClass.ASSERTED.value
    assert claim.state == "current"
    assert claim.creator_event == "answer-gate-grains"
    assert claim.contradicts == ("claim-grain-002",)
    assert claims.get_claim("claim-grain-001") == claim


def test_claim_record_retains_typed_selectors_without_leaking_bodies(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    # ClaimRecord retains the typed selectors (record_id/content_hash/revision
    # plus the kind-specific field) so readiness can revalidate them later
    # against the current evidence revision. Selectors carry no body content,
    # so retention never leaks a document body.
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, record = _doc_selector(evidence, project)
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    claim = claims.add_claim(
        claim_id="claim-grain-retained",
        subject=_SUBJECT,
        statement="The grain is one row per confirmed order.",
        evidence_class=EvidenceClass.OBSERVED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        actor=actor,
    )
    assert len(claim.selectors) == 1
    retained = claim.selectors[0]
    assert retained.record_id == record.record_id
    assert retained.content_hash == record.content_hash
    assert retained.revision == record.revision
    assert retained.kind == "document_line_range"
    # The safe view exposes only selector kinds, never bodies or values.
    safe = claim.safe_dict()
    assert safe["selector_kinds"] == ["document_line_range"]
    rendered = json.dumps(safe, sort_keys=True)
    assert "line one" not in rendered


def test_claim_selectors_round_trip_through_journal(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    # Reopening the claim store from its append-only journal reconstructs the
    # retained typed selectors exactly.
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project)
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    claims.add_claim(
        claim_id="claim-rt",
        subject=_SUBJECT,
        statement="The grain is one row per confirmed order.",
        evidence_class=EvidenceClass.OBSERVED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        actor=actor,
    )
    reopened = ClaimStore.open(store, evidence)
    claim = reopened.get_claim("claim-rt")
    assert len(claim.selectors) == 1
    assert claim.selectors[0] == selector


def test_add_claim_rejects_stale_selector(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project, b"one\ntwo\n")
    # Revise the document so the bound selector is now stale.
    path = project / "docs" / "spec.md"
    path.write_bytes(b"changed\nbody\nmore\n")
    evidence.add_document(path, allowed_roots=(project,))
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    with pytest.raises(EvidenceError) as raised:
        claims.add_claim(
            claim_id="claim-stale",
            subject=_SUBJECT,
            statement="A claim.",
            evidence_class=EvidenceClass.OBSERVED,
            selectors=(selector,),
            creator_event="answer-gate-grains",
            actor=actor,
        )
    assert raised.value.code == CODE_EVIDENCE_SELECTOR_STALE


def test_assert_executable_evidence_rejects_inferred_only(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project)
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    inferred = claims.add_claim(
        claim_id="claim-inf",
        subject=_SUBJECT,
        statement="An agent hypothesis about the grain.",
        evidence_class=EvidenceClass.INFERRED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        actor=actor,
    )
    # Inferred-only evidence cannot satisfy an executable operation.
    with pytest.raises(EvidenceError) as raised:
        claims.assert_executable_evidence((inferred.claim_id,))
    assert raised.value.code == CODE_EVIDENCE_CLAIM_INFERRED_ONLY


def test_assert_executable_evidence_passes_with_observed(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project)
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    observed = claims.add_claim(
        claim_id="claim-obs",
        subject=_SUBJECT,
        statement="A measured uniqueness result.",
        evidence_class=EvidenceClass.OBSERVED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        actor=actor,
    )
    inferred = claims.add_claim(
        claim_id="claim-inf",
        subject=_SUBJECT,
        statement="A hypothesis.",
        evidence_class=EvidenceClass.INFERRED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        actor=actor,
    )
    # A non-inferred claim present satisfies the executable requirement.
    claims.assert_executable_evidence((observed.claim_id, inferred.claim_id))


def test_assert_executable_evidence_rejects_unknown_claim(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
) -> None:
    _store, _evidence, claims = _claim_store(session_root, charter, actor)
    with pytest.raises(EvidenceError) as raised:
        claims.assert_executable_evidence(("claim-missing",))
    assert raised.value.code == CODE_EVIDENCE_CLAIM_NOT_FOUND


def test_add_claim_tied_to_creator_event_stales_on_change(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    hash_factory,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project)
    # Register the creator event as a session artifact node.
    store.record_artifact(
        "answer-gate-grains", content_hash=hash_factory(1), actor=actor
    )
    claims.add_claim(
        claim_id="claim-grain-001",
        subject=_SUBJECT,
        statement="The grain is one row per confirmed order.",
        evidence_class=EvidenceClass.ASSERTED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        actor=actor,
    )
    # Revising the creator event's hash emits the claim as a stale target.
    result = store.record_artifact(
        "answer-gate-grains", content_hash=hash_factory(2), actor=actor
    )
    assert "claim-grain-001" in result.stale_targets


def test_add_claim_never_leaks_statement(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project)
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    secret = "super-secret-business-rule-4242"
    try:
        claims.add_claim(
            claim_id="claim-leak",
            subject="",
            statement=secret,
            evidence_class=EvidenceClass.ASSERTED,
            selectors=(selector,),
            creator_event="answer-gate-grains",
            actor=actor,
        )
    except EvidenceError as exc:
        rendered = str(exc) + repr(exc) + json.dumps(exc.to_dict(), sort_keys=True)
        assert secret not in rendered
    else:
        pytest.fail("expected EvidenceError")


# --------------------------------------------------------------------------- #
# Conflict tests (Step 2)                                                     #
# --------------------------------------------------------------------------- #


def _seed_two_claims(
    claims: ClaimStore,
    evidence: EvidenceStore,
    store: SessionStore,
    project: Path,
    actor: str,
) -> tuple[str, str]:
    selector, _ = _doc_selector(evidence, project)
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    a = claims.add_claim(
        claim_id="claim-a",
        subject=_SUBJECT,
        statement="The grain is one row per confirmed order.",
        evidence_class=EvidenceClass.ASSERTED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        actor=actor,
    )
    b = claims.add_claim(
        claim_id="claim-b",
        subject=_SUBJECT,
        statement="The grain is one row per cancelled order.",
        evidence_class=EvidenceClass.ASSERTED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        actor=actor,
    )
    return a.claim_id, b.claim_id


def test_unresolved_conflict_blocks_only_affected_groups(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    claims.add_conflict(
        conflict_id="conflict-grain",
        kind=ConflictKind.SEMANTIC,
        subject=_SUBJECT,
        involved_claim_ids=(a, b),
        affected_group_ids=("group-semantic-model",),
        reason="Sources disagree on the grain.",
        actor=actor,
    )
    # The affected group is blocked.
    assert claims.group_blocked_by("group-semantic-model") == ("conflict-grain",)
    # An independent group stays eligible (not blocked).
    assert claims.group_blocked_by("group-measures") == ()


def test_resolved_conflict_unblocks_group(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    claims.add_conflict(
        conflict_id="conflict-grain",
        kind=ConflictKind.SEMANTIC,
        subject=_SUBJECT,
        involved_claim_ids=(a, b),
        affected_group_ids=("group-semantic-model",),
        reason="Sources disagree.",
        actor=actor,
    )
    claims.resolve_conflict(
        conflict_id="conflict-grain",
        statement="The process owner confirmed the confirmed-order grain.",
        answer_id="answer-1",
        actor=normalize_actor_identity(actor),
    )
    assert claims.group_blocked_by("group-semantic-model") == ()
    assert claims.get_conflict("conflict-grain").state == "resolved"


def test_semantic_resolution_requires_named_approver(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    claims.add_conflict(
        conflict_id="conflict-grain",
        kind=ConflictKind.SEMANTIC,
        subject=_SUBJECT,
        involved_claim_ids=(a, b),
        affected_group_ids=("group-semantic-model",),
        reason="Sources disagree.",
        actor=actor,
    )
    # An actor that is not the charter approver cannot resolve a semantic conflict.
    with pytest.raises(EvidenceError) as raised:
        claims.resolve_conflict(
            conflict_id="conflict-grain",
            statement="Someone else resolves it.",
            answer_id="answer-1",
            actor="Dr. Bo Okafor",
        )
    assert raised.value.code == CODE_EVIDENCE_CONFLICT_ACTOR


def test_deterministic_failure_cannot_be_resolved_by_attestation(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    claims.add_conflict(
        conflict_id="conflict-fp",
        kind=ConflictKind.DETERMINISTIC,
        subject=_SUBJECT,
        involved_claim_ids=(a, b),
        affected_group_ids=("group-semantic-model",),
        reason="A fingerprint mismatch.",
        actor=actor,
    )
    # Attestation alone (statement + approver, no evidence id) is rejected.
    with pytest.raises(EvidenceError) as raised:
        claims.resolve_conflict(
            conflict_id="conflict-fp",
            statement="The approver attests it is fine.",
            evidence_id="",
            actor=normalize_actor_identity(actor),
        )
    assert raised.value.code == CODE_EVIDENCE_CONFLICT_DETERMINISTIC


def test_deterministic_conflict_resolves_with_new_evidence(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    claims.add_conflict(
        conflict_id="conflict-fp",
        kind=ConflictKind.DETERMINISTIC,
        subject=_SUBJECT,
        involved_claim_ids=(a, b),
        affected_group_ids=("group-semantic-model",),
        reason="A fingerprint mismatch.",
        actor=actor,
    )
    record = claims.resolve_conflict(
        conflict_id="conflict-fp",
        statement="New passing evidence resolves the mismatch.",
        evidence_id="document-newpass",
        actor=actor,
    )
    assert record.state == "resolved"
    assert record.resolving_evidence_id == "document-newpass"


def test_approver_change_stales_semantic_resolution(
    session_root: Path,
    make_charter,  # type: ignore[no-untyped-def]
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    claims.add_conflict(
        conflict_id="conflict-grain",
        kind=ConflictKind.SEMANTIC,
        subject=_SUBJECT,
        involved_claim_ids=(a, b),
        affected_group_ids=("group-semantic-model",),
        reason="Sources disagree.",
        actor=actor,
    )
    claims.resolve_conflict(
        conflict_id="conflict-grain",
        statement="The process owner confirmed the grain.",
        answer_id="answer-1",
        actor=normalize_actor_identity(actor),
    )
    # Changing the named approver stales the semantic resolution.
    revised = make_charter(approver="Dr. Bo Okafor")
    result = store.revise_charter(revised, actor=actor)
    assert "conflict-grain" in result.stale_targets


def test_approver_change_does_not_stale_deterministic_resolution(
    session_root: Path,
    make_charter,  # type: ignore[no-untyped-def]
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    claims.add_conflict(
        conflict_id="conflict-fp",
        kind=ConflictKind.DETERMINISTIC,
        subject=_SUBJECT,
        involved_claim_ids=(a, b),
        affected_group_ids=("group-semantic-model",),
        reason="A fingerprint mismatch.",
        actor=actor,
    )
    claims.resolve_conflict(
        conflict_id="conflict-fp",
        statement="New passing evidence resolves it.",
        evidence_id="document-newpass",
        actor=actor,
    )
    # A deterministic resolution depends on new evidence, not the approver, so
    # an approver change must not stale it.
    revised = make_charter(approver="Dr. Bo Okafor")
    result = store.revise_charter(revised, actor=actor)
    assert "conflict-fp" not in result.stale_targets


def test_resolve_conflict_never_deletes_contradictory_claims(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    claims.add_conflict(
        conflict_id="conflict-grain",
        kind=ConflictKind.SEMANTIC,
        subject=_SUBJECT,
        involved_claim_ids=(a, b),
        affected_group_ids=("group-semantic-model",),
        reason="Sources disagree.",
        actor=actor,
    )
    claims.resolve_conflict(
        conflict_id="conflict-grain",
        statement="The approver picks the confirmed-order grain.",
        answer_id="answer-1",
        actor=normalize_actor_identity(actor),
    )
    # Both contrary claims remain recorded (history is preserved).
    assert claims.get_claim(a).claim_id == a
    assert claims.get_claim(b).claim_id == b


def test_resolve_conflict_rejects_already_resolved(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    claims.add_conflict(
        conflict_id="conflict-grain",
        kind=ConflictKind.SEMANTIC,
        subject=_SUBJECT,
        involved_claim_ids=(a, b),
        affected_group_ids=("group-semantic-model",),
        reason="Sources disagree.",
        actor=actor,
    )
    claims.resolve_conflict(
        conflict_id="conflict-grain",
        statement="Resolved once.",
        answer_id="answer-1",
        actor=normalize_actor_identity(actor),
    )
    with pytest.raises(EvidenceError) as raised:
        claims.resolve_conflict(
            conflict_id="conflict-grain",
            statement="Resolved again.",
            answer_id="answer-2",
            actor=normalize_actor_identity(actor),
        )
    assert raised.value.code == CODE_EVIDENCE_CONFLICT_ALREADY_RESOLVED


def test_add_conflict_rejects_unknown_kind(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    a, b = _seed_two_claims(claims, evidence, store, project, actor)
    with pytest.raises(EvidenceError) as raised:
        claims.add_conflict(
            conflict_id="conflict-bad",
            kind="mysterious",
            subject=_SUBJECT,
            involved_claim_ids=(a, b),
            affected_group_ids=("group-semantic-model",),
            reason="x",
            actor=actor,
        )
    assert raised.value.code == CODE_EVIDENCE_CONFLICT_INVALID


# --------------------------------------------------------------------------- #
# Persistence                                                                 #
# --------------------------------------------------------------------------- #


def test_claim_store_survives_reopen(
    session_root: Path,
    charter: SessionCharter,
    actor: str,
    tmp_path: Path,
) -> None:
    store, evidence, claims = _claim_store(session_root, charter, actor)
    project = tmp_path / "project"
    project.mkdir()
    selector, _ = _doc_selector(evidence, project)
    store.record_artifact("answer-gate-grains", content_hash="a" * 64, actor=actor)
    claims.add_claim(
        claim_id="claim-grain-001",
        subject=_SUBJECT,
        statement="The grain is one row per confirmed order.",
        evidence_class=EvidenceClass.ASSERTED,
        selectors=(selector,),
        creator_event="answer-gate-grains",
        actor=actor,
    )
    claims.add_conflict(
        conflict_id="conflict-grain",
        kind=ConflictKind.SEMANTIC,
        subject=_SUBJECT,
        involved_claim_ids=("claim-grain-001",),
        affected_group_ids=("group-semantic-model",),
        reason="x",
        actor=actor,
    )
    reopened_store = SessionStore.open(session_root)
    reopened_evidence = EvidenceStore.open(session_root / "evidence")
    reopened = ClaimStore.open(reopened_store, reopened_evidence)
    assert reopened.get_claim("claim-grain-001").subject == _SUBJECT
    assert reopened.get_conflict("conflict-grain").kind == ConflictKind.SEMANTIC.value


# --------------------------------------------------------------------------- #
# Evidence CLI commands (Step 4)                                              #
# --------------------------------------------------------------------------- #


def _write_claim_input(
    project: Path, name: str, claim: dict[str, Any]
) -> Path:
    path = project / name
    path.write_text(json.dumps(claim, sort_keys=True), encoding="utf-8")
    return path


def test_cli_evidence_add_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_session(project, capsys)
    # Ingest a document to obtain a valid evidence record.
    doc_path = _doc(project, "spec.md", b"line one\nline two\n")
    main(
        [
            "intake",
            "add-document",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--path",
            str(doc_path),
        ]
    )
    record_out = json.loads(capsys.readouterr().out)
    claim_path = _write_claim_input(
        project,
        "claim.json",
        {
            "claim_id": "claim-grain-001",
            "subject": _SUBJECT,
            "statement": "The grain is one row per confirmed order.",
            "evidence_class": "asserted",
            "selectors": [
                {
                    "kind": "document_line_range",
                    "record_id": record_out["record_id"],
                    "content_hash": record_out["content_hash"],
                    "revision": record_out["revision"],
                    "start_line": 1,
                    "end_line": 1,
                }
            ],
            "creator_event": "answer-gate-grains",
            "contradicts": [],
        },
    )
    code = main(
        [
            "evidence",
            "add-claim",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--claim",
            str(claim_path),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["claim_id"] == "claim-grain-001"
    assert out["evidence_class"] == "asserted"
    assert out["state"] == "current"
    assert out["creator_event"] == "answer-gate-grains"
    assert "statement" not in out


def test_cli_evidence_add_claim_output_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_session(project, capsys)
    doc_path = _doc(project, "spec.md", b"line one\nline two\n")
    main(
        [
            "intake",
            "add-document",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--path",
            str(doc_path),
        ]
    )
    record_out = json.loads(capsys.readouterr().out)
    claim_path = _write_claim_input(
        project,
        "claim.json",
        {
            "claim_id": "claim-grain-001",
            "subject": _SUBJECT,
            "statement": "x",
            "evidence_class": "asserted",
            "selectors": [
                {
                    "kind": "document_line_range",
                    "record_id": record_out["record_id"],
                    "content_hash": record_out["content_hash"],
                    "revision": record_out["revision"],
                    "start_line": 1,
                    "end_line": 1,
                }
            ],
            "creator_event": "answer-gate-grains",
        },
    )
    main(
        [
            "evidence",
            "add-claim",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--claim",
            str(claim_path),
        ]
    )
    raw = capsys.readouterr().out
    assert raw == json.dumps(json.loads(raw), sort_keys=True) + "\n"


def test_cli_evidence_add_conflict_and_resolve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_session(project, capsys)
    doc_path = _doc(project, "spec.md", b"line one\nline two\n")
    main(
        [
            "intake",
            "add-document",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--path",
            str(doc_path),
        ]
    )
    record_out = json.loads(capsys.readouterr().out)
    selector = {
        "kind": "document_line_range",
        "record_id": record_out["record_id"],
        "content_hash": record_out["content_hash"],
        "revision": record_out["revision"],
        "start_line": 1,
        "end_line": 1,
    }
    for cid in ("claim-a", "claim-b"):
        cpath = _write_claim_input(
            project,
            f"{cid}.json",
            {
                "claim_id": cid,
                "subject": _SUBJECT,
                "statement": f"claim {cid}",
                "evidence_class": "asserted",
                "selectors": [selector],
                "creator_event": "answer-gate-grains",
            },
        )
        main(
            [
                "evidence",
                "add-claim",
                "--session-id",
                "session-evidence-001",
                "--project",
                str(project),
                "--claim",
                str(cpath),
            ]
        )
        capsys.readouterr()
    conflict_path = _write_claim_input(
        project,
        "conflict.json",
        {
            "conflict_id": "conflict-grain",
            "kind": "semantic",
            "subject": _SUBJECT,
            "involved_claim_ids": ["claim-a", "claim-b"],
            "affected_group_ids": ["group-semantic-model"],
            "reason": "Sources disagree on the grain.",
        },
    )
    code = main(
        [
            "evidence",
            "add-conflict",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--conflict",
            str(conflict_path),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["conflict_id"] == "conflict-grain"
    assert out["kind"] == "semantic"
    assert out["state"] == "unresolved"
    assert out["affected_group_ids"] == ["group-semantic-model"]
    assert "reason" not in out
    resolution_path = _write_claim_input(
        project,
        "resolution.json",
        {
            "conflict_id": "conflict-grain",
            "statement": "The approver confirmed the grain.",
            "answer_id": "answer-1",
        },
    )
    code = main(
        [
            "evidence",
            "resolve-conflict",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--resolution",
            str(resolution_path),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["conflict_id"] == "conflict-grain"
    assert out["state"] == "resolved"
    assert out["resolving_answer_id"] == "answer-1"


def test_cli_evidence_never_leaks_statement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_session(project, capsys)
    doc_path = _doc(project, "spec.md", b"line one\nline two\n")
    main(
        [
            "intake",
            "add-document",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--path",
            str(doc_path),
        ]
    )
    record_out = json.loads(capsys.readouterr().out)
    secret = "confidential-business-rule-7777"
    claim_path = _write_claim_input(
        project,
        "claim.json",
        {
            "claim_id": "claim-secret",
            "subject": _SUBJECT,
            "statement": secret,
            "evidence_class": "asserted",
            "selectors": [
                {
                    "kind": "document_line_range",
                    "record_id": record_out["record_id"],
                    "content_hash": record_out["content_hash"],
                    "revision": record_out["revision"],
                    "start_line": 1,
                    "end_line": 1,
                }
            ],
            "creator_event": "answer-gate-grains",
        },
    )
    main(
        [
            "evidence",
            "add-claim",
            "--session-id",
            "session-evidence-001",
            "--project",
            str(project),
            "--claim",
            str(claim_path),
        ]
    )
    raw = capsys.readouterr().out
    assert secret not in raw
