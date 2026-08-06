"""Workspace policy and session CLI for ``selayer-discovery``.

The CLI is the only mutation surface for discovery sessions: it parses
structured charter inputs, enforces the workspace path policy, and delegates
persistence to the append-only :class:`~selayer_discovery.session.SessionStore`.

Workspace policy
----------------

* Sessions live under ``<project>/.selayer/discovery/sessions/<id>/`` and are
  Git-ignored (see the repository ``.gitignore``).
* Apply transactions live under ``<project>/.selayer/discovery/transactions/``
  and are Git-ignored.
* ``<project>/.selayer/discovery.yaml`` (project configuration) and
  ``<project>/semantic_changes/`` (approved summaries) remain Git-visible.
* The catalog path and summary root must resolve inside the project root.

Diagnostics
-----------

Errors render as stable, sorted JSON on stderr and never print a traceback by
default. Raw charter values, credentials, and paths outside the project are
never echoed; only stable codes, constant generic details, and validated safe
identifiers surface. ``--debug`` re-raises unexpected errors for development.

Structured inputs are parsed and validated before any mutation lock is
acquired, so an invalid charter or an escaping path fails before the session
store touches the filesystem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import duckdb
from ruamel.yaml import YAML, YAMLError

from selayer.catalog import SemanticLayer
from selayer.sources.errors import SourceError
from selayer.sources.profiles import (
    MappingArrowProviderResolver,
    MappingProfileResolver,
)
from selayer.sources.registry import SourceRegistry
from selayer_discovery.approval import (
    ApplyBatchAttestation,
    ApprovalError,
    GroupAttestation,
    PreparedBatch,
    attest_apply_batch,
    attest_group,
    compute_approved_summary_hash,
    prepare_apply_batch,
    render_approved_summary,
    validate_group_attestation,
    write_approved_summary_preview,
)
from selayer_discovery.canonical import fingerprint
from selayer_discovery.diagnostics import DiscoveryError
from selayer_discovery.evidence import (
    MEDIA_TEXT_MARKDOWN,
    MEDIA_TEXT_PLAIN,
    ClaimStore,
    EvidenceError,
    EvidenceStore,
    selector_from_mapping,
)
from selayer_discovery.interview import (
    InterviewError,
    InterviewStore,
)
from selayer_discovery.knowledge import (
    CODE_KNOWLEDGE_DUPLICATE_PROVIDER,
    CODE_KNOWLEDGE_PROVIDER_UNKNOWN,
    KnowledgeError,
    ProviderRegistry,
)
from selayer_discovery.model import SCHEMA_VERSION, normalize_actor_identity
from selayer_discovery.profiling import (
    CODE_PROFILE_ACTOR,
    PolicyActivation,
    ProfilePolicyError,
    ProfileRunner,
    SamplePolicy,
    SourceProfile,
    activate_policy,
    build_context_export,
    propose_policy,
    verify_activation,
)
from selayer_discovery.proposal import (
    Candidate,
    Proposal,
    ProposalError,
    VerificationBundle,
    build_proposal,
    reconstruct_candidate,
    render_review_preview,
    render_review_summary,
    verify_proposal,
    write_candidate,
)
from selayer_discovery.session import (
    CODE_NOT_INITIALIZED,
    SessionCharter,
    SessionError,
    SessionStore,
)
from selayer_discovery.transaction import (
    ApplyJournal,
    RecoveryConflict,
    TransactionError,
)
from selayer_discovery.transaction import (
    recover as recover_transactions,
)

__all__ = ["main", "run"]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Success exit code.
EXIT_OK: int = 0

#: Operational error exit code (charter, path, or session failure).
EXIT_ERROR: int = 1

#: Project-relative session workspace root.
_DISCOVERY_SESSIONS_REL = ".selayer/discovery/sessions"

#: Default approved-summary root (Git-visible), relative to the project.
_DEFAULT_SUMMARY_ROOT = "semantic_changes"

# Stable CLI diagnostic codes (rendered; never leak raw causes).
CODE_CHARTER_INVALID = "discovery.cli.charter_invalid"
CODE_CHARTER_LOAD = "discovery.cli.charter_load_failed"
CODE_PATH_NOT_CONTAINED = "discovery.cli.path_not_contained"
CODE_SESSION_ID_INVALID = "discovery.cli.session_id_invalid"
CODE_PROFILE_LOAD = "discovery.profile.load_failed"
CODE_PROFILE_SALT = "discovery.profile.salt_missing"
CODE_PROFILE_STALE = "discovery.profile.policy_stale"
CODE_PROPOSAL_ID_INVALID = "discovery.cli.proposal_id_invalid"
CODE_INTERNAL = "discovery.cli.internal"

# Stable session-id grammar (mirrors the session node-id shape so an id is safe
# to use as a single filesystem path component — no '/', no leading '..').
_SESSION_ID_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")
_HEX64_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")

#: Charter text fields that must be present and non-blank.
_REQUIRED_TEXT_FIELDS: tuple[str, ...] = (
    "business_question",
    "approver",
    "catalog_fingerprint",
)

#: Charter list fields that must be present and non-empty string lists.
_REQUIRED_LIST_FIELDS: tuple[str, ...] = (
    "inclusions",
    "exclusions",
    "acceptance_questions",
)


# --------------------------------------------------------------------------- #
# Safe CLI diagnostics                                                        #
# --------------------------------------------------------------------------- #


class _CliError(Exception):
    """Sanitized CLI diagnostic carrying only safe, stable fields.

    Only a stable ``code``, an optional *constant* generic ``safe_detail``,
    and validated ``safe_ids`` are ever rendered. Raw causes are never chained
    or surfaced (``from None`` at every raise site).
    """

    def __init__(
        self,
        code: str,
        *,
        safe_detail: str | None = None,
        safe_ids: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.safe_detail = safe_detail
        self.safe_ids = tuple(_safe_id(item) for item in safe_ids)
        super().__init__(code)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code}
        if self.safe_detail is not None:
            payload["safe_detail"] = self.safe_detail
        if self.safe_ids:
            payload["safe_ids"] = list(self.safe_ids)
        return payload


def _safe_id(value: object) -> str:
    """Return ``value`` only if it is a stable session-id-shaped string."""

    if type(value) is str and _SESSION_ID_RE.match(value) is not None:
        return value
    return "<id>"


def _error_payload(exc: BaseException) -> dict[str, object]:
    """Build a JSON-safe payload from a sanitized CLI, session, or evidence error."""

    if isinstance(exc, _CliError):
        return exc.to_dict()
    if isinstance(exc, SessionError):
        return exc.to_dict()
    if isinstance(exc, EvidenceError):
        return exc.to_dict()
    if isinstance(exc, InterviewError):
        return exc.to_dict()
    if isinstance(exc, KnowledgeError):
        return exc.to_dict()
    if isinstance(exc, SourceError):
        return {"code": exc.code, "source_id": _safe_id(exc.source_id)}
    if isinstance(exc, (ProfilePolicyError, DiscoveryError)):
        return exc.to_dict()
    if isinstance(exc, ProposalError):
        payload: dict[str, object] = {"code": exc.code}
        if exc.safe_detail is not None:
            payload["safe_detail"] = exc.safe_detail
        return payload
    return {"code": CODE_INTERNAL}


def _emit_error(exc: BaseException) -> None:
    """Write a stable, sorted JSON diagnostic to stderr (never a traceback)."""

    payload = _error_payload(exc)
    # Sort every list-valued diagnostic field so output is deterministic.
    for key, value in list(payload.items()):
        if isinstance(value, list):
            payload[key] = sorted(value)
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# Workspace policy                                                            #
# --------------------------------------------------------------------------- #


def _project_root(value: str | None) -> Path:
    """Resolve the project root (defaults to the current directory)."""

    return Path(value).resolve() if value else Path.cwd().resolve()


def _session_dir(project: Path, session_id: str) -> Path:
    """Return the absolute session directory for ``session_id``."""

    return project / _DISCOVERY_SESSIONS_REL / session_id


def _workspace_rel(session_id: str) -> str:
    """Return the project-relative workspace path (forward slashes)."""

    return f"{_DISCOVERY_SESSIONS_REL}/{session_id}"


def _assert_contained(project: Path, candidate: Path, *, kind: str) -> Path:
    """Resolve ``candidate`` against the project and assert it stays inside.

    Relative candidates are resolved against the project root; absolute
    candidates must already be inside it. Symlink targets are resolved so a
    link pointing outside the project is rejected.
    """

    project_abs = project.resolve()
    target = candidate if candidate.is_absolute() else project_abs / candidate
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(project_abs)
    except ValueError:
        raise _CliError(CODE_PATH_NOT_CONTAINED, safe_detail=kind) from None
    return resolved


def _validate_session_id(value: object) -> str:
    """Validate a CLI-supplied session id before using it as a path component."""

    if type(value) is not str or _SESSION_ID_RE.match(value) is None:
        raise _CliError(CODE_SESSION_ID_INVALID) from None
    return value


def _validate_proposal_id(value: object) -> str:
    """Validate an explicit proposal id before using it as a path component.

    The proposal id must satisfy the same stable node-id grammar as a session
    id (no ``/``, no ``..``, no leading digit) so it is always a single safe
    filesystem component and a safe diagnostic token.
    """

    if type(value) is not str or _SESSION_ID_RE.match(value) is None:
        raise _CliError(CODE_PROPOSAL_ID_INVALID) from None
    return value


# --------------------------------------------------------------------------- #
# Charter loading and validation                                              #
# --------------------------------------------------------------------------- #


def _load_charter_mapping(path: Path) -> dict[str, object]:
    """Load a charter YAML file into a mapping (never leaks parse details)."""

    safe_yaml = YAML(typ="safe")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = safe_yaml.load(handle)
    except (OSError, YAMLError):
        raise _CliError(CODE_CHARTER_LOAD) from None
    if not isinstance(data, Mapping):
        raise _CliError(CODE_CHARTER_INVALID) from None
    return dict(data)


def _require_text(data: Mapping[str, object], field: str) -> str:
    value = data.get(field)
    if type(value) is not str or not value.strip():
        raise _CliError(CODE_CHARTER_INVALID) from None
    return value


def _require_string_list(data: Mapping[str, object], field: str) -> list[str]:
    value = data.get(field)
    if not isinstance(value, list) or not value:
        raise _CliError(CODE_CHARTER_INVALID) from None
    result: list[str] = []
    for item in value:
        if type(item) is not str or not item.strip():
            raise _CliError(CODE_CHARTER_INVALID) from None
        result.append(item)
    return result


def _build_charter(
    data: Mapping[str, object],
    *,
    explicit_session_id: str | None,
    catalog_path: str,
) -> SessionCharter:
    """Validate charter fields and construct a :class:`SessionCharter`.

    All charter validation failures surface as :data:`CODE_CHARTER_INVALID`;
    the :class:`SessionCharter` constructor is defense in depth. The session id
    is the explicit CLI/charter id when provided, else an auto-generated value.
    The ``catalog_path`` is the canonical project-relative POSIX path already
    resolved and validated as project-contained by the caller.
    """

    for field in _REQUIRED_TEXT_FIELDS:
        _require_text(data, field)
    # The catalog fingerprint is a text field but must also be a 64-hex hash.
    if _HEX64_RE.match(data["catalog_fingerprint"]) is None:  # type: ignore[arg-type]
        raise _CliError(CODE_CHARTER_INVALID) from None
    inclusions = _require_string_list(data, "inclusions")
    exclusions = _require_string_list(data, "exclusions")
    acceptance_questions = _require_string_list(data, "acceptance_questions")

    session_id: str
    if explicit_session_id is not None:
        session_id = explicit_session_id
    else:
        raw = data.get("session_id")
        if raw is None:
            # Prefix so the id always satisfies the node-id grammar (first
            # character must be a lowercase letter; a bare uuid hex may start
            # with a digit).
            session_id = f"session-{uuid.uuid4().hex}"
        elif type(raw) is str and raw.strip():
            session_id = raw
        else:
            raise _CliError(CODE_CHARTER_INVALID) from None

    return SessionCharter(
        session_id=session_id,
        catalog_path=catalog_path,
        business_question=data["business_question"],  # type: ignore[arg-type]
        catalog_fingerprint=data["catalog_fingerprint"],  # type: ignore[arg-type]
        approver=data["approver"],  # type: ignore[arg-type]
        inclusions=tuple(inclusions),
        exclusions=tuple(exclusions),
        acceptance_questions=tuple(acceptance_questions),
    )


# --------------------------------------------------------------------------- #
# Output helpers                                                              #
# --------------------------------------------------------------------------- #


def _emit_json(payload: Mapping[str, object]) -> None:
    """Write a deterministic JSON object (sorted keys) to stdout."""

    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# Command handlers                                                            #
# --------------------------------------------------------------------------- #


def _handle_init(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    project_abs = project.resolve()
    # The catalog path is required and validated as project-contained before
    # the mutation lock is acquired, then normalized to the canonical
    # project-relative POSIX path persisted alongside the charter fingerprint.
    catalog_abs = _assert_contained(project, Path(args.catalog_path), kind="catalog")
    catalog_path = catalog_abs.relative_to(project_abs).as_posix()
    summary_root = args.summary_root or _DEFAULT_SUMMARY_ROOT
    _assert_contained(project, Path(summary_root), kind="summary_root")

    data = _load_charter_mapping(Path(args.charter))
    explicit_id = args.session_id
    if explicit_id is None:
        charter_id = data.get("session_id")
        explicit_id = charter_id if type(charter_id) is str and charter_id else None
    charter = _build_charter(
        data, explicit_session_id=explicit_id, catalog_path=catalog_path
    )
    session_dir = _session_dir(project, charter.session_id)
    actor = args.actor or charter.approver
    store = SessionStore.create(session_dir, charter=charter, actor=actor)
    snapshot = store.reconstruct()
    _emit_json(
        {
            "session_id": charter.session_id,
            "state": snapshot.state.value,
            "charter_fingerprint": charter.fingerprint,
            "workspace": _workspace_rel(charter.session_id),
        }
    )
    return EXIT_OK


def _handle_status(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _session_dir(project, session_id)
    if not (session_dir / "events.jsonl").exists():
        raise SessionError(CODE_NOT_INITIALIZED, safe_ids=(session_id,))
    store = SessionStore.open(session_dir)
    snapshot = store.reconstruct()
    charter_fingerprint: str | None = (
        snapshot.charter.fingerprint if snapshot.charter is not None else None
    )
    _emit_json(
        {
            "session_id": store.session_id,
            "state": snapshot.state.value,
            "charter_fingerprint": charter_fingerprint,
            "head_hash": snapshot.head_hash,
            "schema_version": SCHEMA_VERSION,
            "event_count": len(snapshot.events),
            "stale_nodes": list(snapshot.stale_nodes),
            "workspace": _workspace_rel(store.session_id),
        }
    )
    return EXIT_OK


def _handle_close(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _session_dir(project, session_id)
    if not (session_dir / "events.jsonl").exists():
        raise SessionError(CODE_NOT_INITIALIZED, safe_ids=(session_id,))
    store = SessionStore.open(session_dir)
    actor = args.actor or store.charter.approver
    store.close(actor=actor)
    snapshot = store.reconstruct()
    _emit_json(
        {
            "session_id": session_id,
            "state": snapshot.state.value,
            "head_hash": snapshot.head_hash,
        }
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Evidence intake handlers                                                   #
# --------------------------------------------------------------------------- #

#: Evidence store lives inside the Git-ignored session workspace.
_EVIDENCE_REL = "evidence"


def _evidence_root(session_dir: Path) -> Path:
    """Return the evidence store root for a session directory."""

    return session_dir / _EVIDENCE_REL


def _require_session(project: Path, session_id: str) -> Path:
    """Validate the session exists and return its directory."""

    session_dir = _session_dir(project, session_id)
    if not (session_dir / "events.jsonl").exists():
        raise SessionError(CODE_NOT_INITIALIZED, safe_ids=(session_id,))
    return session_dir


_PROVIDER_CONFIG_KEY_RE = re.compile(r"\A[a-z][a-z0-9_.-]{0,63}\Z")
_ENV_REF_RE = re.compile(r"\A[A-Z][A-Z0-9_]{0,127}\Z")
_SECRET_KEY_PARTS = frozenset(
    {"password", "passwd", "token", "secret", "credential", "apikey", "private_key"}
)
_FORBIDDEN_PROVIDER_KEYS = frozenset(
    {"command", "executable", "shell", "subprocess", "process"}
)


def _parse_assignments(values: Sequence[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` options without exposing the value in errors."""

    result: dict[str, str] = {}
    for raw in values:
        if type(raw) is not str or raw.count("=") != 1:
            raise KnowledgeError("discovery.knowledge.invalid_configuration") from None
        key, value = raw.split("=", 1)
        if _PROVIDER_CONFIG_KEY_RE.match(key) is None or not value:
            raise KnowledgeError("discovery.knowledge.invalid_configuration") from None
        if key in result:
            raise KnowledgeError("discovery.knowledge.invalid_configuration") from None
        result[key] = value
    return result


def _validate_provider_configuration(
    project: Path,
    *,
    provider_name: str,
    provider_type: str,
    options: Sequence[str],
    env_refs: Sequence[str],
    root: str | None,
) -> tuple[dict[str, object], str]:
    """Validate provider config and return safe config plus its fingerprint."""

    if _PROVIDER_CONFIG_KEY_RE.match(provider_name) is None:
        raise KnowledgeError("discovery.knowledge.invalid_configuration") from None
    if _PROVIDER_CONFIG_KEY_RE.match(provider_type) is None:
        raise KnowledgeError(
            CODE_KNOWLEDGE_PROVIDER_UNKNOWN, safe_provider=provider_type
        ) from None
    if provider_type not in ProviderRegistry.discover_types():
        raise KnowledgeError(
            CODE_KNOWLEDGE_PROVIDER_UNKNOWN, safe_provider=provider_type
        ) from None
    parsed_options = _parse_assignments(options)
    parsed_env = _parse_assignments(env_refs)
    safe_options: dict[str, object] = {}
    for key, value in parsed_options.items():
        lowered = key.lower()
        if lowered in _FORBIDDEN_PROVIDER_KEYS or any(
            part in lowered for part in _SECRET_KEY_PARTS
        ):
            raise KnowledgeError("discovery.knowledge.invalid_configuration") from None
        if "://" in value or "@" in value or "\\x00" in value:
            raise KnowledgeError("discovery.knowledge.invalid_configuration") from None
        safe_options[key] = value
    safe_env: dict[str, str] = {}
    for key, value in parsed_env.items():
        if key.lower() in _FORBIDDEN_PROVIDER_KEYS or any(
            part in key.lower() for part in _SECRET_KEY_PARTS
        ):
            raise KnowledgeError("discovery.knowledge.invalid_configuration") from None
        if _ENV_REF_RE.match(value) is None:
            raise KnowledgeError("discovery.knowledge.invalid_configuration") from None
        safe_env[key] = value
    if root is not None:
        root_abs = _assert_contained(project, Path(root), kind="provider_root")
        safe_options["root"] = root_abs.relative_to(project.resolve()).as_posix()
    config: dict[str, object] = {
        "provider_name": provider_name,
        "provider_type": provider_type,
        "options": safe_options,
        "env_refs": safe_env,
    }
    return config, fingerprint(config)


def _provider_artifact_id(provider_name: str) -> str:
    return f"provider-{provider_name}"


def _handle_intake_add_provider(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    store = SessionStore.open(session_dir)
    snapshot = store.reconstruct()
    artifact_id = _provider_artifact_id(args.name)
    if artifact_id in snapshot.artifact_hashes:
        raise KnowledgeError(
            CODE_KNOWLEDGE_DUPLICATE_PROVIDER, safe_provider=args.name
        ) from None
    config, config_hash = _validate_provider_configuration(
        project,
        provider_name=args.name,
        provider_type=args.provider_type,
        options=tuple(args.option),
        env_refs=tuple(args.env),
        root=args.root,
    )
    actor = args.actor or store.charter.approver
    store.record_artifact(artifact_id, content_hash=config_hash, actor=actor)
    _emit_json(
        {
            "provider_name": config["provider_name"],
            "provider_type": config["provider_type"],
            "config_fingerprint": config_hash,
            "artifact_id": artifact_id,
        }
    )
    return EXIT_OK


def _handle_intake_add_document(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    store = EvidenceStore.create(_evidence_root(session_dir))
    record = store.add_document(
        args.path,
        allowed_roots=(project,),
        source=args.source,
    )
    _emit_json(record.to_dict())
    return EXIT_OK


def _handle_profile_scan(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    charter = SessionStore.open(session_dir).charter
    catalog_abs = _assert_contained(
        project, project / charter.catalog_path, kind="catalog"
    )
    layer = SemanticLayer.load(catalog_abs)
    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        layer,
        connection,
        MappingProfileResolver({}),
        MappingArrowProviderResolver({}),
    )
    try:
        with registry.open_scan_session(
            args.source_id,
            columns=tuple(args.columns),
            batch_size=args.batch_size,
        ) as session:
            profile = ProfileRunner(
                session,
                session_dir / "profile" / "spill",
                timeout=args.timeout,
                grain=tuple(args.grain),
            ).run()
        _emit_json(profile.to_dict())
    finally:
        registry.close()
    return EXIT_OK


def _handle_intake_snapshot(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    media_type = args.media_type
    if media_type not in (MEDIA_TEXT_MARKDOWN, MEDIA_TEXT_PLAIN):
        raise EvidenceError("discovery.evidence.invalid_media") from None

    provider_name = args.provider
    resource_id = args.resource_id
    revision = args.revision
    selector = args.selector or ""
    provider_snapshot = any(
        value is not None for value in (provider_name, resource_id, revision)
    ) or bool(selector)
    store = SessionStore.open(session_dir)
    if provider_snapshot:
        if not provider_name or not resource_id or not revision:
            raise KnowledgeError("discovery.knowledge.invalid_resource") from None
        if _PROVIDER_CONFIG_KEY_RE.match(provider_name) is None:
            raise KnowledgeError("discovery.knowledge.invalid_resource") from None
        if resource_id.count(":") != 1 or not resource_id.startswith(
            f"{provider_name}:"
        ):
            raise KnowledgeError("discovery.knowledge.invalid_resource") from None
        if _HEX64_RE.match(revision) is None:
            raise KnowledgeError("discovery.knowledge.invalid_resource") from None
        if (
            type(selector) is not str
            or len(selector) > 256
            or any(ord(char) < 0x20 for char in selector)
        ):
            raise KnowledgeError("discovery.knowledge.invalid_resource") from None
        provider_artifact = _provider_artifact_id(provider_name)
        if provider_artifact not in store.reconstruct().artifact_hashes:
            raise KnowledgeError(
                CODE_KNOWLEDGE_PROVIDER_UNKNOWN, safe_provider=provider_name
            ) from None
        source = (
            args.source or f"provider/{provider_name}/{_safe_file_name(resource_id)}"
        )
    else:
        source = args.source
        if not source:
            raise EvidenceError("discovery.evidence.invalid_source") from None

    raw = _read_stdin_bytes()
    evidence = EvidenceStore.create(_evidence_root(session_dir))
    record = evidence.add_snapshot(raw, media_type=media_type, source=source)
    payload = record.to_dict()
    if provider_snapshot:
        artifact_id = f"knowledge-{_safe_file_name(resource_id)}"
        artifact_hash = fingerprint(
            {
                "content_hash": record.content_hash,
                "provider_name": provider_name,
                "resource_id": resource_id,
                "revision": revision,
                "selector": selector,
            }
        )
        actor = args.actor or store.charter.approver
        result = store.record_artifact(
            artifact_id, content_hash=artifact_hash, actor=actor
        )
        payload.update(
            {
                "provider_name": provider_name,
                "resource_id": resource_id,
                "provider_revision": revision,
                "selector": selector,
                "stale_targets": list(result.stale_targets),
            }
        )
    _emit_json(payload)
    return EXIT_OK


def _read_stdin_bytes() -> bytes:
    """Read raw bytes from stdin (never decodes or echoes content)."""

    handle = sys.stdin.buffer if sys.stdin is not None else None
    if handle is None:
        raise EvidenceError("discovery.evidence.invalid_encoding") from None
    return handle.read()


# --------------------------------------------------------------------------- #
# Interview handlers                                                         #
# --------------------------------------------------------------------------- #

#: Interview store lives inside the Git-ignored session workspace.
_INTERVIEW_REL = "interview"


def _interview_root(session_dir: Path) -> Path:
    """Return the interview store root for a session directory."""

    return session_dir / _INTERVIEW_REL


def _interview_actor(store: SessionStore, override: str | None) -> str:
    """Return the normalized actor (override or charter approver)."""

    return override if override else store.charter.approver


def _json_str_field(data: Mapping[str, object], key: str) -> str:
    """Extract a string field from a loaded JSON mapping (default empty)."""

    value = data.get(key, "")
    return value if type(value) is str else ""


def _json_seq_field(data: Mapping[str, object], key: str) -> list[str]:
    """Extract a string-list field from a loaded JSON mapping (default empty)."""

    value = data.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _handle_interview_ask(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    store = SessionStore.open(session_dir)
    data = _load_json_mapping(project, args.question, kind="question")
    interview = InterviewStore.create(store)
    actor = _interview_actor(store, args.actor)
    question = interview.ask(
        gate=_json_str_field(data, "gate"),
        text=_json_str_field(data, "text"),
        evidence_ids=_json_seq_field(data, "evidence_ids"),
        subjects=_json_seq_field(data, "subjects"),
        actor=actor,
    )
    _emit_json(question.safe_dict())
    return EXIT_OK


def _handle_interview_answer(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    store = SessionStore.open(session_dir)
    data = _load_json_mapping(project, args.answer, kind="answer")
    interview = InterviewStore.create(store)
    actor = _interview_actor(store, args.actor)
    result = interview.answer(
        gate=_json_str_field(data, "gate"),
        text=_json_str_field(data, "text"),
        actor=actor,
    )
    payload = result.answer.safe_dict()
    payload["stale_targets"] = list(result.stale_targets)
    _emit_json(payload)
    return EXIT_OK


def _handle_interview_correct(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    store = SessionStore.open(session_dir)
    data = _load_json_mapping(project, args.correction, kind="correction")
    interview = InterviewStore.create(store)
    actor = _interview_actor(store, args.actor)
    result = interview.correct(
        answer_id=_json_str_field(data, "answer_id"),
        reason=_json_str_field(data, "reason"),
        replacement=_json_str_field(data, "replacement"),
        actor=actor,
    )
    payload = result.correction.safe_dict()
    payload["stale_targets"] = list(result.stale_targets)
    _emit_json(payload)
    return EXIT_OK


def _handle_interview_set_gate(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    store = SessionStore.open(session_dir)
    data = _load_json_mapping(project, args.disposition, kind="gate_disposition")
    interview = InterviewStore.create(store)
    actor = _interview_actor(store, args.actor)
    record = interview.set_gate(
        gate=args.gate,
        disposition=_json_str_field(data, "disposition"),
        reason=_json_str_field(data, "reason") or None,
        conflict_ids=_json_seq_field(data, "conflict_ids"),
        affected_group_ids=_json_seq_field(data, "affected_group_ids"),
        actor=actor,
    )
    _emit_json(record.safe_dict())
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Evidence claim/conflict handlers (Task 14)                                 #
# --------------------------------------------------------------------------- #


def _claim_store(session_dir: Path) -> tuple[SessionStore, ClaimStore]:
    """Open the session and build a claim store over its evidence store."""

    session_store = SessionStore.open(session_dir)
    evidence_store = EvidenceStore.create(_evidence_root(session_dir))
    return session_store, ClaimStore.create(session_store, evidence_store)


def _handle_evidence_add_claim(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    data = _load_json_mapping(project, args.claim, kind="claim")
    session_store, claims = _claim_store(session_dir)
    raw_selectors = data.get("selectors")
    if not isinstance(raw_selectors, list):
        raw_selectors = []
    selectors = tuple(selector_from_mapping(item) for item in raw_selectors)
    actor = _interview_actor(session_store, args.actor)
    record = claims.add_claim(
        claim_id=_json_str_field(data, "claim_id"),
        subject=_json_str_field(data, "subject"),
        statement=_json_str_field(data, "statement"),
        evidence_class=_json_str_field(data, "evidence_class"),
        selectors=selectors,
        creator_event=_json_str_field(data, "creator_event"),
        contradicts=tuple(_json_seq_field(data, "contradicts")),
        actor=actor,
    )
    _emit_json(record.safe_dict())
    return EXIT_OK


def _handle_evidence_add_conflict(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    data = _load_json_mapping(project, args.conflict, kind="conflict")
    session_store, claims = _claim_store(session_dir)
    actor = _interview_actor(session_store, args.actor)
    record = claims.add_conflict(
        conflict_id=_json_str_field(data, "conflict_id"),
        kind=_json_str_field(data, "kind"),
        subject=_json_str_field(data, "subject"),
        involved_claim_ids=tuple(_json_seq_field(data, "involved_claim_ids")),
        affected_group_ids=tuple(_json_seq_field(data, "affected_group_ids")),
        reason=_json_str_field(data, "reason"),
        actor=actor,
    )
    _emit_json(record.safe_dict())
    return EXIT_OK


def _handle_evidence_resolve_conflict(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    data = _load_json_mapping(project, args.resolution, kind="resolution")
    session_store, claims = _claim_store(session_dir)
    actor = _interview_actor(session_store, args.actor)
    record = claims.resolve_conflict(
        conflict_id=_json_str_field(data, "conflict_id"),
        statement=_json_str_field(data, "statement"),
        answer_id=_json_str_field(data, "answer_id"),
        evidence_id=_json_str_field(data, "evidence_id"),
        actor=actor,
    )
    _emit_json(record.safe_dict())
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Proposal handlers                                                          #
# --------------------------------------------------------------------------- #

#: Proposals and reconstructed candidates live inside the Git-ignored session
#: workspace. Apply (Task 20) owns writing repository files; import only ever
#: writes inside the ignored workspace.
_PROPOSAL_REL = "proposals"

#: Conventional project-contained authored knowledge roots. A reference update
#: resolves its base against ``references/`` and an overlay update against
#: ``okf_overlays/`` (consistent with the repo fixtures). When a root is absent
#: the base is empty, which remains valid for create operations.
_REFERENCE_ROOT_REL = "references"
_OVERLAY_ROOT_REL = "okf_overlays"


def _proposal_root(session_dir: Path) -> Path:
    """Return the proposal store root for a session directory."""

    return session_dir / _PROPOSAL_REL


def _load_proposal_mapping(project: Path, raw_path: str) -> dict[str, object]:
    """Load a proposal YAML/JSON file from a project-contained path."""

    abs_path = _assert_contained(project, Path(raw_path), kind="proposal")
    safe_yaml = YAML(typ="safe")
    try:
        with abs_path.open("r", encoding="utf-8") as handle:
            data = safe_yaml.load(handle)
    except (OSError, YAMLError):
        raise _CliError(CODE_PROFILE_LOAD) from None
    if not isinstance(data, Mapping):
        raise _CliError(CODE_PROFILE_LOAD) from None
    return dict(data)


def _session_base_catalog_text(project: Path, session_dir: Path) -> str:
    """Read the base catalog text bound to the session charter."""

    charter = SessionStore.open(session_dir).charter
    catalog_abs = _assert_contained(
        project, project / charter.catalog_path, kind="catalog"
    )
    try:
        return catalog_abs.read_text(encoding="utf-8")
    except OSError:
        raise _CliError(CODE_PROFILE_LOAD) from None


def _load_authored_knowledge_root(project: Path, root_rel: str) -> dict[str, str]:
    """Read authored ``.md`` files from a project-contained knowledge root.

    Returns a mapping of POSIX-relative path to text for the root, or an empty
    mapping when the root is absent (which remains valid for create
    operations). Each file is resolved and asserted to stay inside the root so
    a symlink can never pull in a file outside the project.
    """

    root_abs = _assert_contained(project, Path(root_rel), kind="knowledge_root")
    if not root_abs.exists() or not root_abs.is_dir():
        return {}
    root_resolved = root_abs.resolve(strict=False)
    result: dict[str, str] = {}
    for path in sorted(root_abs.rglob("*.md")):
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            # Defence in depth: a symlink target escaping the root is skipped.
            continue
        rel = resolved.relative_to(root_resolved).as_posix()
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError:
            raise _CliError(CODE_PROFILE_LOAD) from None
        result[rel] = text
    return result


def _authored_knowledge_bases(
    project: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return the authored ``(references, overlays)`` bases for a project."""

    return (
        _load_authored_knowledge_root(project, _REFERENCE_ROOT_REL),
        _load_authored_knowledge_root(project, _OVERLAY_ROOT_REL),
    )


def _handle_proposal_import(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    data = _load_proposal_mapping(project, args.proposal)
    proposal = build_proposal(data)
    base_catalog_text = _session_base_catalog_text(project, session_dir)
    base_references, base_overlays = _authored_knowledge_bases(project)
    candidate = reconstruct_candidate(
        base_catalog_text=base_catalog_text,
        base_references=base_references,
        base_overlays=base_overlays,
        operations=proposal.operations,
    )
    preview = render_review_preview(
        base_catalog_text=base_catalog_text,
        base_references=base_references,
        base_overlays=base_overlays,
        candidate=candidate,
    )
    # Persist the reconstructed candidate and the proposal record inside the
    # Git-ignored session workspace. Apply (Task 20) owns writing repository
    # files; import never writes outside the ignored workspace.
    proposal_dir = _proposal_root(session_dir) / proposal.proposal_id
    proposal_dir.mkdir(parents=True, exist_ok=True)
    write_candidate(candidate, proposal_dir / "candidate")
    record = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal.proposal_id,
        "title": proposal.title,
        "proposal_fingerprint": proposal.fingerprint,
        "candidate_fingerprint": candidate.fingerprint,
        "preview_fingerprint": preview.fingerprint,
        "groups": [group.to_dict() for group in proposal.groups],
        "operations": [op.to_dict() for op in proposal.operations],
    }
    record_path = proposal_dir / "proposal.json"
    tmp = record_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True)
    os.replace(tmp, record_path)
    _restrict_file(record_path)
    _emit_json(
        {
            "proposal_id": proposal.proposal_id,
            "proposal_fingerprint": proposal.fingerprint,
            "candidate_fingerprint": candidate.fingerprint,
            "preview_fingerprint": preview.fingerprint,
            "operations": len(proposal.operations),
            "groups": [group.group_id for group in proposal.groups],
            "workspace": _workspace_rel(session_id)
            + f"/{_PROPOSAL_REL}/{proposal.proposal_id}",
        }
    )
    return EXIT_OK


def _read_proposal_record(session_dir: Path, proposal_id: str) -> dict[str, object]:
    """Load a stored proposal record, or raise a sanitized CLI error."""

    record_path = _proposal_root(session_dir) / proposal_id / "proposal.json"
    # Containment defence in depth: the proposal id is grammar-validated by
    # the caller, but assert the resolved record never escapes the session
    # directory before any file is opened.
    try:
        record_path.resolve(strict=False).relative_to(session_dir.resolve(strict=False))
    except ValueError:
        raise _CliError(CODE_PATH_NOT_CONTAINED, safe_detail="proposal") from None
    if not record_path.exists():
        raise _CliError(CODE_NOT_INITIALIZED, safe_ids=(proposal_id,))
    try:
        with record_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise _CliError(CODE_INTERNAL) from None
    if not isinstance(data, Mapping):
        raise _CliError(CODE_INTERNAL) from None
    return dict(data)


def _latest_proposal_id(session_dir: Path) -> str:
    """Return the lexicographically-largest stored proposal id."""

    root = _proposal_root(session_dir)
    ids = (
        [
            child.name
            for child in root.iterdir()
            if child.is_dir() and (child / "proposal.json").exists()
        ]
        if root.exists()
        else []
    )
    if not ids:
        raise _CliError(CODE_NOT_INITIALIZED, safe_detail="no proposal")
    return max(ids)


def _handle_proposal_show(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    proposal_id = (
        _validate_proposal_id(args.proposal)
        if args.proposal
        else _latest_proposal_id(session_dir)
    )
    record = _read_proposal_record(session_dir, proposal_id)
    # Reconstruct a fresh candidate from the stored typed operations and render
    # a fresh preview. Previews are renderings only and are never verify or
    # apply authority. The operator-facing output is a body-free summary so a
    # document body or patch never reaches a terminal; the full preview API is
    # retained for deterministic internal use.
    raw_groups = record.get("groups", [])
    proposal = build_proposal(
        {
            "proposal_id": record.get("proposal_id"),
            "title": record.get("title"),
            "groups": raw_groups,
        }
    )
    base_catalog_text = _session_base_catalog_text(project, session_dir)
    base_references, base_overlays = _authored_knowledge_bases(project)
    candidate = reconstruct_candidate(
        base_catalog_text=base_catalog_text,
        base_references=base_references,
        base_overlays=base_overlays,
        operations=proposal.operations,
    )
    preview = render_review_preview(
        base_catalog_text=base_catalog_text,
        base_references=base_references,
        base_overlays=base_overlays,
        candidate=candidate,
    )
    summary = render_review_summary(
        base_references=base_references,
        base_overlays=base_overlays,
        candidate=candidate,
        preview=preview,
    )
    _emit_json(
        {
            "proposal_id": proposal.proposal_id,
            "proposal_fingerprint": proposal.fingerprint,
            "candidate_fingerprint": candidate.fingerprint,
            "preview_fingerprint": preview.fingerprint,
            "catalog": {
                "added_lines": summary.catalog_added_lines,
                "removed_lines": summary.catalog_removed_lines,
            },
            "references": [
                {
                    "path": entry.path,
                    "status": entry.status,
                    "fingerprint": entry.fingerprint,
                    "added_lines": entry.added_lines,
                    "removed_lines": entry.removed_lines,
                }
                for entry in summary.references
            ],
            "overlays": [
                {
                    "path": entry.path,
                    "status": entry.status,
                    "fingerprint": entry.fingerprint,
                    "added_lines": entry.added_lines,
                    "removed_lines": entry.removed_lines,
                }
                for entry in summary.overlays
            ],
        }
    )
    return EXIT_OK


#: The immutable verification report filename, written inside the Git-ignored
#: proposal workspace and bound to the proposal's input hashes.
_VERIFICATION_FILENAME: str = "verification.json"
_APPROVALS_DIR = "attestations"
_BATCH_FILENAME = "prepared-batch.json"
_APPLY_ATTESTATION_FILENAME = "apply-attestation.json"


def _approval_timestamp(value: str | None) -> str:
    return value or datetime.now(UTC).isoformat(timespec="microseconds")


def _proposal_for_approval(
    session_dir: Path, proposal_id: str
) -> tuple[dict[str, object], Proposal]:
    record = _read_proposal_record(session_dir, proposal_id)
    proposal = build_proposal(
        {
            "proposal_id": record.get("proposal_id"),
            "title": record.get("title"),
            "groups": record.get("groups", []),
        }
    )
    return record, proposal


def _current_evidence_lock(session_dir: Path) -> list[dict[str, object]]:
    """Return body-free evidence metadata bound into approval hashes."""

    session_store = SessionStore.open(session_dir)
    evidence_store = EvidenceStore.create(_evidence_root(session_dir))
    claim_store = ClaimStore.create(session_store, evidence_store)
    entries: list[dict[str, object]] = []
    for claim in claim_store.claims():
        for selector in claim.selectors:
            raw = selector.to_dict()
            entries.append(
                {
                    "claim_id": claim.claim_id,
                    "evidence_class": claim.evidence_class,
                    "record_id": raw.get("record_id"),
                    "content_hash": raw.get("content_hash"),
                    "revision": raw.get("revision"),
                    "selector_kind": raw.get("kind"),
                }
            )
    return sorted(
        entries, key=lambda entry: (str(entry["claim_id"]), str(entry["record_id"]))
    )


def _approval_context(
    project: Path,
    session_dir: Path,
    proposal_id: str,
    selected_group_ids: Sequence[str] | None = None,
) -> tuple[
    Proposal,
    Candidate,
    VerificationBundle,
    str,
    dict[str, str],
    dict[str, str],
    SessionStore,
]:
    """Reconstruct and verify the current proposal for approval commands."""

    from selayer_discovery.proposal import verify_proposal

    _, full_proposal = _proposal_for_approval(session_dir, proposal_id)
    selected = tuple(selected_group_ids or ())
    if selected:
        wanted = set(selected)
        groups = [group for group in full_proposal.groups if group.group_id in wanted]
        proposal = build_proposal(
            {
                "proposal_id": full_proposal.proposal_id,
                "title": full_proposal.title,
                "groups": [group.to_dict() for group in groups],
            }
        )
    else:
        proposal = full_proposal
    base_catalog_text = _session_base_catalog_text(project, session_dir)
    base_references, base_overlays = _authored_knowledge_bases(project)
    candidate = reconstruct_candidate(
        base_catalog_text=base_catalog_text,
        base_references=base_references,
        base_overlays=base_overlays,
        operations=proposal.operations,
    )
    proposal_dir = _proposal_root(session_dir) / proposal_id
    candidate_path = write_candidate(candidate, proposal_dir / "approval-candidate")
    candidate_layer = SemanticLayer.load(candidate_path)
    session_store = SessionStore.open(session_dir)
    evidence_store = EvidenceStore.create(_evidence_root(session_dir))
    claim_store = ClaimStore.create(session_store, evidence_store)
    interview_store = InterviewStore.create(session_store)
    bundle = verify_proposal(
        proposal=proposal,
        candidate=candidate,
        candidate_layer=candidate_layer,
        okf_output_dir=proposal_dir / "approval-okf",
        interview_store=interview_store,
        claim_store=claim_store,
        evidence_store=evidence_store,
    )
    snapshot = session_store.reconstruct()
    artifacts = snapshot.artifact_hashes
    evidence_lock = _current_evidence_lock(session_dir)
    evidence_lock_hash = fingerprint(evidence_lock)
    zero = "0" * 64
    session_hashes = {
        "charter": session_store.charter.fingerprint,
        "base_catalog": fingerprint(base_catalog_text),
        "policy": artifacts.get("policy", zero),
        "evidence_lock": evidence_lock_hash,
    }
    base_hashes = {
        "catalog": fingerprint(base_catalog_text),
        "references": fingerprint(base_references),
        "overlays": fingerprint(base_overlays),
    }
    return (
        proposal,
        candidate,
        bundle,
        base_catalog_text,
        base_hashes,
        session_hashes,
        session_store,
    )


def _write_approval_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(dict(value), handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _restrict_file(path)


def _read_approval_json(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        raise _CliError(CODE_INTERNAL) from None
    if not isinstance(value, Mapping):
        raise _CliError(CODE_INTERNAL)
    return dict(value)


def _handle_proposal_attest(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    proposal_id = _validate_proposal_id(args.proposal)
    proposal, candidate, bundle, _base_text, _bases, session_hashes, session_store = (
        _approval_context(project, session_dir, proposal_id)
    )
    group = next(
        (item for item in proposal.groups if item.group_id == args.group), None
    )
    if group is None:
        raise ApprovalError("discovery.approval.unknown_group")
    readiness = bundle.readiness_for(group.group_id)
    input_hashes = dict(session_hashes)
    input_hashes.update(
        {
            "group": fingerprint(group.to_dict()),
            "candidate": candidate.fingerprint,
            "verification": bundle.fingerprint,
        }
    )
    approver = normalize_actor_identity(args.approver)
    attestation = attest_group(
        session_id=session_id,
        group_id=group.group_id,
        approver=approver,
        current_approver=session_store.charter.approver,
        decision=args.decision,
        reason=args.reason or "",
        input_hashes=input_hashes,
        group_ready=readiness.ready,
        group_blocked=readiness.blockers,
        group_stale=False,
        timestamp=_approval_timestamp(args.timestamp),
    )
    path = (
        _proposal_root(session_dir)
        / proposal_id
        / _APPROVALS_DIR
        / f"{group.group_id}.json"
    )
    _write_approval_json(path, attestation.to_dict())
    _emit_json(
        {
            "proposal_id": proposal_id,
            "group_id": group.group_id,
            "fingerprint": attestation.fingerprint,
            "path": _workspace_rel(session_id)
            + f"/{_PROPOSAL_REL}/{proposal_id}/{_APPROVALS_DIR}/{group.group_id}.json",
        }
    )
    return EXIT_OK


def _handle_proposal_prepare_apply(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    proposal_id = _validate_proposal_id(args.proposal)
    group_ids = tuple(args.group)
    proposal, candidate, bundle, base_text, base_hashes, session_hashes, _store = (
        _approval_context(project, session_dir, proposal_id, group_ids)
    )
    attestations: dict[str, GroupAttestation] = {}
    for group_id in group_ids:
        path = (
            _proposal_root(session_dir)
            / proposal_id
            / _APPROVALS_DIR
            / f"{group_id}.json"
        )
        attestations[group_id] = GroupAttestation.from_mapping(
            _read_approval_json(path)
        )
    from selayer_discovery.approval import compute_approved_summary_hash

    evidence_lock = _current_evidence_lock(session_dir)
    summary_hash = compute_approved_summary_hash(
        proposal=proposal,
        group_ids=group_ids,
        candidate=candidate,
        verification=bundle,
        base_hashes=base_hashes,
        base_catalog_text=base_text,
        evidence_lock=evidence_lock,
        decision_statement="Approved dependency batch.",
    )
    batch = prepare_apply_batch(
        proposal=proposal,
        group_ids=group_ids,
        attestations=attestations,
        combined_candidate=candidate,
        combined_verification=bundle,
        base_hashes=base_hashes,
        current_session_hashes=session_hashes,
        approved_summary_hash=summary_hash,
    )
    path = _proposal_root(session_dir) / proposal_id / _BATCH_FILENAME
    _write_approval_json(path, batch.to_dict())
    _emit_json(
        {
            "proposal_id": proposal_id,
            "batch_hash": batch.fingerprint,
            "group_ids": list(batch.group_ids),
            "path": _workspace_rel(session_id)
            + f"/{_PROPOSAL_REL}/{proposal_id}/{_BATCH_FILENAME}",
        }
    )
    return EXIT_OK


def _prepared_batch_from_mapping(data: Mapping[str, object]) -> PreparedBatch:
    schema = data.get("schema_version")
    if type(schema) is not int:
        raise ApprovalError("discovery.approval.invalid")

    def _strings(name: str) -> tuple[str, ...]:
        raw = data.get(name)
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ApprovalError("discovery.approval.invalid")
        if not all(type(item) is str for item in raw):
            raise ApprovalError("discovery.approval.invalid")
        return tuple(cast(Sequence[str], raw))

    def _hashes(name: str) -> dict[str, str]:
        raw = data.get(name)
        if not isinstance(raw, Mapping) or not all(
            type(key) is str and type(value) is str for key, value in raw.items()
        ):
            raise ApprovalError("discovery.approval.invalid")
        return dict(cast(Mapping[str, str], raw))

    text_fields = (
        "session_id",
        "proposal_id",
        "candidate_fingerprint",
        "verification_fingerprint",
        "approved_summary_hash",
        "fingerprint",
    )
    values: dict[str, str] = {}
    for field in text_fields:
        value = data.get(field)
        if type(value) is not str:
            raise ApprovalError("discovery.approval.invalid")
        values[field] = value
    group_ids = _strings("group_ids")
    attestation_fingerprints = _strings("attestation_fingerprints")
    base_hashes = _hashes("base_hashes")
    session_hashes = _hashes("session_hashes")
    batch = PreparedBatch(
        schema_version=schema,
        session_id=values["session_id"],
        proposal_id=values["proposal_id"],
        group_ids=group_ids,
        attestation_fingerprints=attestation_fingerprints,
        base_hashes=base_hashes,
        candidate_fingerprint=values["candidate_fingerprint"],
        verification_fingerprint=values["verification_fingerprint"],
        approved_summary_hash=values["approved_summary_hash"],
        session_hashes=session_hashes,
        fingerprint=values["fingerprint"],
    )
    expected = fingerprint(
        {
            "schema_version": batch.schema_version,
            "proposal_id": batch.proposal_id,
            "group_ids": list(batch.group_ids),
            "attestation_fingerprints": list(batch.attestation_fingerprints),
            "base_hashes": dict(batch.base_hashes),
            "candidate_fingerprint": batch.candidate_fingerprint,
            "verification_fingerprint": batch.verification_fingerprint,
            "session_hashes": dict(batch.session_hashes),
            "approved_summary_hash": batch.approved_summary_hash,
        }
    )
    if expected != batch.fingerprint:
        raise ApprovalError("discovery.approval.fingerprint_changed")
    return batch


def _handle_proposal_attest_apply(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    proposal_id = _validate_proposal_id(args.proposal)
    batch_path = _proposal_root(session_dir) / proposal_id / _BATCH_FILENAME
    batch = _prepared_batch_from_mapping(_read_approval_json(batch_path))
    session_store = SessionStore.open(session_dir)
    attestation = attest_apply_batch(
        batch=batch,
        approver=normalize_actor_identity(args.approver),
        current_approver=session_store.charter.approver,
        timestamp=_approval_timestamp(args.timestamp),
        prepared_batch_hash=args.batch or batch.fingerprint,
    )
    path = _proposal_root(session_dir) / proposal_id / _APPLY_ATTESTATION_FILENAME
    _write_approval_json(path, attestation.to_dict())
    _emit_json(
        {
            "proposal_id": proposal_id,
            "batch_hash": attestation.batch_hash,
            "fingerprint": attestation.fingerprint,
            "path": _workspace_rel(session_id)
            + f"/{_PROPOSAL_REL}/{proposal_id}/{_APPLY_ATTESTATION_FILENAME}",
        }
    )
    return EXIT_OK


def _handle_proposal_export_preview(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    proposal_id = _validate_proposal_id(args.proposal)
    batch = _prepared_batch_from_mapping(
        _read_approval_json(_proposal_root(session_dir) / proposal_id / _BATCH_FILENAME)
    )
    apply_data = _read_approval_json(
        _proposal_root(session_dir) / proposal_id / _APPLY_ATTESTATION_FILENAME
    )
    required_apply = (
        "schema_version",
        "session_id",
        "batch_hash",
        "approver",
        "not_a_signature",
        "timestamp",
        "fingerprint",
    )
    if (
        not all(key in apply_data for key in required_apply)
        or not all(
            type(apply_data[key]) is str
            for key in required_apply
            if key != "schema_version"
        )
        or type(apply_data["schema_version"]) is not int
    ):
        raise ApprovalError("discovery.approval.invalid")
    apply_attestation = ApplyBatchAttestation(
        schema_version=cast(int, apply_data["schema_version"]),
        session_id=cast(str, apply_data["session_id"]),
        batch_hash=cast(str, apply_data["batch_hash"]),
        approver=cast(str, apply_data["approver"]),
        not_a_signature=cast(str, apply_data["not_a_signature"]),
        timestamp=cast(str, apply_data["timestamp"]),
        fingerprint=cast(str, apply_data["fingerprint"]),
    )
    if apply_attestation.batch_hash != batch.fingerprint:
        raise ApprovalError("discovery.approval.batch_hash")
    expected_apply_fp = fingerprint(
        {
            "schema_version": apply_attestation.schema_version,
            "session_id": apply_attestation.session_id,
            "batch_hash": apply_attestation.batch_hash,
            "approver": apply_attestation.approver,
            "not_a_signature": apply_attestation.not_a_signature,
            "timestamp": apply_attestation.timestamp,
        }
    )
    if expected_apply_fp != apply_attestation.fingerprint:
        raise ApprovalError("discovery.approval.fingerprint_changed")
    (
        proposal,
        candidate,
        bundle,
        base_text,
        base_hashes,
        session_hashes,
        session_store,
    ) = _approval_context(project, session_dir, proposal_id, batch.group_ids)
    if any(
        batch.base_hashes.get(key) != base_hashes.get(key) for key in batch.base_hashes
    ):
        raise ApprovalError("discovery.approval.fingerprint_changed")
    if (
        apply_attestation.session_id != session_id
        or apply_attestation.approver != session_store.charter.approver
    ):
        raise ApprovalError("discovery.approval.actor_mismatch")
    if (
        candidate.fingerprint != batch.candidate_fingerprint
        or bundle.fingerprint != batch.verification_fingerprint
    ):
        raise ApprovalError("discovery.approval.fingerprint_changed")
    if any(
        batch.session_hashes.get(key) != session_hashes.get(key)
        for key in batch.session_hashes
    ):
        raise ApprovalError("discovery.approval.fingerprint_changed")
    attestations = {
        gid: GroupAttestation.from_mapping(
            _read_approval_json(
                _proposal_root(session_dir)
                / proposal_id
                / _APPROVALS_DIR
                / f"{gid}.json"
            )
        )
        for gid in batch.group_ids
    }
    for index, gid in enumerate(batch.group_ids):
        attestation = attestations[gid]
        if attestation.fingerprint != batch.attestation_fingerprints[index]:
            raise ApprovalError("discovery.approval.fingerprint_changed")
        group = next(group for group in proposal.groups if group.group_id == gid)
        validate_group_attestation(
            attestation,
            current_input_hashes=session_hashes,
            current_group_fingerprint=fingerprint(group.to_dict()),
            current_candidate_fingerprint=candidate.fingerprint,
            current_verification_fingerprint=bundle.fingerprint,
        )
    summary = render_approved_summary(
        proposal=proposal,
        group_ids=batch.group_ids,
        candidate=candidate,
        verification=bundle,
        group_attestations=attestations,
        apply_attestation=apply_attestation,
        base_hashes=base_hashes,
        base_catalog_text=base_text,
        evidence_lock=_current_evidence_lock(session_dir),
        decision_statement="Approved dependency batch.",
    )
    write_approved_summary_preview(summary, session_dir, batch.fingerprint)
    _emit_json(
        {
            "proposal_id": proposal_id,
            "batch_hash": batch.fingerprint,
            "summary_fingerprint": summary.fingerprint,
            "workspace": _workspace_rel(session_id) + f"/exports/{batch.fingerprint}",
        }
    )
    return EXIT_OK


def _pending_transaction(project: Path) -> bool:
    root = project / ".selayer" / "discovery" / "transactions"
    if not root.exists():
        return False
    for directory in root.iterdir():
        journal = directory / "journal.json"
        if not journal.is_file():
            continue
        try:
            data = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if isinstance(data, Mapping) and data.get("state") not in {
            "completed",
            "rolled_back",
        }:
            return True
    return False


def _load_apply_attestation(path: Path) -> ApplyBatchAttestation:
    data = _read_approval_json(path)
    required = (
        "schema_version",
        "session_id",
        "batch_hash",
        "approver",
        "not_a_signature",
        "timestamp",
        "fingerprint",
    )
    if (
        not all(key in data for key in required)
        or type(data.get("schema_version")) is not int
    ):
        raise ApprovalError("discovery.approval.invalid")
    if not all(type(data[key]) is str for key in required if key != "schema_version"):
        raise ApprovalError("discovery.approval.invalid")
    return ApplyBatchAttestation(
        schema_version=cast(int, data["schema_version"]),
        session_id=cast(str, data["session_id"]),
        batch_hash=cast(str, data["batch_hash"]),
        approver=cast(str, data["approver"]),
        not_a_signature=cast(str, data["not_a_signature"]),
        timestamp=cast(str, data["timestamp"]),
        fingerprint=cast(str, data["fingerprint"]),
    )


def _handle_proposal_apply(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    if _pending_transaction(project):
        raise _CliError("discovery.transaction.recovery_required")
    proposal_id = _validate_proposal_id(args.proposal)
    proposal_dir = _proposal_root(session_dir) / proposal_id
    batch = _prepared_batch_from_mapping(
        _read_approval_json(proposal_dir / _BATCH_FILENAME)
    )
    apply_attestation = _load_apply_attestation(
        proposal_dir / _APPLY_ATTESTATION_FILENAME
    )
    (
        proposal,
        candidate,
        bundle,
        base_text,
        base_hashes,
        session_hashes,
        session_store,
    ) = _approval_context(project, session_dir, proposal_id, batch.group_ids)
    expected_apply_fp = fingerprint(
        {
            "schema_version": apply_attestation.schema_version,
            "session_id": apply_attestation.session_id,
            "batch_hash": apply_attestation.batch_hash,
            "approver": apply_attestation.approver,
            "not_a_signature": apply_attestation.not_a_signature,
            "timestamp": apply_attestation.timestamp,
        }
    )
    if expected_apply_fp != apply_attestation.fingerprint:
        raise ApprovalError("discovery.approval.fingerprint_changed")
    if (
        apply_attestation.batch_hash != batch.fingerprint
        or apply_attestation.session_id != session_id
        or apply_attestation.approver != session_store.charter.approver
    ):
        raise ApprovalError("discovery.approval.fingerprint_changed")
    if (
        candidate.fingerprint != batch.candidate_fingerprint
        or bundle.fingerprint != batch.verification_fingerprint
    ):
        raise ApprovalError("discovery.approval.fingerprint_changed")
    if any(
        batch.base_hashes.get(key) != base_hashes.get(key) for key in batch.base_hashes
    ) or any(
        batch.session_hashes.get(key) != session_hashes.get(key)
        for key in batch.session_hashes
    ):
        raise ApprovalError("discovery.approval.fingerprint_changed")
    files: dict[str, bytes] = {}
    catalog_rel = session_store.charter.catalog_path
    files[catalog_rel] = candidate.catalog_text.encode("utf-8")
    for operation in proposal.operations:
        kind = getattr(operation.kind, "value", str(operation.kind))
        if kind == "reference.create" or kind == "reference.update":
            text = dict(candidate.references).get(operation.target_id)
            if text is not None:
                files[f"references/{operation.target_id}"] = text.encode("utf-8")
        elif kind == "overlay.create" or kind == "overlay.update":
            text = dict(candidate.overlays).get(operation.target_id)
            if text is not None:
                files[f"okf_overlays/{operation.target_id}"] = text.encode("utf-8")
    group_attestations = {
        gid: GroupAttestation.from_mapping(
            _read_approval_json(proposal_dir / _APPROVALS_DIR / f"{gid}.json")
        )
        for gid in batch.group_ids
    }
    for index, gid in enumerate(batch.group_ids):
        attestation = group_attestations[gid]
        if attestation.fingerprint != batch.attestation_fingerprints[index]:
            raise ApprovalError("discovery.approval.fingerprint_changed")
        group = next(group for group in proposal.groups if group.group_id == gid)
        validate_group_attestation(
            attestation,
            current_input_hashes=session_hashes,
            current_group_fingerprint=fingerprint(group.to_dict()),
            current_candidate_fingerprint=candidate.fingerprint,
            current_verification_fingerprint=bundle.fingerprint,
        )
    evidence_lock = _current_evidence_lock(session_dir)
    expected_summary_hash = compute_approved_summary_hash(
        proposal=proposal,
        group_ids=batch.group_ids,
        candidate=candidate,
        verification=bundle,
        base_hashes=base_hashes,
        base_catalog_text=base_text,
        evidence_lock=evidence_lock,
        decision_statement="Approved dependency batch.",
    )
    if expected_summary_hash != batch.approved_summary_hash:
        raise ApprovalError("discovery.approval.fingerprint_changed")
    prepared_now = prepare_apply_batch(
        proposal=proposal,
        group_ids=batch.group_ids,
        attestations=group_attestations,
        combined_candidate=candidate,
        combined_verification=bundle,
        base_hashes=base_hashes,
        current_session_hashes=session_hashes,
        approved_summary_hash=expected_summary_hash,
    )
    if prepared_now.fingerprint != batch.fingerprint:
        raise ApprovalError("discovery.approval.fingerprint_changed")
    summary = render_approved_summary(
        proposal=proposal,
        group_ids=batch.group_ids,
        candidate=candidate,
        verification=bundle,
        group_attestations=group_attestations,
        apply_attestation=apply_attestation,
        base_hashes=base_hashes,
        base_catalog_text=base_text,
        evidence_lock=evidence_lock,
        decision_statement="Approved dependency batch.",
    )
    summary_root = Path("semantic_changes") / proposal_id / batch.fingerprint
    for entry in summary.entries:
        files[(summary_root / entry.path).as_posix()] = entry.text.encode("utf-8")
    transaction_id = f"apply-{batch.fingerprint[:16]}"
    journal = ApplyJournal.create(
        project_root=project,
        transaction_root=project / ".selayer" / "discovery" / "transactions",
        transaction_id=transaction_id,
        actor=args.approver,
        files=files,
    )
    journal.apply()
    try:
        session_store.record_artifact(
            "base_catalog",
            content_hash=fingerprint(candidate.catalog_text),
            actor=args.approver,
        )
    except SessionError:
        journal.rollback()
        raise
    _emit_json(
        {
            "proposal_id": proposal_id,
            "batch_hash": batch.fingerprint,
            "transaction_id": transaction_id,
            "files": sorted(files),
        }
    )
    return EXIT_OK


def _handle_recover(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    recovered = recover_transactions(
        project / ".selayer" / "discovery" / "transactions",
        project_root=project,
    )
    _emit_json({"recovered": list(recovered)})
    return EXIT_OK


def _handle_proposal_verify(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    proposal_id = (
        _validate_proposal_id(args.proposal)
        if args.proposal
        else _latest_proposal_id(session_dir)
    )
    record = _read_proposal_record(session_dir, proposal_id)
    # Reconstruct a fresh candidate from the stored typed operations so the
    # verification verdict reflects the current base catalog, authored
    # knowledge roots, and stored operations — never a stale candidate.
    proposal = build_proposal(
        {
            "proposal_id": record.get("proposal_id"),
            "title": record.get("title"),
            "groups": record.get("groups", []),
        }
    )
    base_catalog_text = _session_base_catalog_text(project, session_dir)
    base_references, base_overlays = _authored_knowledge_bases(project)
    candidate = reconstruct_candidate(
        base_catalog_text=base_catalog_text,
        base_references=base_references,
        base_overlays=base_overlays,
        operations=proposal.operations,
    )
    proposal_dir = _proposal_root(session_dir) / proposal.proposal_id
    # Materialize the reconstructed candidate inside the Git-ignored workspace
    # and strict-load it so the physical, compatibility, and OKF checks run
    # against the exact catalog that apply would produce.
    candidate_scratch = proposal_dir / "verify-candidate"
    candidate_path = write_candidate(candidate, candidate_scratch)
    candidate_layer = SemanticLayer.load(candidate_path)
    # Open the session stores for gate, claim, and conflict readiness.
    session_store = SessionStore.open(session_dir)
    evidence_store = EvidenceStore.create(_evidence_root(session_dir))
    claim_store = ClaimStore.create(session_store, evidence_store)
    interview_store = InterviewStore.create(session_store)
    okf_output_dir = proposal_dir / "verify-okf"
    bundle = verify_proposal(
        proposal=proposal,
        candidate=candidate,
        candidate_layer=candidate_layer,
        okf_output_dir=okf_output_dir,
        interview_store=interview_store,
        claim_store=claim_store,
        evidence_store=evidence_store,
    )
    # Persist an immutable, sorted, safe report bound to the input hashes. The
    # content is a pure function of the inputs, so a repeated run with
    # unchanged inputs writes identical bytes (idempotent); the atomic
    # replace never leaves a partial file.
    record_path = proposal_dir / _VERIFICATION_FILENAME
    tmp = record_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(bundle.to_dict(), handle, sort_keys=True)
    os.replace(tmp, record_path)
    _restrict_file(record_path)
    # Emit metadata only: stable ids, a content-addressed fingerprint, the
    # input hashes the verdict was derived from, and per-group mandatory
    # checks and readiness. Raw values, document bodies, SQL, and error text
    # never reach the terminal.
    _emit_json(
        {
            "proposal_id": bundle.proposal_id,
            "fingerprint": bundle.fingerprint,
            "input_hashes": dict(bundle.input_hashes),
            "groups": [
                {
                    "group_id": group.group_id,
                    "checks": [check.to_dict() for check in group.checks],
                    "ready": group.readiness.ready,
                }
                for group in bundle.groups
            ],
        }
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Sample-policy helpers                                                       #
# --------------------------------------------------------------------------- #

#: Policy artifacts (session salt, context export) live inside the Git-ignored
#: session workspace and never surface in Git-visible output. The salt itself
#: is never rendered; only its SHA-256 identifier (``salt_id``) appears in a
#: proposed policy.
_POLICY_REL = "policy"
_SALT_NAME = "salt"
_CONTEXT_PREFIX = "context"
_ACTIVATION_PREFIX = "activation"

#: A single safe filesystem path component (letters, digits, dot, hyphen,
#: underscore). Used to derive an export filename from a source id without
#: ever trusting it as a raw path.
_SAFE_FILE_RE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._-]")


def _policy_dir(session_dir: Path) -> Path:
    """Return the policy artifact directory for a session."""

    return session_dir / _POLICY_REL


def _restrict_file(path: Path) -> None:
    """Apply owner-only permissions on POSIX (best-effort elsewhere)."""

    if os.name == "posix":
        os.chmod(path, 0o600)


def _salt_path(session_dir: Path) -> Path:
    """Return the session salt path (inside the ignored workspace)."""

    return _policy_dir(session_dir) / _SALT_NAME


def _load_or_create_salt(session_dir: Path) -> bytes:
    """Load the session salt, generating and persisting it on first use.

    The salt stays in the Git-ignored workspace; only its SHA-256 identifier
    ever appears in policy output. Created atomically with owner-only
    permissions so a concurrent reader never observes a partial write.
    """

    path = _salt_path(session_dir)
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    _restrict_file(path)
    return salt


def _load_salt(session_dir: Path) -> bytes:
    """Load an existing session salt (errors if absent)."""

    path = _salt_path(session_dir)
    if not path.exists():
        raise _CliError(CODE_PROFILE_SALT) from None
    return path.read_bytes()


def _safe_file_name(value: str) -> str:
    """Return a filesystem-safe single path component from ``value``."""

    cleaned = _SAFE_FILE_RE.sub("-", value).strip("-")[:128]
    return cleaned or "source"


def _activation_path(session_dir: Path, source_id: str) -> Path:
    """Return the owner-only activation artifact path for one source.

    The activation binds the activated policy to its exact profile/schema/
    session/approver inputs; ``export-context`` loads and re-verifies it so a
    missing or changed binding fails closed.
    """

    return (
        _policy_dir(session_dir)
        / f"{_ACTIVATION_PREFIX}-{_safe_file_name(source_id)}.json"
    )


def _write_activation(
    session_dir: Path, source_id: str, activation: PolicyActivation
) -> Path:
    """Persist an activation artifact atomically with owner-only permissions."""

    path = _activation_path(session_dir, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(activation.to_dict(), sort_keys=True).encode("utf-8"))
    finally:
        os.close(fd)
    _restrict_file(path)
    return path


def _load_activation(session_dir: Path, source_id: str) -> PolicyActivation | None:
    """Load a persisted activation for ``source_id`` (``None`` if absent)."""

    path = _activation_path(session_dir, source_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise _CliError(CODE_PROFILE_LOAD) from None
    if not isinstance(data, Mapping):
        raise _CliError(CODE_PROFILE_LOAD) from None
    try:
        return PolicyActivation.from_dict(dict(data))
    except ProfilePolicyError:
        raise _CliError(CODE_PROFILE_LOAD) from None


def _normalized_approver(session_dir: Path, override: str | None) -> str:
    """Return the normalized current approver (override or charter approver)."""

    charter = SessionStore.open(session_dir).charter
    raw = override if override else charter.approver
    return normalize_actor_identity(raw)


def _next_context_export_path(session_dir: Path, source_id: str) -> Path:
    """Return a distinct owner-only context export path for one source.

    Each successful export is written to a distinct path so prior exports are
    never overwritten and the cumulative session byte accounting always
    retains every prior export. The path embeds a safe, monotonically
    increasing 1-based sequence number scoped to the (sanitized) source id;
    no raw value is ever used in the path. The sequence is derived by scanning
    the existing context artifacts for the source, so it is deterministic for
    a given workspace state.
    """

    safe = _safe_file_name(source_id)
    prefix = f"{_CONTEXT_PREFIX}-{safe}-"
    policy_dir = _policy_dir(session_dir)
    next_seq = 1
    if policy_dir.exists():
        for entry in policy_dir.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if not name.startswith(prefix) or not name.endswith(".json"):
                continue
            suffix = name[len(prefix) : -len(".json")]
            try:
                seq = int(suffix)
            except ValueError:
                continue
            if seq >= next_seq:
                next_seq = seq + 1
    return policy_dir / f"{prefix}{next_seq:06d}.json"


def _session_context_bytes_used(session_dir: Path) -> int:
    """Sum the bytes of every persisted context export in the session.

    Derives prior session export usage from the Git-ignored workspace so the
    ``bytes_per_session`` cap is enforced across multiple exports. Best-effort:
    a corrupt or unreadable file contributes zero (it cannot inflate the cap).
    """

    policy_dir = _policy_dir(session_dir)
    if not policy_dir.exists():
        return 0
    total = 0
    for entry in policy_dir.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.startswith(f"{_CONTEXT_PREFIX}-") or not entry.name.endswith(
            ".json"
        ):
            continue
        try:
            with entry.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, Mapping):
            raw = data.get("bytes")
            if type(raw) is int:
                total += raw
    return total


def _load_json_mapping(project: Path, raw_path: str, *, kind: str) -> dict[str, object]:
    """Load a JSON mapping from a project-contained path (never leaks content)."""

    abs_path = _assert_contained(project, Path(raw_path), kind=kind)
    try:
        with abs_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise _CliError(CODE_PROFILE_LOAD) from None
    if not isinstance(data, Mapping):
        raise _CliError(CODE_PROFILE_LOAD) from None
    return dict(data)


def _load_profile_file(project: Path, raw_path: str) -> SourceProfile:
    """Load a :class:`SourceProfile` from a project-contained JSON file."""

    data = _load_json_mapping(project, raw_path, kind="profile")
    try:
        return SourceProfile.from_dict(data)
    except ProfilePolicyError:
        raise _CliError(CODE_PROFILE_LOAD) from None


def _load_policy_file(project: Path, raw_path: str) -> SamplePolicy:
    """Load a :class:`SamplePolicy` from a project-contained JSON file."""

    data = _load_json_mapping(project, raw_path, kind="policy")
    try:
        return SamplePolicy.from_dict(data)
    except ProfilePolicyError:
        raise _CliError(CODE_PROFILE_LOAD) from None


def _open_registry(
    project: Path, session_dir: Path
) -> tuple[SemanticLayer, duckdb.DuckDBPyConnection, SourceRegistry]:
    """Open the source catalog and registry for a session."""

    charter = SessionStore.open(session_dir).charter
    catalog_abs = _assert_contained(
        project, project / charter.catalog_path, kind="catalog"
    )
    layer = SemanticLayer.load(catalog_abs)
    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        layer,
        connection,
        MappingProfileResolver({}),
        MappingArrowProviderResolver({}),
    )
    return layer, connection, registry


# --------------------------------------------------------------------------- #
# Sample-policy command handlers                                             #
# --------------------------------------------------------------------------- #


def _handle_profile_propose_policy(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    profile = _load_profile_file(project, args.profile)
    grain = tuple(args.grain)
    salt = _load_or_create_salt(session_dir)
    salt_id = hashlib.sha256(salt).hexdigest()
    policy, classifications = propose_policy(profile, grain, salt_id=salt_id)
    payload = policy.to_dict()
    payload["classifications"] = [item.to_dict() for item in classifications]
    _emit_json(payload)
    return EXIT_OK


def _handle_profile_activate_policy(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    profile = _load_profile_file(project, args.profile)
    policy = _load_policy_file(project, args.policy)
    charter = SessionStore.open(session_dir).charter
    charter_approver = normalize_actor_identity(charter.approver)
    override = args.approver
    if override is not None:
        # Global constraints require every approval actor to match the
        # charter's normalized named approver. Export verifies the charter
        # approver and has no override, so a non-matching activation could
        # never be used; reject it up front with the stable actor-mismatch
        # diagnostic. A blank/whitespace override is also rejected: it is an
        # explicit override (``--approver ""``), never the omitted default
        # (``None``), so it fails closed instead of silently falling back.
        try:
            candidate = normalize_actor_identity(override)
        except DiscoveryError:
            raise ProfilePolicyError(
                CODE_PROFILE_ACTOR, safe_detail="approver"
            ) from None
        if candidate != charter_approver:
            raise ProfilePolicyError(
                CODE_PROFILE_ACTOR, safe_detail="approver"
            ) from None
        approver = candidate
    else:
        approver = charter_approver
    activated_at = args.activated_at or ""
    activation = activate_policy(
        policy,
        profile,
        session_id=session_id,
        approver=approver,
        activated_at=activated_at,
    )
    # Persist a safe activation artifact under the ignored session policy
    # directory so export-context can load and re-verify the current bindings.
    _write_activation(session_dir, profile.source_id, activation)
    _emit_json(activation.to_dict())
    return EXIT_OK


def _handle_profile_export_context(args: argparse.Namespace) -> int:
    project = _project_root(args.project)
    project_abs = project.resolve()
    session_id = _validate_session_id(args.session_id)
    session_dir = _require_session(project, session_id)
    profile = _load_profile_file(project, args.profile)
    policy = _load_policy_file(project, args.policy)
    salt = _load_salt(session_dir)
    # Fail closed when no activation artifact exists for this source, or when
    # any current binding (normalized charter approver, session/source ids,
    # policy/profile/schema fingerprints, snapshot, grain) no longer matches the
    # persisted activation.
    activation = _load_activation(session_dir, args.source_id)
    if activation is None:
        raise ProfilePolicyError(CODE_PROFILE_STALE, safe_detail="activation") from None
    approver = _normalized_approver(session_dir, None)
    verify_activation(
        activation,
        policy,
        profile,
        session_id=session_id,
        source_id=args.source_id,
        approver=approver,
    )
    # Enforce the session cap across multiple exports by summing the bytes of
    # every prior context export persisted in the ignored session workspace.
    session_bytes_used = _session_context_bytes_used(session_dir)
    _, _, registry = _open_registry(project, session_dir)
    try:
        with registry.open_scan_session(
            args.source_id,
            columns=tuple(args.columns),
            batch_size=args.batch_size,
        ) as session:
            export = build_context_export(
                session,
                policy,
                profile,
                salt,
                session_id=session_id,
                session_bytes_used=session_bytes_used,
            )
    finally:
        registry.close()
    out_path = _next_context_export_path(session_dir, args.source_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(export.to_dict(), sort_keys=True).encode("utf-8"))
    finally:
        os.close(fd)
    _restrict_file(out_path)
    # Emit only a safe path plus binding fingerprints/hashes. No source/session
    # identifiers, counts, byte count, or canary status reach stdout.
    _emit_json(
        {
            "path": out_path.relative_to(project_abs).as_posix(),
            "fingerprint": export.fingerprint,
            "policy_fingerprint": export.policy_fingerprint,
            "profile_fingerprint": export.profile_fingerprint,
            "schema_fingerprint": export.schema_fingerprint,
        }
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Argument parser                                                             #
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="selayer-discovery",
        description="Deterministic agent-assisted semantic discovery for selayer.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    session_parser = subparsers.add_parser("session", help="Manage discovery sessions.")
    session_sub = session_parser.add_subparsers(dest="session_command", required=True)

    init_parser = session_sub.add_parser(
        "init", help="Initialize a new discovery session."
    )
    init_parser.add_argument("--charter", required=True, help="Charter YAML path.")
    init_parser.add_argument("--project", help="Project root (default: cwd).")
    init_parser.add_argument(
        "--session-id", dest="session_id", help="Explicit session id."
    )
    init_parser.add_argument(
        "--catalog-path",
        dest="catalog_path",
        required=True,
        help="Catalog file path (must be project-contained).",
    )
    init_parser.add_argument(
        "--summary-root",
        dest="summary_root",
        help="Approved-summary root (must be project-contained).",
    )
    init_parser.add_argument("--actor", help="Actor identity (default: approver).")
    init_parser.set_defaults(func=_handle_init)

    status_parser = session_sub.add_parser(
        "status", help="Rebuild and report session status from events."
    )
    status_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    status_parser.add_argument("--project", help="Project root (default: cwd).")
    status_parser.set_defaults(func=_handle_status)

    close_parser = session_sub.add_parser(
        "close", help="Close a discovery session (terminal)."
    )
    close_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    close_parser.add_argument("--project", help="Project root (default: cwd).")
    close_parser.add_argument("--actor", help="Actor identity (default: approver).")
    close_parser.set_defaults(func=_handle_close)

    intake_parser = subparsers.add_parser(
        "intake", help="Capture normalized evidence into a session."
    )
    intake_sub = intake_parser.add_subparsers(dest="intake_command", required=True)

    add_document_parser = intake_sub.add_parser(
        "add-document", help="Ingest a Markdown or plain-text document."
    )
    add_document_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    add_document_parser.add_argument("--project", help="Project root (default: cwd).")
    add_document_parser.add_argument(
        "--path", required=True, help="Document path (must be project-contained)."
    )
    add_document_parser.add_argument(
        "--source",
        help="Source label (default: project-relative POSIX path).",
    )
    add_document_parser.set_defaults(func=_handle_intake_add_document)

    provider_parser = intake_sub.add_parser(
        "add-provider", help="Register a read-only knowledge provider type."
    )
    provider_parser.add_argument("--session-id", required=True)
    provider_parser.add_argument("--project", help="Project root (default: cwd).")
    provider_parser.add_argument(
        "--name", required=True, help="Configured provider name."
    )
    provider_parser.add_argument("--type", dest="provider_type", required=True)
    provider_parser.add_argument(
        "--root", help="Contained root for filesystem providers."
    )
    provider_parser.add_argument("--option", action="append", default=[])
    provider_parser.add_argument("--env", action="append", default=[])
    provider_parser.add_argument("--actor")
    provider_parser.set_defaults(func=_handle_intake_add_provider)

    snapshot_parser = intake_sub.add_parser(
        "snapshot", help="Store normalized text content read from stdin."
    )
    snapshot_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    snapshot_parser.add_argument("--project", help="Project root (default: cwd).")
    snapshot_parser.add_argument("--source", help="Source label for the snapshot.")
    snapshot_parser.add_argument(
        "--provider", help="Provider name for a provider snapshot."
    )
    snapshot_parser.add_argument(
        "--resource-id", help="Namespaced provider resource id."
    )
    snapshot_parser.add_argument("--revision", help="Immutable provider revision hash.")
    snapshot_parser.add_argument("--selector", help="Bounded provider selector.")
    snapshot_parser.add_argument("--actor")
    snapshot_parser.add_argument(
        "--media-type",
        dest="media_type",
        required=True,
        help="Media type (text/markdown or text/plain).",
    )
    snapshot_parser.set_defaults(func=_handle_intake_snapshot)

    interview_parser = subparsers.add_parser(
        "interview", help="Record adaptive interview gates and corrections."
    )
    interview_sub = interview_parser.add_subparsers(
        dest="interview_command", required=True
    )

    ask_parser = interview_sub.add_parser(
        "ask", help="Open a question citing one gate and motivating evidence."
    )
    ask_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    ask_parser.add_argument("--project", help="Project root (default: cwd).")
    ask_parser.add_argument(
        "--question", required=True, help="Question JSON file (project-contained)."
    )
    ask_parser.add_argument("--actor", help="Actor identity (default: approver).")
    ask_parser.set_defaults(func=_handle_interview_ask)

    answer_parser = interview_sub.add_parser(
        "answer", help="Record an answer to a gate and dispose it as answered."
    )
    answer_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    answer_parser.add_argument("--project", help="Project root (default: cwd).")
    answer_parser.add_argument(
        "--answer", required=True, help="Answer JSON file (project-contained)."
    )
    answer_parser.add_argument("--actor", help="Actor identity (default: approver).")
    answer_parser.set_defaults(func=_handle_interview_answer)

    correct_parser = interview_sub.add_parser(
        "correct", help="Supersede a current answer with a typed correction."
    )
    correct_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    correct_parser.add_argument("--project", help="Project root (default: cwd).")
    correct_parser.add_argument(
        "--correction",
        required=True,
        help="Correction JSON file (project-contained).",
    )
    correct_parser.add_argument("--actor", help="Actor identity (default: approver).")
    correct_parser.set_defaults(func=_handle_interview_correct)

    set_gate_parser = interview_sub.add_parser(
        "set-gate", help="Record a terminal gate disposition."
    )
    set_gate_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    set_gate_parser.add_argument("--project", help="Project root (default: cwd).")
    set_gate_parser.add_argument("--gate", required=True, help="Gate id.")
    set_gate_parser.add_argument(
        "--disposition",
        required=True,
        help="Disposition JSON file (project-contained).",
    )
    set_gate_parser.add_argument("--actor", help="Actor identity (default: approver).")
    set_gate_parser.set_defaults(func=_handle_interview_set_gate)

    evidence_parser = subparsers.add_parser(
        "evidence", help="Record typed evidence claims and conflicts."
    )
    evidence_sub = evidence_parser.add_subparsers(
        dest="evidence_command", required=True
    )

    add_claim_parser = evidence_sub.add_parser(
        "add-claim", help="Record a typed, evidence-backed claim."
    )
    add_claim_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    add_claim_parser.add_argument("--project", help="Project root (default: cwd).")
    add_claim_parser.add_argument(
        "--claim", required=True, help="Claim JSON file (project-contained)."
    )
    add_claim_parser.add_argument("--actor", help="Actor identity (default: approver).")
    add_claim_parser.set_defaults(func=_handle_evidence_add_claim)

    add_conflict_parser = evidence_sub.add_parser(
        "add-conflict", help="Record an unresolved evidence conflict."
    )
    add_conflict_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    add_conflict_parser.add_argument("--project", help="Project root (default: cwd).")
    add_conflict_parser.add_argument(
        "--conflict", required=True, help="Conflict JSON file (project-contained)."
    )
    add_conflict_parser.add_argument(
        "--actor", help="Actor identity (default: approver)."
    )
    add_conflict_parser.set_defaults(func=_handle_evidence_add_conflict)

    resolve_conflict_parser = evidence_sub.add_parser(
        "resolve-conflict", help="Resolve an evidence conflict."
    )
    resolve_conflict_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    resolve_conflict_parser.add_argument(
        "--project", help="Project root (default: cwd)."
    )
    resolve_conflict_parser.add_argument(
        "--resolution",
        required=True,
        help="Resolution JSON file (project-contained).",
    )
    resolve_conflict_parser.add_argument(
        "--actor", help="Actor identity (default: approver)."
    )
    resolve_conflict_parser.set_defaults(func=_handle_evidence_resolve_conflict)

    profile_parser = subparsers.add_parser(
        "profile", help="Profile a bounded source scan."
    )
    profile_sub = profile_parser.add_subparsers(dest="profile_command", required=True)
    scan_parser = profile_sub.add_parser(
        "scan", help="Compute exact aggregate profile metadata."
    )
    scan_parser.add_argument("--session-id", required=True)
    scan_parser.add_argument("--source-id", required=True)
    scan_parser.add_argument("--project", help="Project root (default: cwd).")
    scan_parser.add_argument("--timeout", type=float, default=900.0)
    scan_parser.add_argument("--batch-size", type=int, default=1024)
    scan_parser.add_argument("--grain", action="append", default=[])
    scan_parser.add_argument("--columns", action="append", default=[])
    scan_parser.set_defaults(func=_handle_profile_scan)

    propose_parser = profile_sub.add_parser(
        "propose-policy", help="Propose a conservative sample policy."
    )
    propose_parser.add_argument("--session-id", required=True)
    propose_parser.add_argument("--project", help="Project root (default: cwd).")
    propose_parser.add_argument("--profile", required=True)
    propose_parser.add_argument("--grain", action="append", default=[])
    propose_parser.set_defaults(func=_handle_profile_propose_policy)

    activate_parser = profile_sub.add_parser(
        "activate-policy", help="Activate a named sample policy."
    )
    activate_parser.add_argument("--session-id", required=True)
    activate_parser.add_argument("--project", help="Project root (default: cwd).")
    activate_parser.add_argument("--profile", required=True)
    activate_parser.add_argument("--policy", required=True)
    activate_parser.add_argument("--approver")
    activate_parser.add_argument("--activated-at", default="")
    activate_parser.set_defaults(func=_handle_profile_activate_policy)

    export_parser = profile_sub.add_parser(
        "export-context", help="Export bounded transformed context metadata."
    )
    export_parser.add_argument("--session-id", required=True)
    export_parser.add_argument("--source-id", required=True)
    export_parser.add_argument("--project", help="Project root (default: cwd).")
    export_parser.add_argument("--profile", required=True)
    export_parser.add_argument("--policy", required=True)
    export_parser.add_argument("--timeout", type=float, default=900.0)
    export_parser.add_argument("--batch-size", type=int, default=1024)
    export_parser.add_argument("--columns", action="append", default=[])
    export_parser.set_defaults(func=_handle_profile_export_context)

    proposal_parser = subparsers.add_parser(
        "proposal", help="Import and review typed semantic proposals."
    )
    proposal_sub = proposal_parser.add_subparsers(
        dest="proposal_command", required=True
    )

    proposal_import_parser = proposal_sub.add_parser(
        "import",
        help="Import typed operations and reconstruct a candidate.",
    )
    proposal_import_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    proposal_import_parser.add_argument(
        "--project", help="Project root (default: cwd)."
    )
    proposal_import_parser.add_argument(
        "--proposal",
        required=True,
        help="Proposal YAML/JSON file (must be project-contained).",
    )
    proposal_import_parser.set_defaults(func=_handle_proposal_import)

    proposal_show_parser = proposal_sub.add_parser(
        "show",
        help="Render deterministic review previews for a proposal.",
    )
    proposal_show_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    proposal_show_parser.add_argument("--project", help="Project root (default: cwd).")
    proposal_show_parser.add_argument(
        "--proposal", dest="proposal", help="Proposal id (default: latest)."
    )
    proposal_show_parser.set_defaults(func=_handle_proposal_show)

    proposal_verify_parser = proposal_sub.add_parser(
        "verify",
        help="Verify proposal readiness and write a hash-bound report.",
    )
    proposal_verify_parser.add_argument(
        "--session-id", dest="session_id", required=True, help="Session id."
    )
    proposal_verify_parser.add_argument(
        "--project", help="Project root (default: cwd)."
    )
    proposal_verify_parser.add_argument(
        "--proposal", dest="proposal", help="Proposal id (default: latest)."
    )
    proposal_verify_parser.set_defaults(func=_handle_proposal_verify)

    proposal_attest_parser = proposal_sub.add_parser(
        "attest", help="Record a named group decision."
    )
    proposal_attest_parser.add_argument("--session-id", required=True)
    proposal_attest_parser.add_argument("--project")
    proposal_attest_parser.add_argument("--proposal", required=True)
    proposal_attest_parser.add_argument("--group", required=True)
    proposal_attest_parser.add_argument("--approver", required=True)
    proposal_attest_parser.add_argument(
        "--decision", choices=("accepted", "rejected", "deferred"), default="accepted"
    )
    proposal_attest_parser.add_argument("--reason", default="")
    proposal_attest_parser.add_argument("--timestamp")
    proposal_attest_parser.set_defaults(func=_handle_proposal_attest)

    proposal_prepare_parser = proposal_sub.add_parser(
        "prepare-apply", help="Prepare an explicit dependency-closed batch."
    )
    proposal_prepare_parser.add_argument("--session-id", required=True)
    proposal_prepare_parser.add_argument("--project")
    proposal_prepare_parser.add_argument("--proposal", required=True)
    proposal_prepare_parser.add_argument("--group", action="append", required=True)
    proposal_prepare_parser.set_defaults(func=_handle_proposal_prepare_apply)

    proposal_attest_apply_parser = proposal_sub.add_parser(
        "attest-apply", help="Attest an exact prepared batch."
    )
    proposal_attest_apply_parser.add_argument("--session-id", required=True)
    proposal_attest_apply_parser.add_argument("--project")
    proposal_attest_apply_parser.add_argument("--proposal", required=True)
    proposal_attest_apply_parser.add_argument("--approver", required=True)
    proposal_attest_apply_parser.add_argument("--batch")
    proposal_attest_apply_parser.add_argument("--timestamp")
    proposal_attest_apply_parser.set_defaults(func=_handle_proposal_attest_apply)

    proposal_export_parser = proposal_sub.add_parser(
        "export-preview", help="Write the safe ignored approved-summary preview."
    )
    proposal_export_parser.add_argument("--session-id", required=True)
    proposal_export_parser.add_argument("--project")
    proposal_export_parser.add_argument("--proposal", required=True)
    proposal_export_parser.set_defaults(func=_handle_proposal_export_preview)

    proposal_apply_parser = proposal_sub.add_parser(
        "apply", help="Apply one current, attested batch explicitly."
    )
    proposal_apply_parser.add_argument("--session-id", required=True)
    proposal_apply_parser.add_argument("--project")
    proposal_apply_parser.add_argument("--proposal", required=True)
    proposal_apply_parser.add_argument("--approver", required=True)
    proposal_apply_parser.set_defaults(func=_handle_proposal_apply)

    recover_parser = subparsers.add_parser(
        "recover", help="Recover or roll back a pending apply transaction."
    )
    recover_parser.add_argument("--project")
    recover_parser.set_defaults(func=_handle_recover)

    return parser


# --------------------------------------------------------------------------- #
# Entry points                                                                #
# --------------------------------------------------------------------------- #


def _run(handler: object, args: argparse.Namespace) -> int:
    """Invoke a handler, rendering safe diagnostics on any failure."""

    try:
        readonly = {
            _handle_recover,
            _handle_status,
            _handle_proposal_show,
            _handle_proposal_verify,
        }
        if handler not in readonly and _pending_transaction(
            _project_root(getattr(args, "project", None))
        ):
            raise _CliError("discovery.transaction.recovery_required")
        return handler(args)  # type: ignore[operator]
    except SystemExit:
        raise
    except (
        SessionError,
        EvidenceError,
        InterviewError,
        _CliError,
        KnowledgeError,
        ProfilePolicyError,
        DiscoveryError,
        ProposalError,
        ApprovalError,
        TransactionError,
        RecoveryConflict,
    ) as exc:
        _emit_error(exc)
        return EXIT_ERROR
    except Exception:
        if getattr(args, "debug", False):
            raise
        _emit_error(_CliError(CODE_INTERNAL))
        return EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    # parse_args raises SystemExit(0) for --help and SystemExit(2) for usage
    # errors; both propagate so callers observe the conventional codes.
    args = parser.parse_args(argv)
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    return _run(handler, args)


def run() -> None:
    raise SystemExit(main())
