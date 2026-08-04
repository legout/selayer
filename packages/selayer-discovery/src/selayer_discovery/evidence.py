"""Normalized document intake, content-addressed snapshots, and evidence records.

This module owns the evidence foundation for the discovery companion package:

* :class:`EvidenceStore` — an append-only evidence manifest that ingests
  Markdown and plain text, normalizes UTF-8 content and newlines, writes
  content-addressed snapshots with exclusive creation and ``fsync``, and
  reconstructs immutable evidence records from the manifest on every load.
* :class:`EvidenceRecord` — the immutable, machine-readable view of an ingested
  source at a specific revision. It never carries a document body, so ``repr``,
  ``to_dict``, and CLI output cannot leak raw content.
* Typed selectors (:class:`DocumentLineSelector` and siblings) that bind to a
  recorded revision and reject stale revisions or out-of-range bounds.

Design rules enforced here (see the approved discovery design and global plan):

* Version 1 accepts normalized Markdown (``.md``/``.markdown``) and plain text
  (``.txt``) only.
* Preflight resolves the candidate against allowed roots, inspects the link
  itself with ``lstat`` (rejecting symbolic links and non-regular files), and
  enforces the per-document size bound **before** any byte is read. Bytes are
  then decoded as UTF-8, normalized (BOM stripped, ``CRLF``/``CR`` → ``LF``,
  ``NUL`` rejected), hashed, and written with ``O_CREAT | O_EXCL`` plus
  ``fsync`` so a snapshot is written at most once.
* Content addressing means identical content reuses one snapshot file while
  distinct source labels keep distinct records. A changed file creates a new
  revision; the prior revision stays immutable and recoverable.
* Evidence records, selectors, and diagnostics never echo a document body,
  credential, or raw value — only stable codes, validated safe identifiers, a
  safe source label, media type, normalized size, and a content hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from filelock import FileLock, Timeout

from selayer_discovery.model import MAX_TEXT_LENGTH

__all__ = [
    "CODE_EVIDENCE_INVALID_ENCODING",
    "CODE_EVIDENCE_INVALID_MEDIA",
    "CODE_EVIDENCE_INVALID_SOURCE",
    "CODE_EVIDENCE_NOT_FOUND",
    "CODE_EVIDENCE_NOT_REGULAR",
    "CODE_EVIDENCE_PATH_NOT_CONTAINED",
    "CODE_EVIDENCE_PATH_TOO_DEEP",
    "CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE",
    "CODE_EVIDENCE_SELECTOR_STALE",
    "CODE_EVIDENCE_STORE_CORRUPT",
    "CODE_EVIDENCE_TOO_LARGE",
    "CODE_EVIDENCE_TOO_MANY",
    "CODE_EVIDENCE_UNSUPPORTED_SUFFIX",
    "DEFAULT_LIMITS",
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DEFAULT_MAX_PATH_DEPTH",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_MAX_TOTAL_BYTES",
    "MEDIA_TEXT_MARKDOWN",
    "MEDIA_TEXT_PLAIN",
    "CatalogPathSelector",
    "DocumentLineSelector",
    "EvidenceError",
    "EvidenceLimits",
    "EvidenceRecord",
    "EvidenceStore",
    "InterviewEventSelector",
    "ProviderSectionSelector",
    "SourceFieldSelector",
    "VerificationOutcomeSelector",
]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Markdown media type produced for ``.md``/``.markdown`` documents.
MEDIA_TEXT_MARKDOWN: str = "text/markdown"

#: Plain-text media type produced for ``.txt`` documents.
MEDIA_TEXT_PLAIN: str = "text/plain"

#: Media types accepted for snapshot intake (extensible in later tasks).
_ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset({MEDIA_TEXT_MARKDOWN, MEDIA_TEXT_PLAIN})

#: Document suffix → media type. Only these suffixes are accepted for documents.
_DOCUMENT_SUFFIX_MEDIA: Mapping[str, str] = {
    ".md": MEDIA_TEXT_MARKDOWN,
    ".markdown": MEDIA_TEXT_MARKDOWN,
    ".txt": MEDIA_TEXT_PLAIN,
}

#: Allowed document suffixes (insertion-order stable).
_ALLOWED_DOCUMENT_SUFFIXES: tuple[str, ...] = tuple(_DOCUMENT_SUFFIX_MEDIA)

#: UTF-8 byte-order mark, stripped during normalization.
_UTF8_BOM: str = "\ufeff"

# Evidence store layout (within the Git-ignored session workspace).
_SNAPSHOTS_DIR = "snapshots"
_MANIFEST_NAME = "records.jsonl"
_LOCK_NAME = "evidence.lock"

#: Maximum length of a safe source label rendered in output and diagnostics.
_MAX_SOURCE_LABEL_LENGTH: int = 1024

#: Stable identifier shape for record ids and selector-bound ids. Mirrors the
#: session node-id grammar so a record id is safe as a single filesystem path
#: component and as a rendered safe id.
_RECORD_ID_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")
_HEX64_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")

# Evidence kind labels recorded in the manifest (constants, not enum).
_KIND_DOCUMENT: str = "document"
_KIND_SNAPSHOT: str = "snapshot"

# Selector kind labels (constants).
_KIND_DOCUMENT_LINE: str = "document_line_range"
_KIND_CATALOG_PATH: str = "catalog_json_path"
_KIND_SOURCE_FIELD: str = "source_field"
_KIND_PROVIDER_SECTION: str = "provider_section"
_KIND_INTERVIEW_EVENT: str = "interview_event"
_KIND_VERIFICATION_OUTCOME: str = "verification_outcome"

# Stable evidence diagnostic codes (rendered; never leak raw causes).
CODE_EVIDENCE_UNSUPPORTED_SUFFIX: str = "discovery.evidence.unsupported_suffix"
CODE_EVIDENCE_INVALID_MEDIA: str = "discovery.evidence.invalid_media"
CODE_EVIDENCE_INVALID_SOURCE: str = "discovery.evidence.invalid_source"
CODE_EVIDENCE_INVALID_ENCODING: str = "discovery.evidence.invalid_encoding"
CODE_EVIDENCE_PATH_NOT_CONTAINED: str = "discovery.evidence.path_not_contained"
CODE_EVIDENCE_PATH_TOO_DEEP: str = "discovery.evidence.path_too_deep"
CODE_EVIDENCE_NOT_REGULAR: str = "discovery.evidence.not_regular"
CODE_EVIDENCE_TOO_LARGE: str = "discovery.evidence.too_large"
CODE_EVIDENCE_TOO_MANY: str = "discovery.evidence.too_many"
CODE_EVIDENCE_NOT_FOUND: str = "discovery.evidence.not_found"
CODE_EVIDENCE_SELECTOR_STALE: str = "discovery.evidence.selector_stale"
CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE: str = "discovery.evidence.selector_out_of_range"
CODE_EVIDENCE_STORE_CORRUPT: str = "discovery.evidence.store_corrupt"

#: Default ``filelock`` acquisition timeout in seconds.
_DEFAULT_LOCK_TIMEOUT: float = 30.0


# --------------------------------------------------------------------------- #
# Limits                                                                      #
# --------------------------------------------------------------------------- #

#: Default per-document byte bound (2 MiB of normalized UTF-8).
DEFAULT_MAX_DOCUMENT_BYTES: int = 2 * 1024 * 1024

#: Default cumulative snapshot byte bound (128 MiB).
DEFAULT_MAX_TOTAL_BYTES: int = 128 * 1024 * 1024

#: Default maximum number of distinct evidence records (sources).
DEFAULT_MAX_RECORDS: int = 4096

#: Default maximum source-label path depth.
DEFAULT_MAX_PATH_DEPTH: int = 16


@dataclass(frozen=True, slots=True)
class EvidenceLimits:
    """Configurable bounds for evidence intake.

    All limits are enforced *before* a snapshot is written so an oversized or
    overflowing intake never reaches the filesystem.
    """

    max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_records: int = DEFAULT_MAX_RECORDS
    max_path_depth: int = DEFAULT_MAX_PATH_DEPTH

    def __post_init__(self) -> None:
        for name in ("max_document_bytes", "max_total_bytes", "max_records"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.max_path_depth) is not int or self.max_path_depth <= 0:
            raise ValueError("max_path_depth must be a positive integer")
        if self.max_total_bytes < self.max_document_bytes:
            raise ValueError("max_total_bytes must be >= max_document_bytes")


#: Process-wide default limits.
DEFAULT_LIMITS: EvidenceLimits = EvidenceLimits()


# --------------------------------------------------------------------------- #
# Sanitized evidence diagnostics                                              #
# --------------------------------------------------------------------------- #


def _safe_id(value: object) -> str:
    """Return ``value`` only if it is a stable record-id-shaped id, else ``<id>``."""

    if type(value) is str and _RECORD_ID_RE.match(value) is not None:
        return value
    return "<id>"


class EvidenceError(Exception):
    """Sanitized evidence diagnostic exception.

    Mirrors the secrecy discipline of
    :class:`selayer_discovery.session.SessionError`: only a stable ``code``, an
    optional constant ``safe_detail``, and validated ``safe_ids`` are ever
    rendered by ``__str__``, ``__repr__``, or :meth:`to_dict`. Raw causes,
    document bodies, and source values are never chained or surfaced
    (``from None`` at every raise site).
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
# Immutable evidence records                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Immutable view of an ingested source at a specific revision.

    Carries only safe, derived metadata. The document body lives in the
    content-addressed snapshot on disk and is never held on the record, so
    ``repr`` and :meth:`to_dict` cannot leak raw content.
    """

    record_id: str
    kind: str
    source: str
    media_type: str
    size: int
    content_hash: str
    revision: int
    item_count: int

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping of safe metadata (never a body)."""

        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "source": self.source,
            "media_type": self.media_type,
            "size": self.size,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "item_count": self.item_count,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"record_id={self.record_id!r}, kind={self.kind!r}, "
            f"media_type={self.media_type!r}, size={self.size!r}, "
            f"revision={self.revision!r})"
        )


# --------------------------------------------------------------------------- #
# Selectors                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DocumentLineSelector:
    """A 1-based inclusive document line range bound to a recorded revision."""

    record_id: str
    content_hash: str
    start_line: int
    end_line: int
    kind: str = _KIND_DOCUMENT_LINE

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True, slots=True)
class CatalogPathSelector:
    """A catalog JSON pointer bound to a recorded revision."""

    record_id: str
    content_hash: str
    json_path: str
    kind: str = _KIND_CATALOG_PATH

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "json_path": self.json_path,
        }


@dataclass(frozen=True, slots=True)
class SourceFieldSelector:
    """A source field bound to a recorded revision."""

    record_id: str
    content_hash: str
    field: str
    kind: str = _KIND_SOURCE_FIELD

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "field": self.field,
        }


@dataclass(frozen=True, slots=True)
class ProviderSectionSelector:
    """A provider section bound to a recorded revision."""

    record_id: str
    content_hash: str
    section: str
    kind: str = _KIND_PROVIDER_SECTION

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "section": self.section,
        }


@dataclass(frozen=True, slots=True)
class InterviewEventSelector:
    """An interview event id bound to a recorded revision."""

    record_id: str
    content_hash: str
    event_id: str
    kind: str = _KIND_INTERVIEW_EVENT

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "event_id": self.event_id,
        }


@dataclass(frozen=True, slots=True)
class VerificationOutcomeSelector:
    """A verification outcome bound to a recorded revision."""

    record_id: str
    content_hash: str
    outcome: str
    kind: str = _KIND_VERIFICATION_OUTCOME

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "outcome": self.outcome,
        }


# All selector dataclasses share record_id/content_hash/kind/to_dict; duck-typed.
_EvidenceSelector = (
    DocumentLineSelector
    | CatalogPathSelector
    | SourceFieldSelector
    | ProviderSectionSelector
    | InterviewEventSelector
    | VerificationOutcomeSelector
)

#: Concrete selector classes indexed by their persisted kind label.
_SELECTOR_KINDS: Mapping[str, type[object]] = {
    _KIND_DOCUMENT_LINE: DocumentLineSelector,
    _KIND_CATALOG_PATH: CatalogPathSelector,
    _KIND_SOURCE_FIELD: SourceFieldSelector,
    _KIND_PROVIDER_SECTION: ProviderSectionSelector,
    _KIND_INTERVIEW_EVENT: InterviewEventSelector,
    _KIND_VERIFICATION_OUTCOME: VerificationOutcomeSelector,
}

#: Every selector carries a kind-specific bounded text field; map kind → field.
_SELECTOR_TEXT_FIELD: Mapping[str, str] = {
    _KIND_CATALOG_PATH: "json_path",
    _KIND_SOURCE_FIELD: "field",
    _KIND_PROVIDER_SECTION: "section",
    _KIND_INTERVIEW_EVENT: "event_id",
    _KIND_VERIFICATION_OUTCOME: "outcome",
}


# --------------------------------------------------------------------------- #
# Normalization                                                               #
# --------------------------------------------------------------------------- #


def _normalize_text(raw: bytes) -> tuple[bytes, int]:
    """Decode, normalize, and validate UTF-8 text content.

    Returns the normalized UTF-8 bytes and the logical line count. Rejects
    invalid UTF-8, NUL bytes, and over-length single fields. Strips a leading
    UTF-8 BOM and normalizes ``CRLF``/``CR`` to ``LF``.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise EvidenceError(CODE_EVIDENCE_INVALID_ENCODING) from None
    if "\x00" in text:
        raise EvidenceError(CODE_EVIDENCE_INVALID_ENCODING) from None
    text = text.removeprefix(_UTF8_BOM)
    # Normalize newlines: CRLF and lone CR collapse to LF.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = text.encode("utf-8")
    return normalized, _line_count(text)


def _line_count(text: str) -> int:
    """Return the logical line count (LF-terminated lines plus a trailing line)."""

    if text == "":
        return 0
    count = text.count("\n")
    if not text.endswith("\n"):
        count += 1
    return count


def _content_hash(normalized: bytes) -> str:
    """Return the SHA-256 hex digest of normalized content bytes."""

    return hashlib.sha256(normalized).hexdigest()


# --------------------------------------------------------------------------- #
# Source label validation                                                     #
# --------------------------------------------------------------------------- #


def _validate_source_label(source: str, *, max_path_depth: int) -> str:
    """Validate and return a safe, bounded source label.

    A source label is rendered in output and diagnostics, so it must be a
    non-blank, bounded string without NUL or control characters. Path-shaped
    labels must stay within the configured path-depth bound and contain no
    escaping ``..`` component.
    """

    if type(source) is not str:
        raise EvidenceError(CODE_EVIDENCE_INVALID_SOURCE) from None
    if not source.strip() or len(source) > _MAX_SOURCE_LABEL_LENGTH:
        raise EvidenceError(CODE_EVIDENCE_INVALID_SOURCE) from None
    if "\x00" in source or any(ord(ch) < 0x20 for ch in source):
        raise EvidenceError(CODE_EVIDENCE_INVALID_SOURCE) from None
    if "/" in source:
        # Path-shaped label: enforce depth and reject escaping components.
        parts = [p for p in source.split("/") if p != ""]
        if any(part == ".." or part == "." for part in parts):
            raise EvidenceError(CODE_EVIDENCE_INVALID_SOURCE) from None
        if len(parts) > max_path_depth:
            raise EvidenceError(CODE_EVIDENCE_PATH_TOO_DEEP) from None
    return source


# --------------------------------------------------------------------------- #
# Reconstructed state                                                         #
# --------------------------------------------------------------------------- #


@dataclass
class _Index:
    """In-memory projection rebuilt from the manifest on every load/mutation."""

    #: Latest record per record_id (revision authority).
    latest: dict[str, EvidenceRecord] = field(default_factory=dict)
    #: Distinct content hashes present, with their normalized byte size.
    sizes: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# EvidenceStore                                                               #
# --------------------------------------------------------------------------- #


class EvidenceStore:
    """Append-only evidence manifest with content-addressed snapshots.

    The manifest (``records.jsonl``) is the single authority for evidence
    records; snapshots are content-addressed blobs under ``snapshots/``. Both
    live inside the Git-ignored session workspace. Mutations append one
    canonical JSON line, flush, and ``os.fsync`` the manifest before releasing
    the lock, and serialize through a ``filelock``.

    Construct with :meth:`create` (creates the layout if needed) or
    :meth:`open` (existing only).
    """

    def __init__(
        self,
        root: Path,
        *,
        limits: EvidenceLimits = DEFAULT_LIMITS,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._root = root
        self._limits = limits
        self._lock_timeout = lock_timeout
        self._snapshots = root / _SNAPSHOTS_DIR
        self._manifest = root / _MANIFEST_NAME
        self._lock_path = root / _LOCK_NAME
        self._lock = FileLock(str(self._lock_path))
        self._index = _Index()

    # -- construction ------------------------------------------------------- #

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        limits: EvidenceLimits = DEFAULT_LIMITS,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> EvidenceStore:
        """Create the evidence layout (idempotent) and load any manifest."""

        store = cls(root, limits=limits, lock_timeout=lock_timeout)
        store._ensure_layout()
        with store._locked():
            store._index = store._reconstruct()
        return store

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        limits: EvidenceLimits = DEFAULT_LIMITS,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> EvidenceStore:
        """Open an existing evidence store and rebuild state from its manifest."""

        store = cls(root, limits=limits, lock_timeout=lock_timeout)
        if not store._manifest.exists():
            raise EvidenceError(CODE_EVIDENCE_NOT_FOUND) from None
        with store._locked():
            store._index = store._reconstruct()
        return store

    def _ensure_layout(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._snapshots.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(self._root, 0o700)
            except OSError:
                pass

    # -- properties --------------------------------------------------------- #

    @property
    def root(self) -> Path:
        return self._root

    @property
    def limits(self) -> EvidenceLimits:
        return self._limits

    def snapshot_path(self, content_hash: str) -> Path:
        """Return the on-disk path for a content-addressed snapshot.

        Only the hash shape is validated; existence is the caller's concern.
        """

        if type(content_hash) is not str or _HEX64_RE.match(content_hash) is None:
            raise EvidenceError(CODE_EVIDENCE_NOT_FOUND) from None
        return self._snapshots / content_hash

    def total_bytes(self) -> int:
        """Return the cumulative size of all distinct snapshots."""

        return sum(self._index.sizes.values())

    def records(self) -> tuple[EvidenceRecord, ...]:
        """Return the latest revision of every evidence record, sorted by id."""

        return tuple(sorted(self._index.latest.values(), key=lambda r: r.record_id))

    def get(self, record_id: str) -> EvidenceRecord:
        """Return the latest revision of ``record_id``."""

        record = self._index.latest.get(record_id)
        if record is None:
            raise EvidenceError(
                CODE_EVIDENCE_NOT_FOUND, safe_ids=(record_id,)
            ) from None
        return record

    # -- locking ------------------------------------------------------------ #

    def _locked(self) -> _LockedContext:
        return _LockedContext(self)

    # -- intake: documents -------------------------------------------------- #

    def add_document(
        self,
        path: str | Path,
        *,
        allowed_roots: Sequence[Path],
        source: str | None = None,
    ) -> EvidenceRecord:
        """Ingest a Markdown or plain-text document with full preflight.

        Builds the absolute candidate against the first allowed root (without
        following the final symlink), inspects that link itself with ``lstat``
        first (rejecting symlinks and non-regular files), enforces the
        per-document size bound, confines the resolved path, validates the
        suffix, and finally reads, normalizes, hashes, and writes the
        content-addressed snapshot.
        """

        candidate = Path(path)
        roots = [Path(root).resolve() for root in allowed_roots]
        if not roots:
            raise EvidenceError(CODE_EVIDENCE_PATH_NOT_CONTAINED) from None
        base = roots[0]
        target = candidate if candidate.is_absolute() else base / candidate
        self._preflight_stat(target)
        containing, source_label = self._confine(target, roots, source)
        media_type = self._media_type_for_suffix(target)
        raw = self._read_file_bytes(target, containing)
        normalized, item_count = _normalize_text(raw)
        return self._record(
            normalized=normalized,
            media_type=media_type,
            source=source_label,
            kind=_KIND_DOCUMENT,
            item_count=item_count,
        )

    # -- intake: snapshots -------------------------------------------------- #

    def add_snapshot(
        self,
        raw: bytes,
        *,
        media_type: str,
        source: str,
    ) -> EvidenceRecord:
        """Store normalized text content (e.g. a provider result) as a snapshot.

        No file preflight applies (the bytes are supplied directly), but the
        content is normalized and bounded exactly like a document.
        """

        if media_type not in _ALLOWED_MEDIA_TYPES:
            raise EvidenceError(CODE_EVIDENCE_INVALID_MEDIA) from None
        source_label = _validate_source_label(source, max_path_depth=self._limits.max_path_depth)
        if not isinstance(raw, (bytes, bytearray)):
            raise EvidenceError(CODE_EVIDENCE_INVALID_ENCODING) from None
        normalized, _ = _normalize_text(bytes(raw))
        if len(normalized) > self._limits.max_document_bytes:
            raise EvidenceError(CODE_EVIDENCE_TOO_LARGE) from None
        # Snapshots carry no line semantics; item_count is left at zero.
        return self._record(
            normalized=normalized,
            media_type=media_type,
            source=source_label,
            kind=_KIND_SNAPSHOT,
            item_count=0,
        )

    # -- preflight helpers -------------------------------------------------- #

    def _confine(
        self,
        target: Path,
        roots: Sequence[Path],
        source: str | None,
    ) -> tuple[Path, str]:
        """Confine the resolved ``target`` under an allowed root.

        ``target`` is already absolute and has passed ``lstat`` preflight, so
        resolving it here cannot follow a trailing symlink. The resolved path
        must stay inside one of the allowed roots.
        """

        if not roots:
            raise EvidenceError(CODE_EVIDENCE_PATH_NOT_CONTAINED) from None
        resolved = target.resolve(strict=False)
        containing = self._containing_root(resolved, roots)
        if containing is None:
            raise EvidenceError(CODE_EVIDENCE_PATH_NOT_CONTAINED) from None
        try:
            relative = resolved.relative_to(containing)
        except ValueError:
            raise EvidenceError(CODE_EVIDENCE_PATH_NOT_CONTAINED) from None
        default_label = relative.as_posix()
        label = source if source is not None else default_label
        source_label = _validate_source_label(
            label, max_path_depth=self._limits.max_path_depth
        )
        return containing, source_label

    @staticmethod
    def _containing_root(resolved: Path, roots: Sequence[Path]) -> Path | None:
        for root in roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return root
        return None

    def _media_type_for_suffix(self, candidate: Path) -> str:
        suffix = candidate.suffix.lower()
        media_type = _DOCUMENT_SUFFIX_MEDIA.get(suffix)
        if media_type is None:
            raise EvidenceError(CODE_EVIDENCE_UNSUPPORTED_SUFFIX) from None
        return media_type

    def _preflight_stat(self, candidate: Path) -> None:
        """lstat the link itself; reject symlinks and non-regular files.

        Enforces the per-document size bound from the ``lstat`` result so an
        oversized file is rejected before any byte is read.
        """

        try:
            info = os.lstat(candidate)
        except OSError:
            raise EvidenceError(CODE_EVIDENCE_NOT_REGULAR) from None
        mode = info.st_mode
        # Reject symbolic links explicitly (lstat does not follow them).
        if stat.S_ISLNK(mode):
            raise EvidenceError(CODE_EVIDENCE_NOT_REGULAR) from None
        # Reject directories, devices, fifos, sockets — only regular files pass.
        if not stat.S_ISREG(mode):
            raise EvidenceError(CODE_EVIDENCE_NOT_REGULAR) from None
        if info.st_size > self._limits.max_document_bytes:
            raise EvidenceError(CODE_EVIDENCE_TOO_LARGE) from None

    def _read_file_bytes(self, candidate: Path, containing: Path) -> bytes:
        """Read the regular file's bytes and re-check containment defensively."""

        try:
            with candidate.open("rb") as handle:
                raw = handle.read()
        except OSError:
            raise EvidenceError(CODE_EVIDENCE_NOT_REGULAR) from None
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(containing)
        except ValueError:
            raise EvidenceError(CODE_EVIDENCE_PATH_NOT_CONTAINED) from None
        return raw

    # -- recording ---------------------------------------------------------- #

    def _record(
        self,
        *,
        normalized: bytes,
        media_type: str,
        source: str,
        kind: str,
        item_count: int,
    ) -> EvidenceRecord:
        """Write the snapshot (if new) and append the immutable record."""

        content_hash = _content_hash(normalized)
        size = len(normalized)
        record_id = self._record_id_for(kind, source)
        with self._locked():
            existing = self._index.latest.get(record_id)
            if existing is not None and existing.content_hash == content_hash:
                # Idempotent re-intake: the recorded revision already matches.
                return existing
            revision = (existing.revision + 1) if existing is not None else 1
            if existing is None and len(self._index.latest) >= self._limits.max_records:
                raise EvidenceError(
                    CODE_EVIDENCE_TOO_MANY, safe_ids=(record_id,)
                ) from None
            self._enforce_total_bytes(content_hash, size)
            self._write_snapshot(content_hash, normalized)
            record = EvidenceRecord(
                record_id=record_id,
                kind=kind,
                source=source,
                media_type=media_type,
                size=size,
                content_hash=content_hash,
                revision=revision,
                item_count=item_count,
            )
            self._append_manifest(record)
            self._index.latest[record_id] = record
            self._index.sizes[content_hash] = size
            return record

    def _record_id_for(self, kind: str, source: str) -> str:
        digest = hashlib.sha256(
            (kind + "\x00" + source).encode("utf-8")
        ).hexdigest()[:16]
        record_id = f"{kind}-{digest}"
        if _RECORD_ID_RE.match(record_id) is None:
            raise EvidenceError(CODE_EVIDENCE_INVALID_SOURCE) from None
        return record_id

    def _enforce_total_bytes(self, content_hash: str, size: int) -> None:
        if content_hash in self._index.sizes:
            return
        projected = sum(self._index.sizes.values()) + size
        if projected > self._limits.max_total_bytes:
            raise EvidenceError(CODE_EVIDENCE_TOO_LARGE) from None

    def _write_snapshot(self, content_hash: str, normalized: bytes) -> None:
        """Write a content-addressed snapshot with exclusive creation and fsync.

        If the snapshot already exists (duplicate content), it is left
        untouched — content addressing guarantees identical bytes.
        """

        target = self._snapshots / content_hash
        if target.exists():
            return
        tmp = self._snapshots / f".{content_hash}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(tmp, flags, 0o600)
        except FileExistsError:
            # A concurrent writer created the temp name; the snapshot is either
            # present now or will be after the peer finishes. Retry once against
            # the final name.
            tmp.unlink(missing_ok=True)
            if target.exists():
                return
            descriptor = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(normalized)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            tmp.unlink(missing_ok=True)
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None
        try:
            os.replace(tmp, target)
            self._fsync_directory(self._snapshots)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None

    def _append_manifest(self, record: EvidenceRecord) -> None:
        """Append one canonical JSON line, flush, and fsync the manifest."""

        line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        try:
            descriptor = os.open(self._manifest, flags, 0o600)
        except OSError:
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None
        try:
            with os.fdopen(descriptor, "a", closefd=False) as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if os.name == "posix":
            try:
                os.chmod(self._manifest, 0o600)
            except OSError:
                pass

    # -- reconstruction ----------------------------------------------------- #

    def _reconstruct(self) -> _Index:
        index = _Index()
        if not self._manifest.exists():
            return index
        with self._manifest.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.rstrip("\n")
                if line == "":
                    continue
                record = self._parse_record(line)
                index.latest[record.record_id] = record
                index.sizes[record.content_hash] = record.size
        return index

    def _parse_record(self, line: str) -> EvidenceRecord:
        try:
            obj = json.loads(line)
        except ValueError:
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None
        if not isinstance(obj, dict):
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None
        try:
            record_id = obj["record_id"]
            kind = obj["kind"]
            source = obj["source"]
            media_type = obj["media_type"]
            size = obj["size"]
            content_hash = obj["content_hash"]
            revision = obj["revision"]
            item_count = obj["item_count"]
        except KeyError:
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None
        if (
            type(record_id) is not str
            or _RECORD_ID_RE.match(record_id) is None
            or type(kind) is not str
            or kind not in (_KIND_DOCUMENT, _KIND_SNAPSHOT)
            or type(source) is not str
            or type(media_type) is not str
            or media_type not in _ALLOWED_MEDIA_TYPES
            or type(size) is not int
            or size < 0
            or type(content_hash) is not str
            or _HEX64_RE.match(content_hash) is None
            or type(revision) is not int
            or revision < 1
            or type(item_count) is not int
            or item_count < 0
        ):
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None
        return EvidenceRecord(
            record_id=record_id,
            kind=kind,
            source=source,
            media_type=media_type,
            size=size,
            content_hash=content_hash,
            revision=revision,
            item_count=item_count,
        )

    # -- selector validation ------------------------------------------------ #

    def validate_selector(self, selector: _EvidenceSelector) -> None:
        """Validate a selector against the recorded revision.

        Raises :class:`EvidenceError` (``selector_stale``) when the bound
        revision no longer matches the current revision, ``not_found`` when the
        record is absent, and ``selector_out_of_range`` for an invalid document
        line range.
        """

        if not isinstance(selector, tuple(_SELECTOR_KINDS.values())):
            raise EvidenceError(CODE_EVIDENCE_NOT_FOUND) from None
        record_id = selector.record_id
        content_hash = selector.content_hash
        if type(record_id) is not str or _RECORD_ID_RE.match(record_id) is None:
            raise EvidenceError(CODE_EVIDENCE_NOT_FOUND, safe_ids=(record_id,)) from None
        if type(content_hash) is not str or _HEX64_RE.match(content_hash) is None:
            raise EvidenceError(CODE_EVIDENCE_SELECTOR_STALE) from None
        kind = selector.kind
        expected_cls = _SELECTOR_KINDS.get(kind)
        if expected_cls is None or type(selector) is not expected_cls:
            raise EvidenceError(CODE_EVIDENCE_NOT_FOUND, safe_ids=(record_id,)) from None
        record = self._index.latest.get(record_id)
        if record is None:
            raise EvidenceError(CODE_EVIDENCE_NOT_FOUND, safe_ids=(record_id,)) from None
        if record.content_hash != content_hash:
            raise EvidenceError(
                CODE_EVIDENCE_SELECTOR_STALE, safe_ids=(record_id,)
            ) from None
        if isinstance(selector, DocumentLineSelector):
            self._validate_line_range(selector, record)
        else:
            field_name = _SELECTOR_TEXT_FIELD.get(kind)
            if field_name is not None:
                value = getattr(selector, field_name)
                if type(value) is not str or not value.strip() or len(value) > MAX_TEXT_LENGTH:
                    raise EvidenceError(
                        CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE, safe_ids=(record_id,)
                    ) from None

    @staticmethod
    def _validate_line_range(
        selector: DocumentLineSelector, record: EvidenceRecord
    ) -> None:
        start = selector.start_line
        end = selector.end_line
        if (
            type(start) is not int
            or type(end) is not int
            or start < 1
            or start > end
            or end > record.item_count
        ):
            raise EvidenceError(
                CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE, safe_ids=(record.record_id,)
            ) from None

    # -- permissions / durability ------------------------------------------- #

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist atomic snapshot renames on POSIX filesystems."""

        if os.name != "posix":
            return
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class _LockedContext:
    """Acquire the evidence lock for the duration of a ``with`` block."""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def __enter__(self) -> None:
        try:
            self._store._lock.acquire(timeout=self._store._lock_timeout)
        except Timeout:
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None
        if self._store._lock.lock_counter == 1 and os.name == "posix":
            try:
                os.chmod(self._store._lock_path, 0o600)
            except OSError:
                pass

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._store._lock.lock_counter <= 0:
            return
        self._store._lock.release()


# Silence unused-import of Iterator (reserved for future bounded reads).
_ = Iterator
