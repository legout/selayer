from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import selayer_discovery.cli as cli_module
from ruamel.yaml import YAML
from selayer_discovery import __version__
from selayer_discovery.cli import main
from selayer_discovery.profiling import (
    PolicyActivation,
    ProfileRunner,
    SourceProfile,
)
from selayer_discovery.session import SessionStore

from selayer.sources.base import SourceConsistency
from selayer.sources.scan import SourceScanSession, SourceSnapshot
from selayer.sources.schema import schema_fingerprint, table_schema_from_arrow


def test_package_version_is_defined() -> None:
    assert __version__ == "0.1.0"


def test_cli_help_is_a_usage_exit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    assert "selayer-discovery" in capsys.readouterr().out


def test_root_import_does_not_import_discovery() -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import selayer; import sys; assert 'selayer_discovery' not in sys.modules",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stderr == ""


# --------------------------------------------------------------------------- #
# Session CLI tests (Task 8)                                                  #
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CHARTER: dict[str, Any] = {
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


def _write_charter(
    path: Path,
    *,
    remove: tuple[str, ...] = (),
    **overrides: Any,
) -> None:
    data = dict(_DEFAULT_CHARTER)
    data.update(overrides)
    for key in remove:
        data.pop(key, None)
    path.write_text(_dump_yaml(data), encoding="utf-8")


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / ".gitignore").write_text(
        ".selayer/discovery/sessions/\n.selayer/discovery/transactions/\n",
        encoding="utf-8",
    )


def _check_ignore(repo: Path, target: Path) -> bool:
    """Return True when ``target`` is Git-ignored within ``repo``."""

    result = subprocess.run(
        ["git", "check-ignore", str(target)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _init_session(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    session_id: str | None = None,
    charter_path: Path | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    if charter_path is None:
        charter_path = project / "charter.yaml"
    _write_charter(charter_path)
    args = [
        "session",
        "init",
        "--charter",
        str(charter_path),
        "--project",
        str(project),
        "--catalog-path",
        "catalogs/shopfloor.yaml",
    ]
    if session_id is not None:
        args += ["--session-id", session_id]
    if extra_args:
        args += extra_args
    assert main(args) == 0
    return json.loads(capsys.readouterr().out)


# --- Git visibility (real repository .gitignore) --------------------------- #


def test_real_repo_ignores_discovery_sessions() -> None:
    target = _REPO_ROOT / ".selayer" / "discovery" / "sessions" / "example"
    assert _check_ignore(_REPO_ROOT, target)


def test_real_repo_ignores_discovery_transactions() -> None:
    target = _REPO_ROOT / ".selayer" / "discovery" / "transactions" / "example"
    assert _check_ignore(_REPO_ROOT, target)


def test_real_repo_keeps_discovery_yaml_visible() -> None:
    target = _REPO_ROOT / ".selayer" / "discovery.yaml"
    assert not _check_ignore(_REPO_ROOT, target)


def test_real_repo_keeps_semantic_changes_visible() -> None:
    target = _REPO_ROOT / "semantic_changes" / "example.md"
    assert not _check_ignore(_REPO_ROOT, target)


# --- session init: workspace creation and Git visibility -------------------- #


def test_session_init_creates_ignored_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    out = _init_session(tmp_path, capsys)
    workspace = tmp_path / out["workspace"]
    assert workspace.is_dir()
    assert (workspace / "events.jsonl").is_file()
    assert _check_ignore(tmp_path, workspace)


def test_session_init_creates_single_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    _init_session(tmp_path, capsys)
    sessions_root = tmp_path / ".selayer" / "discovery" / "sessions"
    session_dirs = [p for p in sessions_root.iterdir() if p.is_dir()]
    assert len(session_dirs) == 1


def test_session_init_auto_generates_session_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    out = _init_session(tmp_path, capsys)
    assert out["session_id"]
    assert out["state"] == "initialized"
    assert out["charter_fingerprint"]


def test_session_init_uses_explicit_cli_session_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    out = _init_session(tmp_path, capsys, session_id="session-explicit-001")
    assert out["session_id"] == "session-explicit-001"


def test_session_init_uses_charter_session_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    _write_charter(charter_path, session_id="session-charter-001")
    assert (
        main(
            [
                "session",
                "init",
                "--charter",
                str(charter_path),
                "--project",
                str(tmp_path),
                "--catalog-path",
                "catalogs/shopfloor.yaml",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "session-charter-001"


def test_session_init_repeated_explicit_id_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    _init_session(tmp_path, capsys, session_id="session-001")
    capsys.readouterr()  # clear
    charter_path = tmp_path / "charter.yaml"
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
            "--session-id",
            "session-001",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.session.exists"
    assert err["safe_ids"] == ["session-001"]


# --- charter validation ----------------------------------------------------- #


@pytest.mark.parametrize(
    "field",
    [
        "business_question",
        "approver",
        "catalog_fingerprint",
        "inclusions",
        "exclusions",
        "acceptance_questions",
    ],
)
def test_session_init_requires_charter_field(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    _write_charter(charter_path, remove=(field,))
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.charter_invalid"


@pytest.mark.parametrize(
    "field,value",
    [
        ("business_question", ""),
        ("approver", "   "),
        ("inclusions", []),
        ("exclusions", []),
        ("acceptance_questions", []),
    ],
)
def test_session_init_rejects_blank_charter_field(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: Any,
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    _write_charter(charter_path, **{field: value})
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.charter_invalid"


def test_session_init_rejects_invalid_catalog_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    _write_charter(charter_path, catalog_fingerprint="not-a-valid-hash")
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.charter_invalid"


def test_session_init_rejects_non_mapping_charter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    charter_path.write_text("- just\n- a list\n", encoding="utf-8")
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.charter_invalid"


def test_session_init_rejects_unreadable_charter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(tmp_path / "missing.yaml"),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.charter_load_failed"


# --- path containment ------------------------------------------------------- #


def test_session_init_rejects_catalog_path_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    _write_charter(charter_path)
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "../outside.yaml",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.path_not_contained"


def test_session_init_rejects_absolute_catalog_path_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    _write_charter(charter_path)
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "/etc/passwd",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.path_not_contained"


def test_session_init_rejects_summary_root_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    _write_charter(charter_path)
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
            "--summary-root",
            "../../leaked",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.path_not_contained"


def test_session_init_accepts_contained_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "catalogs").mkdir()
    (tmp_path / "catalogs" / "shopfloor.yaml").write_text("{}", encoding="utf-8")
    out = _init_session(
        tmp_path,
        capsys,
        extra_args=[
            "--catalog-path",
            "catalogs/shopfloor.yaml",
            "--summary-root",
            "semantic_changes",
        ],
    )
    assert out["state"] == "initialized"


def test_session_init_persists_catalog_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The catalog path is persisted in the charter and rebuilt from the journal."""
    _init_git_repo(tmp_path)
    _init_session(tmp_path, capsys, session_id="session-001")
    session_dir = tmp_path / ".selayer" / "discovery" / "sessions" / "session-001"
    # Remove the non-authority cache so the charter is rebuilt from the journal.
    (session_dir / "state.json").unlink()
    store = SessionStore.open(session_dir)
    assert store.charter.catalog_path == "catalogs/shopfloor.yaml"


# --- session status --------------------------------------------------------- #


def test_session_status_rebuilds_from_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    init_out = _init_session(tmp_path, capsys, session_id="session-001")
    session_dir = tmp_path / ".selayer" / "discovery" / "sessions" / "session-001"
    # Remove the non-authority cache to prove status rebuilds from the journal.
    (session_dir / "state.json").unlink()
    code = main(
        ["session", "status", "--session-id", "session-001", "--project", str(tmp_path)]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "initialized"
    assert out["session_id"] == "session-001"
    assert out["charter_fingerprint"] == init_out["charter_fingerprint"]
    assert out["event_count"] == 1
    assert out["head_hash"]
    assert out["schema_version"] == 1


def test_session_status_unknown_session_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    code = main(
        [
            "session",
            "status",
            "--session-id",
            "session-missing",
            "--project",
            str(tmp_path),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.session.not_initialized"


def test_session_status_stale_nodes_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    init_out = _init_session(tmp_path, capsys)
    session_id = init_out["session_id"]
    session_dir = tmp_path / ".selayer" / "discovery" / "sessions" / session_id
    # Record artifacts directly (no CLI command for this in Task 8) and revise
    # one to produce transitive stale targets, then assert they are sorted.
    store = SessionStore.open(session_dir)
    store.record_artifact("claim-zeta", content_hash="0" * 64, actor="analyst")
    store.record_artifact(
        "claim-beta", content_hash="b" * 64, depends_on=("claim-zeta",), actor="analyst"
    )
    store.record_artifact(
        "claim-alpha",
        content_hash="1" * 64,
        depends_on=("claim-zeta",),
        actor="analyst",
    )
    store.record_artifact("claim-zeta", content_hash="9" * 64, actor="analyst")
    code = main(
        ["session", "status", "--session-id", session_id, "--project", str(tmp_path)]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stale_nodes"] == sorted(out["stale_nodes"])
    assert "claim-alpha" in out["stale_nodes"]
    assert "claim-beta" in out["stale_nodes"]


# --- session close ---------------------------------------------------------- #


def test_session_close_returns_closed_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    _init_session(tmp_path, capsys, session_id="session-001")
    capsys.readouterr()  # clear
    code = main(
        ["session", "close", "--session-id", "session-001", "--project", str(tmp_path)]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "closed"
    assert out["session_id"] == "session-001"
    assert out["head_hash"]


def test_session_status_reports_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    _init_session(tmp_path, capsys, session_id="session-001")
    main(
        ["session", "close", "--session-id", "session-001", "--project", str(tmp_path)]
    )
    capsys.readouterr()  # clear
    code = main(
        ["session", "status", "--session-id", "session-001", "--project", str(tmp_path)]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "closed"


def test_session_close_already_closed_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    _init_session(tmp_path, capsys, session_id="session-001")
    main(
        ["session", "close", "--session-id", "session-001", "--project", str(tmp_path)]
    )
    capsys.readouterr()  # clear
    code = main(
        ["session", "close", "--session-id", "session-001", "--project", str(tmp_path)]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.session.closed"


def test_session_close_unknown_session_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    code = main(
        [
            "session",
            "close",
            "--session-id",
            "session-missing",
            "--project",
            str(tmp_path),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.session.not_initialized"


# --- exit codes ------------------------------------------------------------- #


def test_missing_required_arg_is_usage_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["session", "init", "--project", str(tmp_path)])
    assert raised.value.code == 2


def test_unknown_command_is_usage_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["frobnicate"])
    assert raised.value.code == 2


def test_session_subcommand_required(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["session"])
    assert raised.value.code == 2


# --- deterministic output --------------------------------------------------- #


def test_session_init_output_keys_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    _write_charter(charter_path)
    assert (
        main(
            [
                "session",
                "init",
                "--charter",
                str(charter_path),
                "--project",
                str(tmp_path),
                "--catalog-path",
                "catalogs/shopfloor.yaml",
                "--session-id",
                "session-sorted-001",
            ]
        )
        == 0
    )
    raw = capsys.readouterr().out.strip()
    assert raw == json.dumps(json.loads(raw), sort_keys=True)


def test_session_status_output_keys_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    _init_session(tmp_path, capsys, session_id="session-001")
    capsys.readouterr()  # clear
    main(
        ["session", "status", "--session-id", "session-001", "--project", str(tmp_path)]
    )
    raw = capsys.readouterr().out.strip()
    assert raw == json.dumps(json.loads(raw), sort_keys=True)


def test_error_output_keys_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    main(
        [
            "session",
            "status",
            "--session-id",
            "session-missing",
            "--project",
            str(tmp_path),
        ]
    )
    raw = capsys.readouterr().err.strip()
    assert raw == json.dumps(json.loads(raw), sort_keys=True)


# --- secrecy: no traceback, no value leakage -------------------------------- #


def test_cli_never_prints_traceback_for_unreadable_charter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    charter_path = tmp_path / "charter.yaml"
    charter_path.mkdir()  # a directory, not a readable file
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    err = json.loads(captured.err)
    assert err["code"]


def test_charter_error_never_echoes_charter_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    secret = "supersecret-dsn-token-VALUE-12345"
    charter_path = tmp_path / "charter.yaml"
    # A charter carrying a secret in valid fields but missing a required field.
    _write_charter(
        charter_path, business_question=secret, approver=secret, remove=("inclusions",)
    )
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert secret not in captured.err
    assert secret not in captured.out


def test_yaml_parse_error_never_echoes_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    secret = "LEAKME-if-rendered-98765"
    charter_path = tmp_path / "charter.yaml"
    charter_path.write_text(f"bad: : : {secret}\n  - broken\n", encoding="utf-8")
    code = main(
        [
            "session",
            "init",
            "--charter",
            str(charter_path),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalogs/shopfloor.yaml",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert secret not in captured.err
    err = json.loads(captured.err)
    assert err["code"] == "discovery.cli.charter_load_failed"


def test_status_rejects_invalid_session_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    code = main(
        [
            "session",
            "status",
            "--session-id",
            "../../etc",
            "--project",
            str(tmp_path),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.session_id_invalid"


# --------------------------------------------------------------------------- #
# Sample-policy CLI tests (Task 11)                                          #
# --------------------------------------------------------------------------- #

_POLICY_SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("amount", pa.int64()),
        ("customer_name", pa.utf8()),
        ("access_token", pa.utf8()),
    ]
)


def _policy_scan_session() -> SourceScanSession:
    """Build a fresh in-memory scan session for the policy test source."""

    rows = [
        {"id": 1, "amount": 100, "customer_name": "Alice", "access_token": "tok-1"},
        {"id": 2, "amount": 200, "customer_name": "Bob", "access_token": "tok-2"},
    ]
    batch = pa.RecordBatch.from_pylist(rows, schema=_POLICY_SCHEMA)
    table_schema = table_schema_from_arrow(_POLICY_SCHEMA)
    fp = schema_fingerprint(table_schema)
    reader = pa.RecordBatchReader.from_batches(_POLICY_SCHEMA, [batch])
    return SourceScanSession(
        source_id="orders",
        schema=table_schema,
        consistency=SourceConsistency.REOPENABLE_SNAPSHOT,
        snapshot_id="snap-1",
        reader=reader,
        release=lambda: None,
        recheck=lambda: SourceSnapshot(
            SourceConsistency.REOPENABLE_SNAPSHOT, "snap-1", fp
        ),
    )


def _policy_profile(tmp_path: Path) -> SourceProfile:
    """Build a completed (available) profile from a small in-memory scan."""

    session = _policy_scan_session()
    return ProfileRunner(session, tmp_path / "spill", grain=("id",)).run()


def _write_profile(tmp_path: Path, profile: SourceProfile) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile.to_dict()), encoding="utf-8")
    return path


def _init_policy_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _init_git_repo(tmp_path)
    _init_session(tmp_path, capsys, session_id="session-policy-001")
    capsys.readouterr()  # clear init output


# --- propose-policy -------------------------------------------------------- #


def test_propose_policy_emits_omit_default_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    code = main(
        [
            "profile",
            "propose-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(profile_path),
            "--grain",
            "id",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    # Every field defaults to omit.
    assert all(field["transform"] == "omit" for field in out["fields"])
    # Credential fields are hard-denied.
    token_field = next(f for f in out["fields"] if f["name"] == "access_token")
    assert token_field["hard_denied"] is True
    amount_field = next(f for f in out["fields"] if f["name"] == "amount")
    assert amount_field["hard_denied"] is False
    # salt_id is a 64-hex identifier; fingerprint present.
    assert len(out["salt_id"]) == 64
    assert out["fingerprint"]
    assert out["classifications"]


def test_propose_policy_never_leaks_raw_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    main(
        [
            "profile",
            "propose-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(profile_path),
        ]
    )
    raw = capsys.readouterr().out
    assert "Alice" not in raw
    assert "Bob" not in raw
    assert "tok-1" not in raw
    assert "tok-2" not in raw


def test_propose_policy_persists_and_reuses_salt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    main(
        [
            "profile",
            "propose-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(profile_path),
        ]
    )
    out1 = json.loads(capsys.readouterr().out)
    main(
        [
            "profile",
            "propose-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(profile_path),
        ]
    )
    out2 = json.loads(capsys.readouterr().out)
    assert out1["salt_id"] == out2["salt_id"]
    assert out1["fingerprint"] == out2["fingerprint"]


def test_propose_policy_unknown_session_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    code = main(
        [
            "profile",
            "propose-policy",
            "--session-id",
            "session-missing",
            "--project",
            str(tmp_path),
            "--profile",
            str(tmp_path / "profile.json"),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.session.not_initialized"


def test_propose_policy_profile_path_escape_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    code = main(
        [
            "profile",
            "propose-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            "../../leaked.json",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.path_not_contained"


def test_propose_policy_missing_profile_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    code = main(
        [
            "profile",
            "propose-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(tmp_path / "missing.json"),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.profile.load_failed"


def test_propose_policy_unavailable_profile_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    unavailable = {
        "source_id": "orders",
        "schema_fingerprint": "a" * 64,
        "consistency": "reopenable_snapshot",
        "snapshot_id": "snap-1",
        "mode": "reopenable",
        "outcome": "unavailable",
        "unavailable_reason": "unsupported_type",
        "row_count": None,
        "batch_count": 0,
        "batch_hashes": [],
        "grain_duplicate_count": None,
        "columns": [],
    }
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(unavailable), encoding="utf-8")
    code = main(
        [
            "profile",
            "propose-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(profile_path),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.profile.not_available"


# --- activate-policy ------------------------------------------------------- #


def _propose_and_write_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], profile_path: Path
) -> dict[str, Any]:
    """Run propose-policy and return the policy dict, writing it to a file."""

    main(
        [
            "profile",
            "propose-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(profile_path),
            "--grain",
            "id",
        ]
    )
    policy_out = json.loads(capsys.readouterr().out)
    policy_path = tmp_path / "policy.json"
    policy_dict = {k: v for k, v in policy_out.items() if k != "classifications"}
    policy_path.write_text(json.dumps(policy_dict), encoding="utf-8")
    return policy_out


def test_activate_policy_emits_activation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    policy_out = _propose_and_write_policy(tmp_path, capsys, profile_path)
    code = main(
        [
            "profile",
            "activate-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(profile_path),
            "--policy",
            str(tmp_path / "policy.json"),
            "--activated-at",
            "2026-01-01",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "session-policy-001"
    assert out["policy_fingerprint"] == policy_out["fingerprint"]
    assert out["profile_fingerprint"]
    assert out["schema_fingerprint"]
    assert out["fingerprint"]  # activation binding fingerprint
    assert out["approver"]  # normalized charter approver
    assert out["activated_at"] == "2026-01-01"


def test_activate_policy_changed_approver_changes_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    _propose_and_write_policy(tmp_path, capsys, profile_path)
    policy_arg = str(tmp_path / "policy.json")
    common = [
        "profile",
        "activate-policy",
        "--session-id",
        "session-policy-001",
        "--project",
        str(tmp_path),
        "--profile",
        str(profile_path),
        "--policy",
        policy_arg,
    ]
    main(common + ["--approver", "Alice One", "--activated-at", "2026-01-01"])
    fp1 = json.loads(capsys.readouterr().out)["fingerprint"]
    main(common + ["--approver", "Bob Two", "--activated-at", "2026-01-01"])
    fp2 = json.loads(capsys.readouterr().out)["fingerprint"]
    assert fp1 != fp2


def test_activate_policy_unknown_session_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    code = main(
        [
            "profile",
            "activate-policy",
            "--session-id",
            "session-missing",
            "--project",
            str(tmp_path),
            "--profile",
            str(tmp_path / "profile.json"),
            "--policy",
            str(tmp_path / "policy.json"),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.session.not_initialized"


def test_activate_policy_policy_path_escape_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    code = main(
        [
            "profile",
            "activate-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(profile_path),
            "--policy",
            "../../leaked.json",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.path_not_contained"


def test_activate_policy_missing_policy_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    code = main(
        [
            "profile",
            "activate-policy",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--profile",
            str(profile_path),
            "--policy",
            str(tmp_path / "missing.json"),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.profile.load_failed"


# --- export-context -------------------------------------------------------- #


def test_export_context_missing_salt_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    # Build a valid policy file but never create the session salt.
    _propose_and_write_policy(tmp_path, capsys, profile_path)
    # Remove the salt so export-context sees it missing.
    salt_path = (
        tmp_path
        / ".selayer"
        / "discovery"
        / "sessions"
        / "session-policy-001"
        / "policy"
        / "salt"
    )
    salt_path.unlink()
    code = main(
        [
            "profile",
            "export-context",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--source-id",
            "orders",
            "--profile",
            str(profile_path),
            "--policy",
            str(tmp_path / "policy.json"),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.profile.salt_missing"


def test_export_context_unknown_session_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    code = main(
        [
            "profile",
            "export-context",
            "--session-id",
            "session-missing",
            "--project",
            str(tmp_path),
            "--source-id",
            "orders",
            "--profile",
            str(tmp_path / "profile.json"),
            "--policy",
            str(tmp_path / "policy.json"),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.session.not_initialized"


def test_export_context_policy_path_escape_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    code = main(
        [
            "profile",
            "export-context",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--source-id",
            "orders",
            "--profile",
            str(profile_path),
            "--policy",
            "../../leaked.json",
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.cli.path_not_contained"


def _activate_policy_for_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[Path, Path, dict[str, Any]]:
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    policy_out = _propose_and_write_policy(tmp_path, capsys, profile_path)
    assert (
        main(
            [
                "profile",
                "activate-policy",
                "--session-id",
                "session-policy-001",
                "--project",
                str(tmp_path),
                "--profile",
                str(profile_path),
                "--policy",
                str(tmp_path / "policy.json"),
                "--activated-at",
                "2026-01-01",
            ]
        )
        == 0
    )
    capsys.readouterr()
    return profile_path, tmp_path / "policy.json", policy_out


def test_activate_policy_persists_activation_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile_path, _, _ = _activate_policy_for_cli(tmp_path, capsys)
    session_dir = cli_module._session_dir(
        tmp_path.resolve(), "session-policy-001"
    )
    activation_path = cli_module._activation_path(session_dir, "orders")
    assert activation_path.is_file()
    activation = PolicyActivation.from_dict(json.loads(activation_path.read_text()))
    profile = _policy_profile(tmp_path)
    assert activation.session_id == "session-policy-001"
    assert activation.source_id == "orders"
    assert activation.profile_fingerprint == profile.fingerprint
    assert profile_path.is_file()


def test_export_context_fails_without_activation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile = _policy_profile(tmp_path)
    profile_path = _write_profile(tmp_path, profile)
    _propose_and_write_policy(tmp_path, capsys, profile_path)
    code = main(
        [
            "profile",
            "export-context",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--source-id",
            "orders",
            "--profile",
            str(profile_path),
            "--policy",
            str(tmp_path / "policy.json"),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.profile.policy_stale"
    assert err["safe_detail"] == "activation"


def test_export_context_fails_when_policy_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile_path, policy_path, _ = _activate_policy_for_cli(tmp_path, capsys)
    policy = json.loads(policy_path.read_text())
    next(field for field in policy["fields"] if field["name"] == "amount")[
        "transform"
    ] = "hash"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    code = main(
        [
            "profile",
            "export-context",
            "--session-id",
            "session-policy-001",
            "--project",
            str(tmp_path),
            "--source-id",
            "orders",
            "--profile",
            str(profile_path),
            "--policy",
            str(policy_path),
        ]
    )
    assert code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "discovery.profile.policy_stale"
    assert err["safe_detail"] == "policy"


def test_session_context_bytes_used_sums_valid_context_exports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_policy_session(tmp_path, capsys)
    session_dir = cli_module._session_dir(
        tmp_path.resolve(), "session-policy-001"
    )
    policy_dir = cli_module._policy_dir(session_dir)
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "context-a.json").write_text('{"bytes": 100}', encoding="utf-8")
    (policy_dir / "context-b.json").write_text('{"bytes": 200}', encoding="utf-8")
    (policy_dir / "context-bad.json").write_text("not json", encoding="utf-8")
    (policy_dir / "context-string.json").write_text(
        '{"bytes": "300"}', encoding="utf-8"
    )
    (policy_dir / "activation-orders.json").write_text(
        '{"bytes": 400}', encoding="utf-8"
    )
    assert cli_module._session_context_bytes_used(session_dir) == 300


def test_export_context_stdout_contains_only_safe_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_policy_session(tmp_path, capsys)
    profile_path, policy_path, _ = _activate_policy_for_cli(tmp_path, capsys)

    class FakeRegistry:
        def open_scan_session(self, *_args: object, **_kwargs: object) -> SourceScanSession:
            return _policy_scan_session()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cli_module,
        "_open_registry",
        lambda _project, _session_dir: (None, None, FakeRegistry()),
    )
    assert (
        main(
            [
                "profile",
                "export-context",
                "--session-id",
                "session-policy-001",
                "--project",
                str(tmp_path),
                "--source-id",
                "orders",
                "--profile",
                str(profile_path),
                "--policy",
                str(policy_path),
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert set(summary) == {
        "path",
        "fingerprint",
        "policy_fingerprint",
        "profile_fingerprint",
        "schema_fingerprint",
    }
    assert "session_id" not in summary
    assert "source_id" not in summary
    assert "bytes" not in summary
    assert "canary_scan" not in summary
