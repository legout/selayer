"""Build the enriched shopfloor OKF bundle on demand.

Composes a fresh OKF bundle from the catalog, authored business-context
references, and curated overlays, validates it against the shopfloor
knowledge policy, and publishes it atomically into the requested output
directory.  No partial output is ever published; a sibling candidate
directory is cleaned up on every failure path.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from examples.shopfloor.knowledge_policy import (
    ShopfloorKnowledgeIssue,
    validate_shopfloor_knowledge,
)
from selayer import SemanticLayer
from selayer.okf import OkfBundle

EXAMPLE_ROOT = Path(__file__).resolve().parent
CATALOG = EXAMPLE_ROOT / "shopfloor_semantic_layer.yaml"
REFERENCES_DIR = EXAMPLE_ROOT / "business_context"
OVERLAYS_DIR = EXAMPLE_ROOT / "okf_overlays"
DEFAULT_OUTPUT = EXAMPLE_ROOT / ".generated" / "knowledge"


class ShopfloorKnowledgeBuildError(Exception):
    """The composed bundle failed shopfloor knowledge policy validation."""

    def __init__(self, issues: tuple[ShopfloorKnowledgeIssue, ...]) -> None:
        self._issues = issues
        descriptions = ", ".join(f"{issue.code}({issue.path})" for issue in issues)
        super().__init__(
            f"shopfloor knowledge policy failed with {len(issues)} "
            f"issue(s): {descriptions}"
        )

    @property
    def issues(self) -> tuple[ShopfloorKnowledgeIssue, ...]:
        return self._issues


def _reject_symlink_ancestors(path: Path) -> None:
    """Reject any existing symlink in the lexical parent chain of ``path``.

    Walks ancestors without resolving through symlinks so a symlinked
    parent cannot redirect output to an unintended target.
    """
    for ancestor in path.parents:
        if ancestor.is_symlink():
            raise ValueError("path component must not be a symbolic link")


def _require_absent_or_empty(destination: Path) -> None:
    """Reject symlinks and every non-directory filesystem object.

    Only an absent path or an empty directory is accepted.  Symlinks
    (including broken links), regular files, FIFOs, sockets, and devices
    are rejected before any staging directory is created.
    """
    if destination.is_symlink():
        raise ValueError("output path must not be a symbolic link")
    if destination.exists() and not destination.is_dir():
        raise ValueError("output path must not be a non-directory filesystem object")
    if destination.is_dir() and any(destination.iterdir()):
        raise ValueError("output directory must be empty")


def build_knowledge(output_dir: Path) -> OkfBundle:
    """Compose, validate, and atomically publish the shopfloor OKF bundle.

    The bundle is built entirely in a sibling candidate directory and renamed
    into ``output_dir`` only after strict loading and policy validation pass.
    Any failure --- a domain error, malformed overlay, policy violation, I/O
    error, or rename failure --- cleans up the candidate tree without
    publishing partial output.
    """
    layer = SemanticLayer.load(CATALOG)
    # Preserve the lexical absolute path without resolving symlinks so a
    # symlinked ancestor cannot redirect output to an unintended target.
    destination = output_dir.absolute()
    _reject_symlink_ancestors(destination)
    _require_absent_or_empty(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}.candidate-",
        dir=destination.parent,
    ) as directory:
        candidate = Path(directory) / "knowledge"
        bundle = OkfBundle.build(
            layer,
            candidate,
            references_dir=REFERENCES_DIR,
            overlays_dir=OVERLAYS_DIR,
        )
        issues = validate_shopfloor_knowledge(bundle, layer)
        if issues:
            raise ShopfloorKnowledgeBuildError(issues)
        # Single atomic rename: os.replace atomically replaces an existing
        # empty directory on the same filesystem, so the destination is
        # preserved (not removed first) if the rename fails.
        candidate.replace(destination)
    return OkfBundle.load(destination, layer=layer, strict=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the enriched shopfloor OKF bundle."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: build knowledge and print deterministic JSON on success."""
    arguments = _parser().parse_args(argv)
    try:
        bundle = build_knowledge(arguments.output_dir)
    except ShopfloorKnowledgeBuildError as error:
        # Policy issues carry only deterministic codes and paths.
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        # Fixed, secret-safe envelope: never echo arbitrary exception text.
        print("error: shopfloor knowledge build failed", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "command": "build-shopfloor-knowledge",
                "concepts": len(bundle.concepts),
                "destination": str(arguments.output_dir),
                "diagnostics": [],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
