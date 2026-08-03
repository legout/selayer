"""Unified command-line interface for selayer.

Stage 1 exposes a single ``catalog validate`` subcommand that adapts
:func:`selayer.verification.validate_catalog` into a one-line JSON report on
stdout. The report contract is owned by the verification module; this module
only parses arguments, renders the report, and maps the boolean ``passed``
flag onto an exit code.

Expected failures are handled with a fixed, secret-safe JSON payload on
stderr: only the I/O error that can actually escape is caught, and its text is
never interpolated, because it may echo secret-bearing source bytes (file
paths, credentials, authenticated locations). Catalog-domain errors
(``CatalogValidationError``) are already adapted into a *failed report* by
:func:`validate_catalog`, so they surface as normal ``passed: false`` JSON on
stdout rather than as exceptions here. The only exception that can escape
``validate_catalog`` is ``OSError`` from ``Path.read_text`` (a missing or
unreadable catalog); that alone is caught. Programmer mistakes such as
``AssertionError`` (the unreachable "unhandled command" branch), ``TypeError``,
and unexpected ``LookupError``/``ValueError`` (e.g. ``KeyError``/``IndexError``
or a non-domain ``ValueError``) propagate unchanged so they are not silently
swallowed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from selayer.catalog import SemanticLayer
from selayer.okf import OkfBundle
from selayer.okf.cli import add_okf_commands, execute_okf
from selayer.verification.static import validate_catalog

#: Fixed, secret-safe failure emitted for the expected I/O error (a missing
#: or unreadable catalog). The exception text is intentionally never
#: interpolated: it can carry the catalog path or echoed source bytes that the
#: loader has already scrubbed out of diagnostics, so echoing it here would
#: re-leak those secrets. Catalog-domain errors never reach this path: they
#: are adapted into a failed report by ``validate_catalog``.
_FAILURE_MESSAGE = "could not read or validate catalog"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="selayer")
    commands = parser.add_subparsers(dest="area", required=True)
    catalog = commands.add_parser("catalog")
    catalog_commands = catalog.add_subparsers(dest="command", required=True)
    validate = catalog_commands.add_parser("validate")
    validate.add_argument("catalog")
    add_okf_commands(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, returning 0 on a passed report and 1 otherwise."""
    args = _parser().parse_args(argv)
    if args.area == "catalog" and args.command == "validate":
        try:
            result = validate_catalog(args.catalog)
        except OSError:
            # The only expected exception that can escape ``validate_catalog``:
            # a missing or unreadable catalog (``OSError``/``FileNotFoundError``
            # from ``Path.read_text``). Catalog-domain errors
            # (``CatalogValidationError``) are already wrapped into a *failed
            # report* by ``validate_catalog``, so they never reach this
            # handler. Do NOT broaden this to ``LookupError``/``ValueError``:
            # ``validate_catalog`` has already adapted the only legitimate
            # ``ValueError`` subclass, so any ``ValueError`` or ``LookupError``
            # (``KeyError``/``IndexError``) escaping here is a programmer mistake
            # that must propagate rather than be silently masked. Never echo
            # the exception text: it may carry secrets.
            print(
                json.dumps({"error": _FAILURE_MESSAGE}, sort_keys=True),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result.report.to_dict(), sort_keys=True))
        return 0 if result.report.passed else 1
    if args.area == "okf":
        # The unified ``okf`` area reuses the legacy command handler for
        # generate/sync/validate/retrieve so parity (stdout/stderr/exit) is
        # exact, and handles the unified-only ``build`` command itself. Domain
        # and I/O errors map to ``error: <message>`` on stderr with exit 1,
        # mirroring ``selayer.okf.cli.main``.
        try:
            if args.command == "build":
                return _run_okf_build(args)
            return execute_okf(args)
        except (OSError, LookupError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    raise AssertionError("unhandled command")


def _run_okf_build(arguments: argparse.Namespace) -> int:
    """Compose a fresh bundle via ``OkfBundle.build`` and report counts."""
    layer = SemanticLayer.load(arguments.catalog)
    bundle = OkfBundle.build(
        layer,
        arguments.destination,
        references_dir=arguments.references,
        overlays_dir=arguments.overlays,
    )
    print(
        json.dumps(
            {
                "command": "build",
                "concepts": len(bundle.concepts),
                "destination": str(arguments.destination),
                "diagnostics": len(bundle.diagnostics),
            },
            sort_keys=True,
        )
    )
    return 0


def run() -> None:
    """Console-script entry point."""
    raise SystemExit(main())


if __name__ == "__main__":
    run()
