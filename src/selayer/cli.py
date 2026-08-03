"""Unified command-line interface for selayer.

Stage 1 exposes a single ``catalog validate`` subcommand that adapts
:func:`selayer.verification.validate_catalog` into a one-line JSON report on
stdout. The report contract is owned by the verification module; this module
only parses arguments, renders the report, and maps the boolean ``passed``
flag onto an exit code.

Expected failures are handled with a fixed, secret-safe JSON payload on
stderr: only expected I/O and domain errors are caught, and their text is
never interpolated, because it may echo secret-bearing source bytes (file
paths, credentials, authenticated locations). Programmer mistakes such as
``AssertionError`` (the unreachable "unhandled command" branch) and
``TypeError`` propagate unchanged so they are not silently swallowed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from selayer.verification import validate_catalog

#: Fixed, secret-safe failure emitted for expected I/O and domain errors. The
#: exception text is intentionally never interpolated: it can carry the
#: catalog path or echoed source bytes that the loader has already scrubbed
#: out of diagnostics, so echoing it here would re-leak those secrets.
_FAILURE_MESSAGE = "could not read or validate catalog"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="selayer")
    commands = parser.add_subparsers(dest="area", required=True)
    catalog = commands.add_parser("catalog")
    catalog_commands = catalog.add_subparsers(dest="command", required=True)
    validate = catalog_commands.add_parser("validate")
    validate.add_argument("catalog")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, returning 0 on a passed report and 1 otherwise."""
    args = _parser().parse_args(argv)
    if args.area == "catalog" and args.command == "validate":
        try:
            result = validate_catalog(args.catalog)
        except (OSError, LookupError, ValueError):
            # Expected I/O and domain errors only: a missing or unreadable
            # catalog (OSError from ``read_text``), a failed key lookup
            # (LookupError), or a domain error not already wrapped into a
            # failed report by ``validate_catalog`` (ValueError, e.g.
            # ``CatalogValidationError``). Programmer mistakes such as
            # ``TypeError`` are deliberately not caught. Never echo the
            # exception text: it may carry secrets.
            print(
                json.dumps({"error": _FAILURE_MESSAGE}, sort_keys=True),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result.report.to_dict(), sort_keys=True))
        return 0 if result.report.passed else 1
    raise AssertionError("unhandled command")


def run() -> None:
    """Console-script entry point."""
    raise SystemExit(main())


if __name__ == "__main__":
    run()
