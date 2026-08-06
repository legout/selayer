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

* Version 1 accepts normalized Markdown (``.md``) and plain text (``.txt``)
  only.
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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from filelock import FileLock, Timeout

from selayer_discovery import canonical
from selayer_discovery.model import (
    MAX_TEXT_LENGTH,
    EvidenceClass,
    normalize_actor_identity,
)

if TYPE_CHECKING:
    from selayer_discovery.session import SessionStore

__all__ = [
    "CODE_EVIDENCE_CLAIM_INFERRED_ONLY",
    "CODE_EVIDENCE_CLAIM_INVALID",
    "CODE_EVIDENCE_CLAIM_NOT_FOUND",
    "CODE_EVIDENCE_CLAIM_NOT_INITIALIZED",
    "CODE_EVIDENCE_CLAIM_STORE_CORRUPT",
    "CODE_EVIDENCE_CONFLICT_ACTOR",
    "CODE_EVIDENCE_CONFLICT_ALREADY_RESOLVED",
    "CODE_EVIDENCE_CONFLICT_DETERMINISTIC",
    "CODE_EVIDENCE_CONFLICT_INVALID",
    "CODE_EVIDENCE_CONFLICT_NOT_FOUND",
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
    "ClaimRecord",
    "ClaimStore",
    "ConflictKind",
    "ConflictRecord",
    "DocumentLineSelector",
    "EvidenceError",
    "EvidenceLimits",
    "EvidenceRecord",
    "EvidenceStore",
    "InterviewEventSelector",
    "ProviderSectionSelector",
    "SourceFieldSelector",
    "VerificationOutcomeSelector",
    "selector_from_mapping",
]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Markdown media type produced for ``.md`` documents.
MEDIA_TEXT_MARKDOWN: str = "text/markdown"

#: Plain-text media type produced for ``.txt`` documents.
MEDIA_TEXT_PLAIN: str = "text/plain"

#: Media types accepted for snapshot intake (extensible in later tasks).
_ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {MEDIA_TEXT_MARKDOWN, MEDIA_TEXT_PLAIN}
)

#: Document suffix → media type. Only these suffixes are accepted for documents.
#: The long ``.markdown`` form is intentionally rejected; callers must use
#: ``.md`` so a source label never carries an ambiguous or spoofable suffix.
_DOCUMENT_SUFFIX_MEDIA: Mapping[str, str] = {
    ".md": MEDIA_TEXT_MARKDOWN,
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

# Stable claim/conflict diagnostic codes (rendered; never leak raw causes).
CODE_EVIDENCE_CLAIM_INVALID: str = "discovery.evidence.claim_invalid"
CODE_EVIDENCE_CLAIM_NOT_FOUND: str = "discovery.evidence.claim_not_found"
CODE_EVIDENCE_CLAIM_INFERRED_ONLY: str = "discovery.evidence.claim_inferred_only"
CODE_EVIDENCE_CLAIM_NOT_INITIALIZED: str = "discovery.evidence.claim_not_initialized"
CODE_EVIDENCE_CLAIM_STORE_CORRUPT: str = "discovery.evidence.claim_store_corrupt"
CODE_EVIDENCE_CONFLICT_INVALID: str = "discovery.evidence.conflict_invalid"
CODE_EVIDENCE_CONFLICT_NOT_FOUND: str = "discovery.evidence.conflict_not_found"
CODE_EVIDENCE_CONFLICT_ALREADY_RESOLVED: str = (
    "discovery.evidence.conflict_already_resolved"
)
CODE_EVIDENCE_CONFLICT_DETERMINISTIC: str = "discovery.evidence.conflict_deterministic"
CODE_EVIDENCE_CONFLICT_ACTOR: str = "discovery.evidence.conflict_actor"

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
    revision: int
    start_line: int
    end_line: int
    kind: str = _KIND_DOCUMENT_LINE

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True, slots=True)
class CatalogPathSelector:
    """A catalog JSON pointer bound to a recorded revision."""

    record_id: str
    content_hash: str
    revision: int
    json_path: str
    kind: str = _KIND_CATALOG_PATH

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "json_path": self.json_path,
        }


@dataclass(frozen=True, slots=True)
class SourceFieldSelector:
    """A source field bound to a recorded revision."""

    record_id: str
    content_hash: str
    revision: int
    field: str
    kind: str = _KIND_SOURCE_FIELD

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "field": self.field,
        }


@dataclass(frozen=True, slots=True)
class ProviderSectionSelector:
    """A provider section bound to a recorded revision."""

    record_id: str
    content_hash: str
    revision: int
    section: str
    kind: str = _KIND_PROVIDER_SECTION

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "section": self.section,
        }


@dataclass(frozen=True, slots=True)
class InterviewEventSelector:
    """An interview event id bound to a recorded revision."""

    record_id: str
    content_hash: str
    revision: int
    event_id: str
    kind: str = _KIND_INTERVIEW_EVENT

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "event_id": self.event_id,
        }


@dataclass(frozen=True, slots=True)
class VerificationOutcomeSelector:
    """A verification outcome bound to a recorded revision."""

    record_id: str
    content_hash: str
    revision: int
    outcome: str
    kind: str = _KIND_VERIFICATION_OUTCOME

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "revision": self.revision,
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

#: JSON Pointer (RFC 6901): the empty root or ``/``-prefixed reference tokens
#: with ``~`` only as ``~0``/``~1``. Rejects bare text, dangling ``~``, and bad
#: escapes so a catalog selector never carries free text.
_JSON_POINTER_RE: re.Pattern[str] = re.compile(r"\A(/([^~/]|~[01])*)*\Z")

#: Upper bound on a catalog JSON Pointer length (defends against pathological
#: inputs before the regex runs).
_MAX_JSON_POINTER_LENGTH: int = 1024

#: Source field identifier (snake_case, like ``order_id``/``schema``).
_SOURCE_FIELD_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")

#: Provider/OKF section identifier (safe-id shape, like ``overview``).
_PROVIDER_SECTION_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")

#: Interview event identifier (a slug or lowercase UUID hex; may start with a
#: digit, like ``evt-1``).
_INTERVIEW_EVENT_RE: re.Pattern[str] = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,127}\Z")

#: Allowed verification outcomes (mirrors the verification ``OutcomeStatus``).
_VERIFICATION_OUTCOMES: frozenset[str] = frozenset(
    {"passed", "failed", "skipped", "unavailable"}
)

#: Identifier-shaped selector fields validated by a shared regex. Maps the
#: kind to the attribute name on the selector dataclass.
_SELECTOR_TEXT_FIELD: Mapping[str, str] = {
    _KIND_SOURCE_FIELD: "field",
    _KIND_PROVIDER_SECTION: "section",
    _KIND_INTERVIEW_EVENT: "event_id",
}

#: Regex for each identifier-shaped selector field (indexed by kind).
_SELECTOR_FIELD_RE: Mapping[str, re.Pattern[str]] = {
    _KIND_SOURCE_FIELD: _SOURCE_FIELD_RE,
    _KIND_PROVIDER_SECTION: _PROVIDER_SECTION_RE,
    _KIND_INTERVIEW_EVENT: _INTERVIEW_EVENT_RE,
}

#: Record kinds each selector kind may bind to. A document line range requires
#: a ``document`` record (snapshots carry no line semantics); the remaining
#: selectors reference structured content stored as either kind.
_SELECTOR_APPLICABLE_KINDS: Mapping[str, frozenset[str]] = {
    _KIND_DOCUMENT_LINE: frozenset({_KIND_DOCUMENT}),
    _KIND_CATALOG_PATH: frozenset({_KIND_DOCUMENT, _KIND_SNAPSHOT}),
    _KIND_SOURCE_FIELD: frozenset({_KIND_DOCUMENT, _KIND_SNAPSHOT}),
    _KIND_PROVIDER_SECTION: frozenset({_KIND_DOCUMENT, _KIND_SNAPSHOT}),
    _KIND_INTERVIEW_EVENT: frozenset({_KIND_DOCUMENT, _KIND_SNAPSHOT}),
    _KIND_VERIFICATION_OUTCOME: frozenset({_KIND_DOCUMENT, _KIND_SNAPSHOT}),
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

    Because a label is surfaced verbatim, it must never carry a URL, DSN, or
    embedded credential. The structural indicators ``://`` (a URL/DSN scheme
    separator), ``@`` (a userinfo/credential separator as in
    ``user:pass@host``), and a backslash (a path-escape separator on Windows
    and an unusual character in a POSIX label) are rejected outright. This
    preserves every allowed label shape (paths, dotted identifiers, hyphenated
    slugs) while blocking the common credential-leak vectors.
    """

    if type(source) is not str:
        raise EvidenceError(CODE_EVIDENCE_INVALID_SOURCE) from None
    if not source.strip() or len(source) > _MAX_SOURCE_LABEL_LENGTH:
        raise EvidenceError(CODE_EVIDENCE_INVALID_SOURCE) from None
    if "\x00" in source or any(ord(ch) < 0x20 for ch in source):
        raise EvidenceError(CODE_EVIDENCE_INVALID_SOURCE) from None
    # Reject URL/DSN locators and embedded credentials before any path work:
    # ``://`` signals a scheme, ``@`` signals userinfo, and a backslash signals
    # a path separator/escape. No safe label shape uses any of these.
    if "://" in source or "@" in source or "\\" in source:
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

    def reopen_snapshot(self, content_hash: str) -> None:
        """Reopen and verify a content-addressed snapshot by content hash.

        Confirms the on-disk snapshot backing ``content_hash`` is a regular
        file (inspected with ``lstat`` so a substituted symlink is never
        followed), reads at most ``max_document_bytes + 1`` bytes, and verifies
        the re-hash matches ``content_hash``. Fails safely (raising
        :class:`EvidenceError`) when the snapshot is missing, a symlink or
        non-regular file, unreadable, oversized, or tampered (the on-disk hash
        differs). This is a genuine reopenability check -- never a mere
        path-existence probe -- yet it never exposes a path, body, or raw
        exception cause: only stable evidence error codes surface.
        """

        if type(content_hash) is not str or _HEX64_RE.match(content_hash) is None:
            raise EvidenceError(CODE_EVIDENCE_NOT_FOUND) from None
        target = self._snapshots / content_hash
        try:
            info = os.lstat(target)
        except OSError:
            raise EvidenceError(CODE_EVIDENCE_NOT_FOUND) from None
        mode = info.st_mode
        # Reject symbolic links explicitly (lstat does not follow them),
        # mirroring _preflight_stat, so a symlink swapped in for the snapshot
        # cannot be resolved and read as a substitute.
        if stat.S_ISLNK(mode):
            raise EvidenceError(CODE_EVIDENCE_NOT_REGULAR) from None
        if not stat.S_ISREG(mode):
            raise EvidenceError(CODE_EVIDENCE_NOT_REGULAR) from None
        try:
            with target.open("rb") as handle:
                raw = handle.read(self._limits.max_document_bytes + 1)
        except OSError:
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None
        if len(raw) > self._limits.max_document_bytes:
            raise EvidenceError(CODE_EVIDENCE_TOO_LARGE) from None
        if _content_hash(raw) != content_hash:
            raise EvidenceError(CODE_EVIDENCE_STORE_CORRUPT) from None

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
        source_label = _validate_source_label(
            source, max_path_depth=self._limits.max_path_depth
        )
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
        digest = hashlib.sha256((kind + "\x00" + source).encode("utf-8")).hexdigest()[
            :16
        ]
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

        Raises :class:`EvidenceError`:

        * ``selector_stale`` when the bound revision/hash no longer match the
          current revision. Comparing both defeats the A→B→A replay where the
          content hash repeats at a later revision.
        * ``not_found`` when the record is absent or the selector is malformed.
        * ``selector_out_of_range`` for an inapplicable record kind, an invalid
          document line range, or a malformed kind-specific field (e.g. a
          non-JSON-Pointer catalog path or an unknown verification outcome).
        """

        if not isinstance(selector, tuple(_SELECTOR_KINDS.values())):
            raise EvidenceError(CODE_EVIDENCE_NOT_FOUND) from None
        record_id = selector.record_id
        content_hash = selector.content_hash
        revision = selector.revision
        if type(record_id) is not str or _RECORD_ID_RE.match(record_id) is None:
            raise EvidenceError(
                CODE_EVIDENCE_NOT_FOUND, safe_ids=(record_id,)
            ) from None
        if type(content_hash) is not str or _HEX64_RE.match(content_hash) is None:
            raise EvidenceError(CODE_EVIDENCE_SELECTOR_STALE) from None
        if type(revision) is not int or revision < 1:
            raise EvidenceError(CODE_EVIDENCE_SELECTOR_STALE) from None
        kind = selector.kind
        expected_cls = _SELECTOR_KINDS.get(kind)
        if expected_cls is None or type(selector) is not expected_cls:
            raise EvidenceError(
                CODE_EVIDENCE_NOT_FOUND, safe_ids=(record_id,)
            ) from None
        record = self._index.latest.get(record_id)
        if record is None:
            raise EvidenceError(
                CODE_EVIDENCE_NOT_FOUND, safe_ids=(record_id,)
            ) from None
        # Bind to a specific revision, not just a content hash: identical
        # content re-added later produces a new revision whose hash repeats.
        if record.content_hash != content_hash or record.revision != revision:
            raise EvidenceError(
                CODE_EVIDENCE_SELECTOR_STALE, safe_ids=(record_id,)
            ) from None
        applicable = _SELECTOR_APPLICABLE_KINDS.get(kind)
        if applicable is None or record.kind not in applicable:
            raise EvidenceError(
                CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE, safe_ids=(record_id,)
            ) from None
        if isinstance(selector, DocumentLineSelector):
            self._validate_line_range(selector, record)
        else:
            self._validate_typed_field(selector, kind, record_id)

    @staticmethod
    def _validate_typed_field(selector: object, kind: str, record_id: str) -> None:
        """Validate the kind-specific field shape for non-line selectors."""

        valid: bool
        if kind == _KIND_CATALOG_PATH:
            value = getattr(selector, "json_path", None)
            valid = (
                type(value) is str
                and len(value) <= _MAX_JSON_POINTER_LENGTH
                and _JSON_POINTER_RE.match(value) is not None
            )
        elif kind == _KIND_VERIFICATION_OUTCOME:
            value = getattr(selector, "outcome", None)
            valid = value in _VERIFICATION_OUTCOMES
        else:
            field_name = _SELECTOR_TEXT_FIELD.get(kind)
            pattern = _SELECTOR_FIELD_RE.get(kind)
            if field_name is None or pattern is None:
                raise EvidenceError(
                    CODE_EVIDENCE_NOT_FOUND, safe_ids=(record_id,)
                ) from None
            value = getattr(selector, field_name, None)
            valid = type(value) is str and pattern.match(value) is not None
        if not valid:
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


# --------------------------------------------------------------------------- #
# Typed claims and conflicts (Task 14)                                       #
# --------------------------------------------------------------------------- #

#: Claim/conflict store layout (within the Git-ignored session workspace).
_CLAIMS_DIR: str = "claims"
_CLAIMS_JOURNAL: str = "claims.jsonl"
_CONFLICTS_JOURNAL: str = "conflicts.jsonl"
_CLAIMS_LOCK: str = "claims.lock"

# Claim/conflict state labels (constants, not enum).
_CLAIM_STATE_CURRENT: str = "current"
_CONFLICT_STATE_UNRESOLVED: str = "unresolved"
_CONFLICT_STATE_RESOLVED: str = "resolved"

#: Allowed evidence-class values (mirrors :class:`EvidenceClass`).
_EVIDENCE_CLASS_VALUES: frozenset[str] = frozenset({c.value for c in EvidenceClass})


class ConflictKind(StrEnum):
    """Whether a conflict is resolved by named-approver authority or new evidence.

    ``SEMANTIC`` conflicts (disagreements over meaning) require the charter's
    current named approver to resolve; the resolution depends on the approver
    node so a charter approver change stales it. ``DETERMINISTIC`` conflicts
    (stale fingerprints, failed physical checks) cannot be resolved by
    attestation alone — they require new passing evidence.
    """

    SEMANTIC = "semantic"
    DETERMINISTIC = "deterministic"


#: Allowed conflict-kind values.
_CONFLICT_KIND_VALUES: frozenset[str] = frozenset({k.value for k in ConflictKind})


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """Immutable view of a typed evidence claim.

    Carries only safe, derived metadata. The declarative statement is held for
    journal reconstruction and canonical hashing but is never echoed by
    :meth:`safe_dict` or CLI output (it may carry sensitive business detail).
    Source observation scope is kept explicit in the claim statement and the
    bound selectors; the system never assigns a numeric confidence.
    """

    claim_id: str
    subject: str
    statement: str
    evidence_class: str
    creator_event: str
    contradicts: tuple[str, ...]
    selector_kinds: tuple[str, ...]
    #: Typed selectors retained for later revalidation against the current
    #: evidence revision. Each carries only identifiers and hashes (record id,
    #: content hash, revision, plus a kind-specific safe field) -- never a
    #: document body or value -- so retention never leaks sensitive content.
    selectors: tuple[_EvidenceSelector, ...]
    actor: str
    timestamp: str
    state: str

    def safe_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping without free-text content.

        Exposes only the selector *kinds* (not the full selectors): readiness
        revalidation reads ``.selectors`` directly while diagnostics surface
        only the coarse kind labels.
        """

        return {
            "claim_id": self.claim_id,
            "subject": self.subject,
            "evidence_class": self.evidence_class,
            "creator_event": self.creator_event,
            "contradicts": list(self.contradicts),
            "selector_kinds": list(self.selector_kinds),
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """Immutable view of an evidence conflict and any resolution.

    A resolution records a statement, a resolving answer or evidence id, the
    resolver identity, and a timestamp. It never deletes the involved (contrary)
    claims; they remain in history. The resolution statement is held for
    journal reconstruction but never echoed by :meth:`safe_dict`.
    """

    conflict_id: str
    kind: str
    subject: str
    involved_claim_ids: tuple[str, ...]
    affected_group_ids: tuple[str, ...]
    state: str
    resolution_statement: str | None
    resolving_answer_id: str | None
    resolving_evidence_id: str | None
    resolver: str | None
    resolved_at: str | None
    actor: str
    timestamp: str

    def safe_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping without free-text content."""

        payload: dict[str, object] = {
            "conflict_id": self.conflict_id,
            "kind": self.kind,
            "involved_claim_ids": list(self.involved_claim_ids),
            "affected_group_ids": list(self.affected_group_ids),
            "state": self.state,
        }
        if self.resolving_answer_id is not None:
            payload["resolving_answer_id"] = self.resolving_answer_id
        if self.resolving_evidence_id is not None:
            payload["resolving_evidence_id"] = self.resolving_evidence_id
        return payload


# --------------------------------------------------------------------------- #
# Claim/conflict input validation                                            #
# --------------------------------------------------------------------------- #


def _claim_utc_now_iso() -> str:
    """Return the current UTC time as a microsecond ISO-8601 string."""

    return datetime.now(UTC).isoformat(timespec="microseconds")


def _claim_validate_node(value: object, *, code: str) -> str:
    """Validate and return a stable identifier-shaped string."""

    if type(value) is not str or _RECORD_ID_RE.match(value) is None:
        raise EvidenceError(code) from None
    return value


def _claim_validate_text(value: object, *, code: str) -> str:
    """Validate that ``value`` is a non-blank, bounded text string."""

    if type(value) is not str or not value.strip():
        raise EvidenceError(code) from None
    if len(value) > MAX_TEXT_LENGTH:
        raise EvidenceError(code) from None
    return value


def _claim_validate_evidence_class(value: object) -> str:
    """Validate and return an allowed evidence-class value."""

    if isinstance(value, EvidenceClass):
        return value.value
    if type(value) is str and value in _EVIDENCE_CLASS_VALUES:
        return value
    raise EvidenceError(CODE_EVIDENCE_CLAIM_INVALID) from None


def _claim_validate_kind(value: object) -> str:
    """Validate and return an allowed conflict-kind value."""

    if isinstance(value, ConflictKind):
        return value.value
    if type(value) is str and value in _CONFLICT_KIND_VALUES:
        return value
    raise EvidenceError(CODE_EVIDENCE_CONFLICT_INVALID) from None


def _claim_validate_id_list(
    values: object,
    *,
    min_items: int = 0,
    code: str = CODE_EVIDENCE_CLAIM_INVALID,
) -> tuple[str, ...]:
    """Validate a sequence of stable identifiers (deduplicated, order-stable)."""

    if isinstance(values, str) or not isinstance(values, Sequence):
        raise EvidenceError(code) from None
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        identifier = _claim_validate_node(item, code=code)
        if identifier not in seen:
            seen.add(identifier)
            result.append(identifier)
    if len(result) < min_items:
        raise EvidenceError(code) from None
    return tuple(result)


def _claim_validate_selectors(
    selectors: object, evidence_store: EvidenceStore
) -> tuple[_EvidenceSelector, ...]:
    """Validate a non-empty selector sequence, rejecting stale revisions."""

    if isinstance(selectors, str) or not isinstance(selectors, Sequence):
        raise EvidenceError(CODE_EVIDENCE_CLAIM_INVALID) from None
    if len(selectors) == 0:
        raise EvidenceError(CODE_EVIDENCE_CLAIM_INVALID) from None
    result: list[_EvidenceSelector] = []
    for selector in selectors:
        if not isinstance(selector, tuple(_SELECTOR_KINDS.values())):
            raise EvidenceError(CODE_EVIDENCE_CLAIM_INVALID) from None
        typed = cast("_EvidenceSelector", selector)
        # validate_selector raises selector_stale for stale revisions and
        # selector_out_of_range/not_found for malformed or stale selectors.
        evidence_store.validate_selector(typed)
        result.append(typed)
    return tuple(result)


# --------------------------------------------------------------------------- #
# Selector reconstruction from a JSON mapping                                #
# --------------------------------------------------------------------------- #


def selector_from_mapping(data: object) -> _EvidenceSelector:
    """Build a typed selector dataclass from a JSON-safe mapping.

    Validates only the field shapes; binding to a recorded revision (rejecting
    stale selectors) is enforced later by
    :meth:`EvidenceStore.validate_selector`.
    """

    if not isinstance(data, Mapping):
        raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None
    kind = data.get("kind")
    record_id = data.get("record_id")
    content_hash = data.get("content_hash")
    revision = data.get("revision")
    if (
        type(kind) is not str
        or type(record_id) is not str
        or type(content_hash) is not str
        or type(revision) is not int
        or revision < 1
    ):
        raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None
    cls = _SELECTOR_KINDS.get(kind)
    if cls is None:
        raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None
    if kind == _KIND_DOCUMENT_LINE:
        start = data.get("start_line")
        end = data.get("end_line")
        if type(start) is not int or type(end) is not int:
            raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None
        return DocumentLineSelector(
            record_id=record_id,
            content_hash=content_hash,
            revision=revision,
            start_line=start,
            end_line=end,
        )
    if kind == _KIND_CATALOG_PATH:
        json_path = data.get("json_path")
        if type(json_path) is not str:
            raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None
        return CatalogPathSelector(
            record_id=record_id,
            content_hash=content_hash,
            revision=revision,
            json_path=json_path,
        )
    if kind == _KIND_SOURCE_FIELD:
        field_name = data.get("field")
        if type(field_name) is not str:
            raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None
        return SourceFieldSelector(
            record_id=record_id,
            content_hash=content_hash,
            revision=revision,
            field=field_name,
        )
    if kind == _KIND_PROVIDER_SECTION:
        section = data.get("section")
        if type(section) is not str:
            raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None
        return ProviderSectionSelector(
            record_id=record_id,
            content_hash=content_hash,
            revision=revision,
            section=section,
        )
    if kind == _KIND_INTERVIEW_EVENT:
        event_id = data.get("event_id")
        if type(event_id) is not str:
            raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None
        return InterviewEventSelector(
            record_id=record_id,
            content_hash=content_hash,
            revision=revision,
            event_id=event_id,
        )
    if kind == _KIND_VERIFICATION_OUTCOME:
        outcome = data.get("outcome")
        if outcome not in _VERIFICATION_OUTCOMES:
            raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None
        return VerificationOutcomeSelector(
            record_id=record_id,
            content_hash=content_hash,
            revision=revision,
            outcome=str(outcome),
        )
    raise EvidenceError(CODE_EVIDENCE_SELECTOR_OUT_OF_RANGE) from None


# --------------------------------------------------------------------------- #
# Reconstructed claim/conflict state                                         #
# --------------------------------------------------------------------------- #


@dataclass
class _ClaimIndex:
    """In-memory projection rebuilt from the journals on every load/mutation."""

    claims: dict[str, ClaimRecord] = field(default_factory=dict)
    conflicts: dict[str, ConflictRecord] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# ClaimStore                                                                 #
# --------------------------------------------------------------------------- #


class ClaimStore:
    """Append-only typed claim and conflict journal with stale propagation.

    Mirrors the secrecy and append-only discipline of
    :class:`EvidenceStore` and :class:`~selayer_discovery.interview.InterviewStore`.
    Claims and conflicts live in two append-only JSONL journals inside the
    Git-ignored session workspace. Every mutation binds a session artifact node
    via :meth:`SessionStore.record_artifact` so transitive stale dependents
    propagate through the session dependency index:

    * a claim node depends on its creator event (a creator revision stales the
      claim);
    * a conflict node depends on its involved claims (a claim revision stales
      the conflict);
    * a semantic conflict resolution additionally depends on the charter
      ``approver`` node, so a charter approver change stales the resolution;
    * a deterministic conflict resolution depends only on its new evidence,
      never on the approver.

    Diagnostics never echo a claim statement, a resolution statement, a reason,
    evidence bodies, or raw exception causes — only stable codes, constant
    generic details, and validated safe identifiers.
    """

    def __init__(
        self,
        session_store: SessionStore,
        evidence_store: EvidenceStore,
        *,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._session = session_store
        self._evidence = evidence_store
        self._root = session_store.root / _CLAIMS_DIR
        self._lock_timeout = lock_timeout
        self._claims_journal = self._root / _CLAIMS_JOURNAL
        self._conflicts_journal = self._root / _CONFLICTS_JOURNAL
        self._lock_path = self._root / _CLAIMS_LOCK
        self._lock = FileLock(str(self._lock_path))
        self._index = _ClaimIndex()

    # -- construction ------------------------------------------------------- #

    @classmethod
    def create(
        cls,
        session_store: SessionStore,
        evidence_store: EvidenceStore,
        *,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> ClaimStore:
        """Create the claim/conflict layout (idempotent) and load any journals."""

        store = cls(session_store, evidence_store, lock_timeout=lock_timeout)
        store._ensure_layout()
        with store._locked():
            store._index = store._reconstruct()
        return store

    @classmethod
    def open(
        cls,
        session_store: SessionStore,
        evidence_store: EvidenceStore,
        *,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> ClaimStore:
        """Open an existing claim/conflict store and rebuild state from journals."""

        store = cls(session_store, evidence_store, lock_timeout=lock_timeout)
        if not store._claims_journal.exists():
            raise EvidenceError(CODE_EVIDENCE_CLAIM_NOT_INITIALIZED) from None
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

    def claims(self) -> tuple[ClaimRecord, ...]:
        """Return all claims sorted by id."""

        return tuple(sorted(self._index.claims.values(), key=lambda c: c.claim_id))

    def get_claim(self, claim_id: str) -> ClaimRecord:
        """Return the claim with ``claim_id``."""

        record = self._index.claims.get(claim_id)
        if record is None:
            raise EvidenceError(
                CODE_EVIDENCE_CLAIM_NOT_FOUND, safe_ids=(claim_id,)
            ) from None
        return record

    def conflicts(self) -> tuple[ConflictRecord, ...]:
        """Return all conflicts (latest state per id) sorted by id."""

        return tuple(
            sorted(self._index.conflicts.values(), key=lambda c: c.conflict_id)
        )

    def get_conflict(self, conflict_id: str) -> ConflictRecord:
        """Return the latest state of conflict ``conflict_id``."""

        record = self._index.conflicts.get(conflict_id)
        if record is None:
            raise EvidenceError(
                CODE_EVIDENCE_CONFLICT_NOT_FOUND, safe_ids=(conflict_id,)
            ) from None
        return record

    # -- claim mutations ---------------------------------------------------- #

    def add_claim(
        self,
        *,
        claim_id: str,
        subject: str,
        statement: str,
        evidence_class: EvidenceClass | str,
        selectors: Sequence[_EvidenceSelector],
        creator_event: str,
        contradicts: Sequence[str] = (),
        actor: str,
    ) -> ClaimRecord:
        """Record a typed, evidence-selector-backed claim tied to a creator event.

        Requires a stable subject, a declarative statement, a valid evidence
        class, at least one selector bound to a current (non-stale) revision,
        and a creator event reference. The system never assigns a numeric
        confidence; source observation scope stays explicit in the statement
        and selector.
        """

        cid = _claim_validate_node(claim_id, code=CODE_EVIDENCE_CLAIM_INVALID)
        subj = _claim_validate_node(subject, code=CODE_EVIDENCE_CLAIM_INVALID)
        stmt = _claim_validate_text(statement, code=CODE_EVIDENCE_CLAIM_INVALID)
        eclazz = _claim_validate_evidence_class(evidence_class)
        creator = _claim_validate_node(creator_event, code=CODE_EVIDENCE_CLAIM_INVALID)
        contra = _claim_validate_id_list(contradicts)
        # Selectors are validated against the evidence store before the claim
        # lock is acquired (stale revisions raise selector_stale).
        sel_list = _claim_validate_selectors(selectors, self._evidence)
        author = normalize_actor_identity(actor)
        with self._locked():
            if cid in self._index.claims:
                raise EvidenceError(
                    CODE_EVIDENCE_CLAIM_INVALID, safe_ids=(cid,)
                ) from None
            timestamp = _claim_utc_now_iso()
            selector_kinds = tuple(sorted({s.kind for s in sel_list}))
            record = ClaimRecord(
                claim_id=cid,
                subject=subj,
                statement=stmt,
                evidence_class=eclazz,
                creator_event=creator,
                contradicts=contra,
                selector_kinds=selector_kinds,
                selectors=sel_list,
                actor=author,
                timestamp=timestamp,
                state=_CLAIM_STATE_CURRENT,
            )
            self._append_record(self._claims_journal, self._claim_payload(record))
            self._index.claims[cid] = record
            # Bind the claim as a session artifact node depending on its
            # creator event so a creator revision emits the claim as stale.
            self._session.record_artifact(
                cid,
                content_hash=canonical.fingerprint(self._claim_payload(record)),
                depends_on=(creator,),
                actor=author,
            )
            return record

    def assert_executable_evidence(self, claim_ids: Sequence[str]) -> None:
        """Reject inferred-only evidence for executable operations.

        Raises :class:`EvidenceError` when the cited evidence is inferred-only
        (no observed or asserted claim is present) or cites an unknown claim.
        """

        ids = _claim_validate_id_list(claim_ids, min_items=1)
        has_non_inferred = False
        for cid in ids:
            record = self._index.claims.get(cid)
            if record is None:
                raise EvidenceError(
                    CODE_EVIDENCE_CLAIM_NOT_FOUND, safe_ids=(cid,)
                ) from None
            if record.evidence_class != EvidenceClass.INFERRED.value:
                has_non_inferred = True
        if not has_non_inferred:
            raise EvidenceError(CODE_EVIDENCE_CLAIM_INFERRED_ONLY) from None

    # -- conflict mutations ------------------------------------------------- #

    def add_conflict(
        self,
        *,
        conflict_id: str,
        kind: ConflictKind | str,
        subject: str,
        involved_claim_ids: Sequence[str],
        affected_group_ids: Sequence[str],
        reason: str,
        actor: str,
    ) -> ConflictRecord:
        """Record an unresolved conflict affecting one or more dependency groups."""

        cfid = _claim_validate_node(conflict_id, code=CODE_EVIDENCE_CONFLICT_INVALID)
        kkind = _claim_validate_kind(kind)
        subj = _claim_validate_node(subject, code=CODE_EVIDENCE_CONFLICT_INVALID)
        involved = _claim_validate_id_list(
            involved_claim_ids, min_items=1, code=CODE_EVIDENCE_CONFLICT_INVALID
        )
        groups = _claim_validate_id_list(
            affected_group_ids,
            min_items=1,
            code=CODE_EVIDENCE_CONFLICT_INVALID,
        )
        _claim_validate_text(reason, code=CODE_EVIDENCE_CONFLICT_INVALID)
        author = normalize_actor_identity(actor)
        with self._locked():
            if cfid in self._index.conflicts:
                raise EvidenceError(
                    CODE_EVIDENCE_CONFLICT_INVALID, safe_ids=(cfid,)
                ) from None
            for cid in involved:
                if cid not in self._index.claims:
                    raise EvidenceError(
                        CODE_EVIDENCE_CLAIM_NOT_FOUND, safe_ids=(cid,)
                    ) from None
            timestamp = _claim_utc_now_iso()
            record = ConflictRecord(
                conflict_id=cfid,
                kind=kkind,
                subject=subj,
                involved_claim_ids=involved,
                affected_group_ids=groups,
                state=_CONFLICT_STATE_UNRESOLVED,
                resolution_statement=None,
                resolving_answer_id=None,
                resolving_evidence_id=None,
                resolver=None,
                resolved_at=None,
                actor=author,
                timestamp=timestamp,
            )
            self._append_record(self._conflicts_journal, self._conflict_payload(record))
            self._index.conflicts[cfid] = record
            # Bind the conflict as a session artifact node depending on the
            # involved claims so a claim revision emits the conflict as stale.
            self._session.record_artifact(
                cfid,
                content_hash=canonical.fingerprint(self._conflict_payload(record)),
                depends_on=tuple(involved),
                actor=author,
            )
            return record

    def resolve_conflict(
        self,
        *,
        conflict_id: str,
        statement: str,
        answer_id: str = "",
        evidence_id: str = "",
        actor: str,
    ) -> ConflictRecord:
        """Resolve a conflict, recording statement, answer/evidence id, and actor.

        Semantic conflicts require the charter's current named approver; the
        resolution depends on the ``approver`` node so a charter approver change
        stales it. Deterministic conflicts cannot be resolved by attestation
        alone — they require a resolving evidence id (new passing evidence) and
        never depend on the approver. The resolution never deletes the involved
        (contrary) claims.
        """

        cfid = _claim_validate_node(conflict_id, code=CODE_EVIDENCE_CONFLICT_NOT_FOUND)
        stmt = _claim_validate_text(statement, code=CODE_EVIDENCE_CONFLICT_INVALID)
        answer = answer_id if type(answer_id) is str else ""
        evidence = evidence_id if type(evidence_id) is str else ""
        if answer:
            answer = _claim_validate_node(answer, code=CODE_EVIDENCE_CONFLICT_INVALID)
        if evidence:
            evidence = _claim_validate_node(
                evidence, code=CODE_EVIDENCE_CONFLICT_INVALID
            )
        author = normalize_actor_identity(actor)
        with self._locked():
            current = self._index.conflicts.get(cfid)
            if current is None:
                raise EvidenceError(
                    CODE_EVIDENCE_CONFLICT_NOT_FOUND, safe_ids=(cfid,)
                ) from None
            if current.state == _CONFLICT_STATE_RESOLVED:
                raise EvidenceError(
                    CODE_EVIDENCE_CONFLICT_ALREADY_RESOLVED, safe_ids=(cfid,)
                ) from None
            deps: list[str] = list(current.involved_claim_ids)
            if current.kind == ConflictKind.SEMANTIC.value:
                approver = normalize_actor_identity(self._session.charter.approver)
                if author != approver:
                    raise EvidenceError(
                        CODE_EVIDENCE_CONFLICT_ACTOR, safe_ids=(cfid,)
                    ) from None
                # The resolution depends on the approver so a charter approver
                # change stales this semantic resolution.
                deps.append("approver")
            else:
                # Deterministic failures cannot be resolved by attestation
                # alone; they require new passing evidence.
                if not evidence:
                    raise EvidenceError(
                        CODE_EVIDENCE_CONFLICT_DETERMINISTIC, safe_ids=(cfid,)
                    ) from None
            timestamp = _claim_utc_now_iso()
            resolved = ConflictRecord(
                conflict_id=current.conflict_id,
                kind=current.kind,
                subject=current.subject,
                involved_claim_ids=current.involved_claim_ids,
                affected_group_ids=current.affected_group_ids,
                state=_CONFLICT_STATE_RESOLVED,
                resolution_statement=stmt,
                resolving_answer_id=answer or None,
                resolving_evidence_id=evidence or None,
                resolver=author,
                resolved_at=timestamp,
                actor=author,
                timestamp=timestamp,
            )
            self._append_record(
                self._conflicts_journal, self._conflict_payload(resolved)
            )
            self._index.conflicts[cfid] = resolved
            # Re-record the conflict node with updated dependencies. A semantic
            # resolution now depends on the approver; a deterministic resolution
            # depends on the involved claims (and new evidence) but not approver.
            self._session.record_artifact(
                cfid,
                content_hash=canonical.fingerprint(self._conflict_payload(resolved)),
                depends_on=tuple(sorted(set(deps))),
                actor=author,
            )
            return resolved

    def group_blocked_by(self, group_id: str) -> tuple[str, ...]:
        """Return sorted unresolved conflict ids that affect ``group_id``."""

        gid = _claim_validate_node(group_id, code=CODE_EVIDENCE_CONFLICT_INVALID)
        blocking = [
            record.conflict_id
            for record in self._index.conflicts.values()
            if record.state == _CONFLICT_STATE_UNRESOLVED
            and gid in record.affected_group_ids
        ]
        return tuple(sorted(blocking))

    # -- payload builders --------------------------------------------------- #

    @staticmethod
    def _claim_payload(record: ClaimRecord) -> dict[str, object]:
        return {
            "claim_id": record.claim_id,
            "subject": record.subject,
            "statement": record.statement,
            "evidence_class": record.evidence_class,
            "creator_event": record.creator_event,
            "contradicts": list(record.contradicts),
            "selector_kinds": list(record.selector_kinds),
            "selectors": [selector.to_dict() for selector in record.selectors],
            "actor": record.actor,
            "timestamp": record.timestamp,
            "state": record.state,
        }

    @staticmethod
    def _conflict_payload(record: ConflictRecord) -> dict[str, object]:
        payload: dict[str, object] = {
            "conflict_id": record.conflict_id,
            "kind": record.kind,
            "subject": record.subject,
            "involved_claim_ids": list(record.involved_claim_ids),
            "affected_group_ids": list(record.affected_group_ids),
            "state": record.state,
            "actor": record.actor,
            "timestamp": record.timestamp,
        }
        if record.resolution_statement is not None:
            payload["resolution_statement"] = record.resolution_statement
        if record.resolving_answer_id is not None:
            payload["resolving_answer_id"] = record.resolving_answer_id
        if record.resolving_evidence_id is not None:
            payload["resolving_evidence_id"] = record.resolving_evidence_id
        if record.resolver is not None:
            payload["resolver"] = record.resolver
        if record.resolved_at is not None:
            payload["resolved_at"] = record.resolved_at
        return payload

    # -- append + durability ------------------------------------------------ #

    def _append_record(self, journal: Path, payload: Mapping[str, object]) -> None:
        """Append one canonical JSON line, flush, and fsync the journal."""

        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        try:
            descriptor = os.open(journal, flags, 0o600)
        except OSError:
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        try:
            with os.fdopen(descriptor, "a", closefd=False) as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if os.name == "posix":
            try:
                os.chmod(journal, 0o600)
            except OSError:
                pass

    # -- reconstruction ----------------------------------------------------- #

    def _reconstruct(self) -> _ClaimIndex:
        index = _ClaimIndex()
        if self._claims_journal.exists():
            with self._claims_journal.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    line = raw.rstrip("\n")
                    if line == "":
                        continue
                    record = self._parse_claim(line)
                    index.claims[record.claim_id] = record
        if self._conflicts_journal.exists():
            with self._conflicts_journal.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    line = raw.rstrip("\n")
                    if line == "":
                        continue
                    record = self._parse_conflict(line)
                    # Last-write-wins: a resolution supersedes the unresolved
                    # record while the journal stays append-only.
                    index.conflicts[record.conflict_id] = record
        return index

    def _parse_claim(self, line: str) -> ClaimRecord:
        try:
            obj = json.loads(line)
        except ValueError:
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        if not isinstance(obj, dict):
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        evidence_class = obj.get("evidence_class")
        if (
            type(evidence_class) is not str
            or evidence_class not in _EVIDENCE_CLASS_VALUES
        ):
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        raw_selectors = obj.get("selectors", ())
        if not isinstance(raw_selectors, Sequence) or isinstance(raw_selectors, str):
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        try:
            selectors = tuple(selector_from_mapping(item) for item in raw_selectors)
        except EvidenceError:
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        if len(selectors) != len(raw_selectors):
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        return ClaimRecord(
            claim_id=_claim_req_str(obj, "claim_id"),
            subject=_claim_req_str(obj, "subject"),
            statement=_claim_req_str(obj, "statement"),
            evidence_class=evidence_class,
            creator_event=_claim_req_str(obj, "creator_event"),
            contradicts=_claim_tuple_str(obj.get("contradicts", ())),
            selector_kinds=_claim_tuple_str(obj.get("selector_kinds", ())),
            selectors=selectors,
            actor=_claim_req_str(obj, "actor"),
            timestamp=_claim_validate_timestamp(obj.get("timestamp")),
            state=_claim_req_str(obj, "state"),
        )

    def _parse_conflict(self, line: str) -> ConflictRecord:
        try:
            obj = json.loads(line)
        except ValueError:
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        if not isinstance(obj, dict):
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        kind = obj.get("kind")
        state = obj.get("state")
        if (
            type(kind) is not str
            or kind not in _CONFLICT_KIND_VALUES
            or type(state) is not str
            or state not in (_CONFLICT_STATE_UNRESOLVED, _CONFLICT_STATE_RESOLVED)
        ):
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        return ConflictRecord(
            conflict_id=_claim_req_str(obj, "conflict_id"),
            kind=kind,
            subject=_claim_req_str(obj, "subject"),
            involved_claim_ids=_claim_tuple_str(obj.get("involved_claim_ids", ())),
            affected_group_ids=_claim_tuple_str(obj.get("affected_group_ids", ())),
            state=state,
            resolution_statement=_claim_opt_str(obj.get("resolution_statement")),
            resolving_answer_id=_claim_opt_str(obj.get("resolving_answer_id")),
            resolving_evidence_id=_claim_opt_str(obj.get("resolving_evidence_id")),
            resolver=_claim_opt_str(obj.get("resolver")),
            resolved_at=_claim_opt_timestamp(obj.get("resolved_at")),
            actor=_claim_req_str(obj, "actor"),
            timestamp=_claim_validate_timestamp(obj.get("timestamp")),
        )

    # -- locking ------------------------------------------------------------ #

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Acquire the claim lock, refresh from journals, then release."""

        try:
            self._lock.acquire(timeout=self._lock_timeout)
        except Timeout:
            raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
        try:
            self._index = self._reconstruct()
            yield
        finally:
            if self._lock.lock_counter > 0:
                self._lock.release()


# --------------------------------------------------------------------------- #
# Journal payload parsing helpers (strict on replay)                          #
# --------------------------------------------------------------------------- #


def _claim_req_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if type(value) is not str:
        raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
    return value


def _claim_opt_str(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is str:
        return value
    raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None


def _claim_tuple_str(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
    return tuple(str(item) for item in value)


def _claim_validate_timestamp(value: object) -> str:
    """Validate that ``value`` is an ISO-8601 UTC string."""

    if type(value) is not str:
        raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
    offset = parsed.tzinfo.utcoffset(parsed) if parsed.tzinfo is not None else None
    if offset != timedelta(0):
        raise EvidenceError(CODE_EVIDENCE_CLAIM_STORE_CORRUPT) from None
    return value


def _claim_opt_timestamp(value: object) -> str | None:
    if value is None:
        return None
    return _claim_validate_timestamp(value)
