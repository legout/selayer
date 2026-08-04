from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from selayer_discovery import __version__
from selayer_discovery.cli import main
from selayer_discovery.session import SessionStore


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
