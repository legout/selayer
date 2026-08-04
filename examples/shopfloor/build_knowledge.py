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


def _require_absent_or_empty(destination: Path) -> None:
    """Reject files, symlinks, and non-empty directories."""
    if destination.is_symlink():
        raise ValueError(
            f"output directory '{destination}' must not be a symbolic link"
        )
    if destination.is_file():
        raise ValueError(f"output directory '{destination}' must not be a file")
    if destination.is_dir():
        children = list(destination.iterdir())
        if children:
            raise ValueError(f"output directory '{destination}' must be empty")


def build_knowledge(output_dir: Path) -> OkfBundle:
    """Compose, validate, and atomically publish the shopfloor OKF bundle.

    The bundle is built entirely in a sibling candidate directory and renamed
    into ``output_dir`` only after strict loading and policy validation pass.
    Any failure --- a domain error, malformed overlay, policy violation, I/O
    error, or rename failure --- cleans up the candidate tree without
    publishing partial output.
    """
    layer = SemanticLayer.load(CATALOG)
    if output_dir.is_symlink():
        raise ValueError(f"output directory '{output_dir}' must not be a symbolic link")
    destination = output_dir.resolve()
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
        if destination.exists():
            destination.rmdir()
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
    except (OSError, ValueError, ShopfloorKnowledgeBuildError) as error:
        print(f"error: {error}", file=sys.stderr)
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
