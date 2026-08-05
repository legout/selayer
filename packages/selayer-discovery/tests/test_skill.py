"""Contract tests for the canonical semantic-discovery Agent Skill.

The Agent Skill is the only reasoning layer in discovery: it may reason, but
every state mutation must flow through the deterministic ``selayer-discovery``
companion CLI. These tests pin the *contract* the canonical skill body must
uphold so a packaging or wording change cannot silently weaken the guardrails.

They never invoke a model. They read the shipped skill markdown, the
repository forwarding skill, and the built wheel, and assert:

* the canonical body states each non-negotiable workflow rule;
* the workflow orders charter before intake and policy activation before any
  value-derived context;
* the skill tells the agent to stop and report blocked/unavailable checks
  instead of bypassing them;
* the root skill forwards to the canonical body by relative path without
  duplicating it, and the forwarder target resolves;
* the built wheel ships exactly one canonical skill body.
"""

from __future__ import annotations

import re
import subprocess
import zipfile
from itertools import pairwise
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CANONICAL_REL = Path("packages/selayer-discovery/skills/semantic-discovery/SKILL.md")
_FORWARDER_REL = Path(".agents/skills/semantic-discovery/SKILL.md")

_CANONICAL_PATH = _REPO_ROOT / _CANONICAL_REL
_FORWARDER_PATH = _REPO_ROOT / _FORWARDER_REL


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def canonical() -> str:
    """The canonical packaged skill body (fails fast if it is absent)."""

    return _read(_CANONICAL_PATH)


@pytest.fixture(scope="module")
def forwarder() -> str:
    """The repository forwarding skill body (fails fast if it is absent)."""

    return _read(_FORWARDER_PATH)


def _lower(text: str) -> str:
    return text.casefold()


# --------------------------------------------------------------------------- #
# Existence and frontmatter                                                   #
# --------------------------------------------------------------------------- #


def test_canonical_skill_file_exists() -> None:
    assert _CANONICAL_PATH.is_file(), f"canonical skill missing: {_CANONICAL_PATH}"


def test_forwarder_skill_file_exists() -> None:
    assert _FORWARDER_PATH.is_file(), f"forwarding skill missing: {_FORWARDER_PATH}"


def test_canonical_skill_has_frontmatter(canonical: str) -> None:
    """The canonical body carries skill metadata frontmatter."""

    match = re.match(r"\A---\n(?P<body>.*?)\n---\n", canonical, re.DOTALL)
    assert match is not None, "canonical skill lacks YAML frontmatter"
    body = match.group("body")
    assert "name:" in body
    assert "description:" in body


def test_forwarder_skill_has_frontmatter(forwarder: str) -> None:
    match = re.match(r"\A---\n(?P<body>.*?)\n---\n", forwarder, re.DOTALL)
    assert match is not None, "forwarding skill lacks YAML frontmatter"
    body = match.group("body")
    assert "name:" in body
    assert "description:" in body


# --------------------------------------------------------------------------- #
# Non-negotiable workflow rules (the skill contract)                          #
# --------------------------------------------------------------------------- #


def test_skill_requires_charter_before_intake(canonical: str) -> None:
    text = _lower(canonical)
    assert "charter" in text
    assert "before any intake" in text or "before intake" in text


def test_skill_requires_one_question_at_a_time(canonical: str) -> None:
    text = _lower(canonical)
    assert "one question at a time" in text or "one open question" in text


def test_skill_requires_untrusted_evidence_treatment(canonical: str) -> None:
    text = _lower(canonical)
    assert "untrusted" in text
    # Evidence must be treated as inert, quoted content (no instruction
    # execution from document or provider text).
    assert "quoted" in text or "inert" in text


def test_skill_requires_sample_policy_approval_before_values(canonical: str) -> None:
    text = _lower(canonical)
    assert "sample policy" in text or "sample-policy" in text
    assert "activate" in text
    # A value-derived context export must not precede activation.
    assert "export-context" in text


def test_skill_requires_companion_cli_for_every_mutation(canonical: str) -> None:
    text = _lower(canonical)
    assert "selayer-discovery" in text
    # The skill must forbid direct edits to session / state files.
    assert "never edit" in text or "do not edit" in text
    assert "directly" in text


def test_skill_forbids_direct_wiki_writes(canonical: str) -> None:
    text = _lower(canonical)
    assert "wiki" in text
    assert "never write" in text or "do not write" in text or "no direct" in text


def test_skill_requires_group_and_batch_attestation_before_apply(canonical: str) -> None:
    text = _lower(canonical)
    assert "apply" in text
    assert "attestation" in text
    # Both a group decision and an apply-batch attestation are required.
    assert "group" in text
    assert "batch" in text
    assert (
        "group decision and" in text
        or "group and batch" in text
        or "group attestation and" in text
        or "group decision" in text
    )


def test_skill_forbids_git_operations(canonical: str) -> None:
    text = _lower(canonical)
    assert "git" in text
    assert (
        "never run git" in text
        or "no git" in text
        or "do not run git" in text
        or "never invoke git" in text
    )


def test_skill_forbids_verified_claims_from_agent_reasoning(canonical: str) -> None:
    text = _lower(canonical)
    assert "verified" in text
    # The agent must not assert verification from its own reasoning.
    assert (
        "never claim" in text
        or "do not claim" in text
        or "must not claim" in text
        or "from reasoning" in text
        or "reasoning alone" in text
    )


# --------------------------------------------------------------------------- #
# Workflow ordering contracts                                                 #
# --------------------------------------------------------------------------- #


def test_workflow_orders_charter_before_intake(canonical: str) -> None:
    """The charter command reference precedes the intake command reference."""

    charter_idx = canonical.find("session init")
    intake_idx = canonical.find("intake add-document")
    assert charter_idx != -1, "canonical skill does not reference `session init`"
    assert intake_idx != -1, "canonical skill does not reference `intake add-document`"
    assert charter_idx < intake_idx


def test_workflow_orders_policy_activation_before_context_export(
    canonical: str,
) -> None:
    """An activated policy must precede any value-derived context export."""

    activate_idx = canonical.find("activate-policy")
    export_idx = canonical.find("export-context")
    assert activate_idx != -1, "canonical skill does not reference `activate-policy`"
    assert export_idx != -1, "canonical skill does not reference `export-context`"
    assert activate_idx < export_idx


# --------------------------------------------------------------------------- #
# Downstream proposal / apply / recover command flow                         #
# --------------------------------------------------------------------------- #


_DOWNSTREAM_COMMANDS = [
    "proposal import",
    "proposal show",
    "proposal verify",
    "proposal attest",
    "proposal prepare-apply",
    "proposal attest-apply",
    "proposal export-preview",
    "proposal apply",
    "recover",
]


@pytest.mark.parametrize("command", _DOWNSTREAM_COMMANDS)
def test_skill_names_each_downstream_command(canonical: str, command: str) -> None:
    """Every deterministic downstream command is named explicitly in the skill."""

    assert command in canonical, (
        f"canonical skill does not name the `{command}` command"
    )


def test_skill_orders_downstream_command_flow(canonical: str) -> None:
    """The proposal lifecycle is a deterministic ordered command flow."""

    ordered = [
        "proposal import",
        "proposal show",
        "proposal verify",
        "proposal attest",
        "proposal prepare-apply",
        "proposal attest-apply",
        "proposal export-preview",
        "proposal apply",
    ]
    positions: dict[str, int] = {}
    for command in ordered:
        idx = canonical.find(command)
        assert idx != -1, f"canonical skill does not name `{command}`"
        positions[command] = idx
    # Each command must appear after the one it depends on.
    for earlier, later in pairwise(ordered):
        assert positions[earlier] < positions[later], (
            f"`{later}` must follow `{earlier}` in the canonical flow"
        )


def test_skill_requires_explicit_user_request_for_apply(canonical: str) -> None:
    """Apply must not run without a separate explicit user request."""

    text = _lower(canonical)
    assert "apply" in text
    assert "explicit" in text
    assert "user request" in text or "user authorization" in text


# --------------------------------------------------------------------------- #
# Error / safety: no leakage of sensitive categories                         #
# --------------------------------------------------------------------------- #


_LEAKAGE_CATEGORIES = [
    "credentials",
    "document bodies",
    "interview answers",
    "sample values",
    "source locations",
    "raw sessions",
    "full transcripts",
    "provider bodies",
    "backup paths",
    "journals",
    "driver errors",
]


@pytest.mark.parametrize("category", _LEAKAGE_CATEGORIES)
def test_skill_forbids_leakage_of(canonical: str, category: str) -> None:
    """The skill must name every sensitive category it forbids exposing."""

    assert category in _lower(canonical), (
        f"canonical skill does not forbid leaking `{category}`"
    )


# --------------------------------------------------------------------------- #
# Blocked / unavailable behavior                                              #
# --------------------------------------------------------------------------- #


def test_skill_stops_on_blocked_or_unavailable_checks(canonical: str) -> None:
    """The agent must stop and report blocked/unavailable checks, not bypass."""

    text = _lower(canonical)
    assert "blocked" in text
    assert "unavailable" in text
    assert "stop" in text
    assert "report" in text


# --------------------------------------------------------------------------- #
# Root forwarding skill                                                       #
# --------------------------------------------------------------------------- #


def test_forwarder_skill_is_tracked_by_git() -> None:
    """The forwarder must be version-controlled despite the broad ``.agents/`` rule.

    The repository ``.gitignore`` ignores all of ``.agents/``. That rule would
    silently drop the forwarder (which these tests read from disk), so a fresh
    clone would lack the file and the forwarding-skill contract would break.
    Force-add it and pin the invariant here with ``git ls-files --error-unmatch``
    so it can never regress to untracked.
    """

    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", _FORWARDER_REL.as_posix()],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"forwarder skill is not tracked by git (the broad .agents/ rule "
        f"ignores it). Run `git add -f {_FORWARDER_REL}`. "
        f"stderr: {result.stderr.strip()}"
    )


def test_forwarder_references_canonical_by_relative_path(forwarder: str) -> None:
    """The root skill points at the canonical package skill by relative path."""

    assert _CANONICAL_REL.as_posix() in forwarder


def test_forwarder_does_not_duplicate_workflow_body(forwarder: str) -> None:
    """The forwarder must not copy the canonical workflow body."""

    # Distinctive canonical-only directive language must be absent here.
    assert "before any intake" not in _lower(forwarder)
    assert "one question at a time" not in _lower(forwarder)
    assert "export-context" not in forwarder


def test_forwarder_target_resolves() -> None:
    """The relative path the forwarder names must point at a real file."""

    assert _CANONICAL_PATH.is_file(), (
        f"forwarder target does not resolve: {_CANONICAL_PATH}"
    )


# --------------------------------------------------------------------------- #
# Built-wheel packaging                                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def discovery_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the ``selayer-discovery`` wheel once into an isolated directory."""

    out_dir = tmp_path_factory.mktemp("skill-wheel")
    subprocess.run(
        ["uv", "build", "--package", "selayer-discovery", "--out-dir", str(out_dir)],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
    )
    wheels = sorted(out_dir.glob("selayer_discovery-*.whl"))
    assert wheels, "no discovery wheel was built"
    return wheels[0]


def test_wheel_contains_exactly_one_canonical_skill_body(
    discovery_wheel: Path, canonical: str
) -> None:
    """The wheel ships exactly one canonical ``skills/semantic-discovery/SKILL.md``."""

    skill_name = "skills/semantic-discovery/SKILL.md"
    with zipfile.ZipFile(discovery_wheel) as archive:
        names = archive.namelist()
        skill_entries = [n for n in names if n.endswith(skill_name)]
        assert skill_entries, "wheel does not contain the canonical skill body"
        assert len(skill_entries) == 1, (
            f"wheel must contain exactly one canonical skill body, found: {skill_entries}"
        )
        shipped = archive.read(skill_entries[0]).decode("utf-8")
    assert shipped == canonical, (
        "shipped canonical skill body differs from the source file"
    )
