from __future__ import annotations

import argparse
from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="selayer-discovery")


def main(argv: Sequence[str] | None = None) -> int:
    _parser().parse_args(argv)
    return 0


def run() -> None:
    raise SystemExit(main())
