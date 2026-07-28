"""Dependency-free command-line interface for advisory OKF bundles."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from selayer.catalog import SemanticLayer

from .bundle import OkfBundle
from .model import ContextResult, OkfIssue, SyncReport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="selayer-okf",
        description="Generate, sync, validate, and retrieve advisory OKF context.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="create a new bundle")
    generate.add_argument("catalog", type=Path)
    generate.add_argument("destination", type=Path)

    sync = commands.add_parser("sync", help="sync catalog definitions into a bundle")
    sync.add_argument("catalog", type=Path)
    sync.add_argument("bundle", type=Path)
    sync.add_argument("--dry-run", action="store_true")

    validate = commands.add_parser("validate", help="validate an existing bundle")
    validate.add_argument("bundle", type=Path)
    validate.add_argument("--catalog", type=Path)

    retrieve = commands.add_parser("retrieve", help="retrieve attributed context")
    retrieve.add_argument("bundle", type=Path)
    retrieve.add_argument("semantic_ids", nargs="+")
    retrieve.add_argument("--catalog", type=Path)
    retrieve.add_argument("--no-linked", action="store_true")
    retrieve.add_argument("--max-chars", type=int, default=12_000)
    retrieve.add_argument("--max-depth", type=int, default=1)
    return parser


def _issue(issue: OkfIssue) -> dict[str, str]:
    return {
        "message": issue.message,
        "path": issue.path,
        "severity": issue.severity,
    }


def _sync_payload(report: SyncReport, *, dry_run: bool) -> dict[str, Any]:
    return {
        "command": "sync",
        "conflicts": list(report.conflicts),
        "dry_run": dry_run,
        "orphaned": list(report.orphaned),
        "unchanged": list(report.unchanged),
        "written": list(report.written),
    }


def _context_payload(result: ContextResult) -> dict[str, Any]:
    return {
        "command": "retrieve",
        "diagnostics": [_issue(issue) for issue in result.diagnostics],
        "items": [
            {
                "concept_id": item.concept_id,
                "content": item.content,
                "freshness": item.freshness,
                "kind": item.kind,
                "provider": item.provider,
                "semantic_refs": list(item.semantic_refs),
                "sources": list(item.sources),
                "trust": item.trust,
            }
            for item in result.items
        ],
        "total_chars": result.total_chars,
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _load_layer(path: Path | None) -> SemanticLayer | None:
    return SemanticLayer.load(path) if path is not None else None


def _execute(arguments: argparse.Namespace) -> int:
    if arguments.command == "generate":
        layer = SemanticLayer.load(arguments.catalog)
        bundle = OkfBundle.from_layer(layer)
        bundle.write(arguments.destination)
        _emit(
            {
                "command": "generate",
                "concepts": len(bundle.concepts),
                "destination": str(arguments.destination),
            }
        )
        return 0

    if arguments.command == "sync":
        layer = SemanticLayer.load(arguments.catalog)
        report = OkfBundle.from_layer(layer).sync(
            arguments.bundle, dry_run=arguments.dry_run
        )
        _emit(_sync_payload(report, dry_run=arguments.dry_run))
        if report.conflicts:
            print(
                f"error: sync completed with {len(report.conflicts)} conflict(s)",
                file=sys.stderr,
            )
            return 1
        return 0

    layer = _load_layer(arguments.catalog)
    bundle = OkfBundle.load(arguments.bundle, layer=layer)
    if arguments.command == "validate":
        _emit(
            {
                "command": "validate",
                "concepts": len(bundle.concepts),
                "diagnostics": [_issue(issue) for issue in bundle.diagnostics],
            }
        )
        return 0

    result = bundle.context_for(
        arguments.semantic_ids,
        include_linked=not arguments.no_linked,
        max_chars=arguments.max_chars,
        max_depth=arguments.max_depth,
    )
    _emit(_context_payload(result))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, returning 0 on success and 1 for domain or I/O errors."""
    arguments = _parser().parse_args(argv)
    try:
        return _execute(arguments)
    except (OSError, LookupError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def run() -> None:
    """Console-script entry point."""
    raise SystemExit(main())


if __name__ == "__main__":
    run()
