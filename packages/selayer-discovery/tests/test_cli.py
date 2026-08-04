from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from selayer_discovery import __version__
from selayer_discovery.cli import main


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
