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
unreadable catalog); that alone is caught. The unified ``okf`` area (``generate``/``sync``/``validate``/``retrieve``
plus unified-only ``build``) wraps its handlers in the same
``except (OSError, LookupError, ValueError)`` envelope, but emits a fixed,
secret-safe JSON payload on stderr and never interpolates the exception:
those errors can carry credentials, authenticated locations, paths, or raw
driver text. The legacy ``selayer-okf`` CLI keeps its own
``error: <message>`` envelope; only the unified path is hardened.

Programmer mistakes such as
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
from pathlib import Path

from selayer.catalog import SemanticLayer
from selayer.okf import OkfBundle
from selayer.okf.cli import add_okf_commands, execute_okf
from selayer.planning.types import FilterInput, QueryRequest, RangeFilter
from selayer.sources.profile_file import (
    ProfileFileValidationError,
    load_profile_file,
)
from selayer.sources.profiles import MappingProfileResolver
from selayer.verification import CompatibilityCheck, PhysicalCheck, verify
from selayer.verification.static import validate_catalog

#: Fixed, secret-safe failure emitted for the expected I/O error (a missing
#: or unreadable catalog). The exception text is intentionally never
#: interpolated: it can carry the catalog path or echoed source bytes that the
#: loader has already scrubbed out of diagnostics, so echoing it here would
#: re-leak those secrets. Catalog-domain errors never reach this path: they
#: are adapted into a failed report by ``validate_catalog``.
_FAILURE_MESSAGE = "could not read or validate catalog"

#: Fixed, secret-safe failure emitted for the unified ``okf`` area when a
#: domain or I/O error escapes ``OkfBundle.build`` or the shared OKF
#: ``_execute`` handler. The exception text is never interpolated: an
#: ``OSError``/``ValueError``/``LookupError`` can carry a catalog path,
#: credentials, authenticated locations, or raw driver errors, so echoing it
#: would re-leak secrets the loaders already scrubbed. The legacy
#: ``selayer-okf`` CLI keeps its own ``error: <message>`` envelope and exit
#: code; only the unified ``okf`` area is hardened here.
_OKF_FAILURE_MESSAGE = "okf command failed"

#: Fixed, secret-safe failure emitted for the ``catalog compatibility``
#: command when a domain or I/O error escapes. Like the other envelopes, the
#: exception text is never interpolated: a malformed query-cases file may echo
#: attacker-controlled or credential-bearing bytes, and an ``OSError`` may
#: carry the catalog/query-cases path, so echoing either would re-leak what
#: the loaders already scrubbed. An invalid catalog never reaches this path:
#: it is adapted into a failed static report by ``validate_catalog``.
_COMPAT_FAILURE_MESSAGE = "could not run compatibility check"

#: Fixed, secret-safe failure emitted for the ``catalog audit`` command when a
#: domain or I/O error escapes.  Like the other envelopes, the exception text
#: is never interpolated: a profile document may carry credential-bearing
#: bytes (and a missing/malformed profile file raises a sanitized
#: ``ProfileFileValidationError`` whose message is constant but whose
#: filesystem path or environment name must still never be echoed), while an
#: ``OSError`` may carry the catalog path.  An invalid catalog never reaches
#: this path: it is adapted into a failed static report by
#: ``validate_catalog``.
_AUDIT_FAILURE_MESSAGE = "could not run physical audit"

#: Object keys accepted by a query-cases JSON entry. Whitelisting (rather
#: than forwarding arbitrary keys) enforces "do not accept SQL or expression
#: text": a ``sql``/``expression``/``where`` key is rejected up front.
_ALLOWED_QUERY_CASE_KEYS = frozenset({"metrics", "dimensions", "filters"})

#: JSON scalar types accepted as a filter value (or list/range member). A
#: filter value may carry a secret, so only these primitive JSON shapes are
#: permitted; everything else is rejected before construction.
_FILTER_SCALAR_TYPES = (str, int, float, bool, type(None))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="selayer")
    commands = parser.add_subparsers(dest="area", required=True)
    catalog = commands.add_parser("catalog")
    catalog_commands = catalog.add_subparsers(dest="command", required=True)
    validate = catalog_commands.add_parser("validate")
    validate.add_argument("catalog")
    compatibility = catalog_commands.add_parser("compatibility")
    compatibility.add_argument("catalog")
    # ``append`` lets the flags repeat; the default empty list means "all".
    compatibility.add_argument("--metric", action="append", default=[])
    compatibility.add_argument("--dimension", action="append", default=[])
    compatibility.add_argument("--query-cases", action="append", default=[])
    audit = catalog_commands.add_parser("audit")
    audit.add_argument("catalog")
    audit.add_argument("--profiles")
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
    if args.area == "catalog" and args.command == "compatibility":
        return _run_catalog_compatibility(args)
    if args.area == "catalog" and args.command == "audit":
        return _run_catalog_audit(args)
    if args.area == "okf":
        # The unified ``okf`` area reuses the legacy command handler for
        # generate/sync/validate/retrieve so parity (stdout/exit) is exact on
        # success, and handles the unified-only ``build`` command itself. The
        # success path delegates to the shared ``_execute`` handler; on the
        # failure path the unified area emits a fixed, secret-safe JSON
        # envelope (no exception text), unlike the legacy ``selayer-okf`` CLI,
        # which keeps its ``error: <message>`` form. Exit code (1) is preserved.
        try:
            if args.command == "build":
                return _run_okf_build(args)
            return execute_okf(args)
        except (OSError, LookupError, ValueError):
            # Secret-safe failure for the unified ``okf`` area: never echo the
            # exception, whose text can carry credentials, authenticated
            # locations, paths, or raw driver errors. The legacy
            # ``selayer-okf`` CLI keeps its own ``error: <message>`` envelope
            # and exit code; only the unified path is hardened here.
            print(
                json.dumps({"error": _OKF_FAILURE_MESSAGE}, sort_keys=True),
                file=sys.stderr,
            )
            return 1
    raise AssertionError("unhandled command")


def _selector_list(value: object, field: str, *, non_empty: bool) -> list[str]:
    """Validate a *present* selector-list value as non-empty strings.

    ``value`` must be a list whose every entry is a non-empty string;
    ``non_empty`` additionally requires at least one entry. The caller decides
    whether an *absent* key is permitted before calling, so a ``None`` reaching
    here means the key was present but explicitly null, which is always
    rejected (only omission/``[]`` are allowed for optional selectors). A bare
    string is rejected: it would otherwise be character-iterated into
    single-letter "selectors" by :class:`QueryRequest`. ``ValueError`` (not
    ``TypeError``) is raised so the caller's secret-safe
    ``(OSError, ValueError)`` envelope masks it.
    """
    if value is None:
        # Present-but-null is always invalid; the caller permits omission by
        # not calling this function at all.
        raise ValueError(f"query case {field} must not be null")
    if not isinstance(value, list):
        raise ValueError(f"query case {field} must be a list")  # noqa: TRY004
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"query case {field} entries must be non-empty strings")
        result.append(item)
    if non_empty and not result:
        raise ValueError(f"query case {field} must not be empty")
    return result


def _normalise_filter_value(value: object) -> FilterInput:
    """Validate and normalise one JSON filter value for :class:`QueryRequest`.

    Accepts exactly the three JSON-expressible filter forms and rejects
    everything else, so a malformed value (for example a non-mapping
    ``filters`` object) can never reach ``QueryRequest`` and trigger an
    ``AttributeError``/traceback:

    * a scalar (``str``/``int``/``float``/``bool``/``null``) is passed through
      and normalised to a :class:`~selayer.planning.types.ScalarFilter`;
    * a list of scalars is passed through and normalised to a ``ListFilter``;
    * a range object ``{"start": scalar, "end": scalar}`` becomes a
      :class:`~selayer.planning.types.RangeFilter`.
    """
    if isinstance(value, _FILTER_SCALAR_TYPES):
        return value
    if isinstance(value, list):
        scalars: list[str | int | float | bool | None] = []
        for item in value:
            if not isinstance(item, _FILTER_SCALAR_TYPES):
                raise ValueError("query case filter list values must be scalars")  # noqa: TRY004
            scalars.append(item)
        return scalars
    if isinstance(value, dict):
        if set(value) != {"start", "end"}:
            raise ValueError("query case range filter must have start and end")
        start = value["start"]
        end = value["end"]
        if not isinstance(start, _FILTER_SCALAR_TYPES) or not isinstance(
            end, _FILTER_SCALAR_TYPES
        ):
            raise ValueError("query case range filter bounds must be scalars")  # noqa: TRY004
        return RangeFilter(start, end)
    raise ValueError("query case filter value has an unsupported shape")


def _query_request_from_json(entry: object) -> QueryRequest:
    """Build a :class:`QueryRequest` from one query-cases JSON object.

    Every field's shape is validated *before* construction so a malformed
    query-cases file can never trigger an ``AttributeError``/traceback (for
    example a non-mapping ``filters`` value) and selector names are never
    silently character-iterated. Only ``metrics``/``dimensions``/``filters``
    are accepted: any other key (``sql``/``expression``/``where``/...) is
    rejected so SQL or expression text can never reach the planner.

    ``metrics`` is **required**: it must be present, non-null, and a non-empty
    list of non-empty strings. ``dimensions`` is optional for metric-alone
    cases: it may be omitted entirely or be an empty list, but a
    present-but-null value (or a non-list, or a bad member) is rejected.
    ``filters`` is optional: an *omitted* key yields ``{}``, but a present
    value must be a mapping whose values are scalar, list, or range forms — a
    present ``null`` or any other non-mapping shape is rejected (it would
    otherwise reach ``QueryRequest.__init__`` and call ``.items()`` on it,
    raising ``AttributeError``). ``entry.get`` cannot distinguish omission from
    an explicit ``null``, so presence is checked with ``in``. Invalid shapes
    raise ``ValueError`` for the caller's secret-safe envelope.
    """
    if not isinstance(entry, dict):
        raise ValueError("query case must be a JSON object")  # noqa: TRY004
    unknown = set(entry) - _ALLOWED_QUERY_CASE_KEYS
    if unknown:
        raise ValueError("query case has unknown keys")
    # ``metrics`` is required: a missing key, explicit null, empty list,
    # non-list, or bad member is rejected up front.
    if "metrics" not in entry:
        raise ValueError("query case metrics must be present")
    metrics = _selector_list(entry["metrics"], "metrics", non_empty=True)
    # ``dimensions`` is optional: only omission or ``[]`` are permitted for a
    # metric-alone case. A present-but-null value is rejected (only
    # omission/``[]`` allowed); non-list and bad members are rejected.
    dimensions: list[str]
    if "dimensions" not in entry:
        dimensions = []
    else:
        dimensions = _selector_list(entry["dimensions"], "dimensions", non_empty=False)
    # ``filters`` is optional but, when present, must be a mapping. Only an
    # *omitted* key defaults to ``{}``; an explicit ``null`` or any other
    # non-mapping shape is rejected so it never reaches
    # ``QueryRequest.__init__`` (which would call ``.items()`` on it and raise
    # ``AttributeError``). ``entry.get`` returns ``None`` for both omission and
    # an explicit ``null``, so presence is checked with ``in`` to tell them
    # apart.
    if "filters" not in entry:
        normalised_filters: dict[str, FilterInput] = {}
    else:
        raw_filters = entry["filters"]
        if not isinstance(raw_filters, dict):
            raise ValueError("query case filters must be a JSON object")
        normalised_filters = {
            key: _normalise_filter_value(item) for key, item in raw_filters.items()
        }
    return QueryRequest(metrics, dimensions, normalised_filters)


def _load_query_cases(paths: Sequence[str]) -> tuple[QueryRequest, ...]:
    """Load every query-cases JSON file into validated ``QueryRequest``s.

    Each file must hold a JSON list of objects accepted by
    :func:`_query_request_from_json`. ``ValueError`` (bad JSON, wrong shape,
    unknown keys) and ``OSError`` (missing/unreadable file) are raised for the
    caller's secret-safe envelope.
    """
    cases: list[QueryRequest] = []
    for path in paths:
        with Path(path).open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            raise ValueError("query-cases file must hold a JSON list")  # noqa: TRY004
        for entry in data:
            cases.append(_query_request_from_json(entry))
    return tuple(cases)


def _run_catalog_compatibility(arguments: argparse.Namespace) -> int:
    """Run compatibility verification and render its report as JSON.

    An invalid catalog is reported via the static failure report produced by
    :func:`validate_catalog` (there is no layer to verify compatibility
    against). I/O and query-cases domain errors are masked by the same
    secret-safe envelope used elsewhere: their text may carry paths,
    credentials, or attacker-controlled bytes, so it is never echoed.
    """
    try:
        result = validate_catalog(arguments.catalog)
        if result.layer is None:
            print(json.dumps(result.report.to_dict(), sort_keys=True))
            return 1
        query_cases = _load_query_cases(arguments.query_cases)
        check = CompatibilityCheck(
            metrics=tuple(arguments.metric) or None,
            dimensions=tuple(arguments.dimension) or None,
            query_cases=query_cases,
        )
        report = verify(result.layer, check)
    except (OSError, ValueError):
        # Secret-safe failure: never echo the exception, whose text may carry
        # the catalog/query-cases path, credentials, or attacker-controlled
        # bytes from a malformed query-cases file. Do not broaden to
        # ``LookupError``: ``KeyError``/``IndexError`` here is a programmer
        # mistake that must propagate.
        print(
            json.dumps({"error": _COMPAT_FAILURE_MESSAGE}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed else 1


def _run_catalog_audit(arguments: argparse.Namespace) -> int:
    """Run physical verification and render its report as JSON.

    Handler order:

    1. :func:`validate_catalog` — an invalid catalog has no layer, so its
       static failure report is printed and the command exits 1.
    2. profile resolution — when ``--profiles`` is absent an empty
       :class:`MappingProfileResolver` is used; otherwise
       :func:`load_profile_file` resolves the version-1 document (reading
       environment values, never retaining or rendering them).
    3. :func:`verify` with a :class:`PhysicalCheck` carrying only the profile
       resolver.  An arrow-provider resolver is intentionally *not* initialized
       from configuration: the physical audit is a credential-free, exact
       full-scan, and a ``pyarrow`` source audits as ``unavailable`` (an
       incomplete, non-passing report) rather than reading caller-supplied
       objects.

    Profile-file and I/O errors are masked by the same secret-safe envelope
    used elsewhere: a missing environment variable, a malformed profile
    document, or an unreadable file can carry paths or credential-bearing
    bytes, so the exception text is never echoed.
    """
    try:
        result = validate_catalog(arguments.catalog)
        if result.layer is None:
            print(json.dumps(result.report.to_dict(), sort_keys=True))
            return 1
        if arguments.profiles is None:
            resolver: MappingProfileResolver = MappingProfileResolver({})
        else:
            resolver = load_profile_file(arguments.profiles)
        report = verify(result.layer, PhysicalCheck(profiles=resolver))
    except (OSError, ProfileFileValidationError):
        # Secret-safe failure: never echo the exception.  A
        # ``ProfileFileValidationError`` is already sanitized (its code/path/
        # message are constants), but its filesystem path or environment name
        # must still never be echoed; an ``OSError`` may carry the catalog
        # path.  Do not broaden to ``LookupError``/``ValueError``: a
        # ``KeyError``/``IndexError``/``ValueError`` escaping
        # ``verify_physical`` (e.g. a malformed grain identifier) is a
        # programmer mistake that must propagate rather than be masked.
        print(
            json.dumps({"error": _AUDIT_FAILURE_MESSAGE}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed else 1


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
